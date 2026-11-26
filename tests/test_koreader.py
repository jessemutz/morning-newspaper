"""KOReader delivery: progress state, partial MD5, text slicing, spoiler guard, selection,
the plugin server, and a dated-edition build. No network, no LLM, no Calibre."""
import hashlib
import importlib
import json
import shutil
import subprocess
import sys
import threading
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
OURS = ["kobo_config", "koreader_state", "llm", "gather_candidates", "curate", "book_companion", "paper_server", "epub_meta"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fresh imports of the pipeline modules against a temp data_dir."""
    cfg = {"data_dir": str(tmp_path / "data"), "calibre_library": str(tmp_path / "lib"),
           "llm_base_url": "http://127.0.0.1:9/v1", "articles_per_edition": 4}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    monkeypatch.setenv("KOBO_PAPER_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    for m in OURS:
        sys.modules.pop(m, None)
    yield lambda name: importlib.import_module(name)
    for m in OURS:
        sys.modules.pop(m, None)


def test_partial_md5_samples_koreader_offsets(env, tmp_path):
    ks = env("koreader_state")
    data = bytes(range(256)) * 20000  # ~5 MB
    f = tmp_path / "b.epub"
    f.write_bytes(data)
    h = hashlib.md5()
    for off in [0] + [1024 << (2 * i) for i in range(11)]:
        if off >= len(data):
            break
        h.update(data[off:off + 1024])
    assert ks.partial_md5(f) == h.hexdigest()


def test_snapshot_state_and_currently_reading(env):
    ks = env("koreader_state")
    now = datetime.now(timezone.utc)
    t = int(now.timestamp())
    ks.record_snapshot({"books": [{"md5": "abc", "title": "Dune", "authors": "Frank Herbert",
                                   "percent": 0.3, "chapter": "Book I", "updated": t - 100}],
                        "stats": [{"md5": "abc", "date": "2026-11-24", "pages": 30, "seconds": 1800}]})
    # an older snapshot arriving late must not move progress backwards
    ks.record_snapshot({"books": [{"md5": "abc", "percent": 0.1, "updated": t - 5000}]})
    cur = ks.currently_reading(now=now)
    assert len(cur) == 1 and cur[0]["percent"] == 0.3 and cur[0]["stats"]["2026-11-24"]["pages"] == 30
    assert ks.HISTORY.read_text().count("\n") == 2


def test_slice_text_stops_before_position(env):
    bc = env("book_companion")
    text = "\n\n".join(f"Paragraph {i} " + "x" * 90 for i in range(100))
    s = bc.slice_text(text, 0.10, 0.20)
    assert s.startswith("Paragraph 1") and "Paragraph 20 " not in s
    assert bc.slice_text(text, 0.5, 0.5) == ""
    parts = bc.chunks(text, 1000)
    assert "".join(parts) == text and all(len(p) <= 1000 for p in parts)


def test_spoiler_guard_without_llm_drops_mentions(env):
    cu = env("curate")
    books = [{"name": "The Name of the Wind", "creator": "Patrick Rothfuss", "progress_pct": 40}]
    cands = [
        {"url": "https://a.com/1", "title": "The Name of the Wind ending explained", "snippet": ""},
        {"url": "https://b.com/2", "title": "Rothfuss on Wind and doors", "snippet": ""},
        {"url": "https://c.com/3", "title": "A new fantasy map of the world", "snippet": ""},
    ]
    kept, dropped = cu.guard(cands, books, use_llm=False)
    assert [c["url"] for c in kept] == ["https://c.com/3"]
    assert len(dropped) == 2


def test_select_honours_quota_and_site_cap(env):
    cu = env("curate")
    interests = {"inputs": {"books_reading": {"weight": 0.5}, "topics": {"weight": 0.5}}}
    cands = [{"url": f"https://same.com/{i}", "score": 9, "interest": "topics"} for i in range(5)]
    cands += [{"url": f"https://s{i}.com/x", "score": 6, "interest": "books_reading"} for i in range(3)]
    picked = cu.select(cands, interests, 4)
    sites = [c["url"].split("/")[2] for c in picked]
    assert len(picked) == 4 and sites.count("same.com") == 2


def test_server_manifest_and_progress(env, tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_TOKEN", "t0k")
    ps = env("paper_server")
    ps.EDITIONS.mkdir(parents=True)
    (ps.EDITIONS / "Morning Paper 2026-11-25.epub").write_bytes(b"epub-bytes")
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ps.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(path, body=None, token="t0k"):
        req = urllib.request.Request(base + path, data=body, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()

    with pytest.raises(urllib.error.HTTPError):
        call("/api/manifest", token="wrong")
    _, body = call("/api/manifest")
    items = json.loads(body)["items"]
    assert items[0]["filename"] == "Morning Paper 2026-11-25.epub"
    assert items[0]["sha256"] == hashlib.sha256(b"epub-bytes").hexdigest()
    assert " " not in items[0]["url"]  # KOReader sends URLs as-is: spaces break the request
    assert call(items[0]["url"])[1] == b"epub-bytes"
    with pytest.raises(urllib.error.HTTPError):
        call("/files/edition/..%2F..%2Fconfig.json")
    call("/api/progress", json.dumps({"books": [{"md5": "m", "title": "T", "percent": 0.5}]}).encode())
    assert json.loads((tmp_path / "data" / "reading" / "state.json").read_text())["m"]["percent"] == 0.5
    srv.shutdown()


def test_koreader_edition_build(tmp_path):
    data = tmp_path / "data"
    shutil.copytree(ROOT / "fixtures" / "articles", data / "articles")
    urls = [json.loads(p.read_text())["url"] for p in (data / "articles").glob("*.json")]
    # a fixture article whose file name is its URL slug is "already stored": no network
    for p in list((data / "articles").glob("*.json")):
        p.rename(data / "articles" / f"{hashlib.sha1(json.loads(p.read_text())['url'].encode()).hexdigest()[:16]}.json")
    (data / "queue.txt").write_text("\n".join(urls) + "\n")
    (tmp_path / "c.json").write_text(json.dumps({"data_dir": str(data), "delivery": "koreader",
                                                 "lead_image_avoid_faces": False}))
    r = subprocess.run([sys.executable, str(SCRIPTS / "kobo-digest-build.py")], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "KOBO_PAPER_CONFIG": str(tmp_path / "c.json")})
    assert r.returncode == 0, r.stdout + r.stderr
    eds = list((data / "editions").glob("Morning Paper *.epub"))
    assert len(eds) == 1
    with zipfile.ZipFile(eds[0]) as z:
        assert "EPUB/frontpage.xhtml" in z.namelist()
    assert len((data / "seen.jsonl").read_text().splitlines()) == 2
    assert (data / "queue.txt").read_text() == ""


def test_quotes_must_be_verbatim(env):
    bc = env("book_companion")
    src = "He said, \u201cThe barrier holds because we feed it.\u201d Then he left the room quietly."
    qs = [{"quote": '"The barrier holds because we feed it."', "note": "x"},
          {"quote": "The barrier holds because the gods will it.", "note": "invented"},
          {"quote": "short", "note": "too short"}]
    kept = bc.verified_quotes(qs, src)
    assert [q["quote"] for q in kept] == ["The barrier holds because we feed it."]
    # a near-miss of a real passage is replaced by the passage itself
    near = bc.verified_quotes([{"quote": "Then he left the room very quietly.", "note": "x"}], src, near=src)
    assert near and near[0]["quote"] == "Then he left the room quietly."


def test_companion_render_sections(env):
    bc = env("book_companion")
    c = {"kind": "fiction", "recap": ["A happened."], "session_summary": "A.", "story_so_far": "S",
         "key_points": ["k"], "people": [{"name": "Sciona", "note": "mage"}], "terms": [], "threads": ["t?"],
         "quotes": [{"quote": "q" * 25, "note": "n"}], "recall": [{"q": "Who?", "a": "Sciona"}], "tips": ["tip"]}
    sessions = [{"label": "Mon", "from": 0.1, "to": 0.2, "pages": 20, "minutes": 30, "summary": "s1"},
                {"label": "Tue", "from": 0.2, "to": 0.3, "pages": 10, "minutes": 15, "summary": "s2"}]
    out = bc.render("Book", "Author", {"percent": 0.3, "chapter": "Ch 7"}, c, sessions,
                    {"text": "Bio.", "url": ""}, first=False)
    for h in ("Previously", "Your recent sessions", "Key moments", "Passages worth remembering",
              "Test your recall", "About the author", "The story so far", "Recall answers"):
        assert h in out, h
    assert out.index("Test your recall") < out.index("Recall answers")


def _epub(path, opf, extra=()):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<container><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", opf)
        for name, data in extra:
            z.writestr(name, data)


def test_shelf_copy_gets_library_metadata_and_cover(env, tmp_path):
    import xml.etree.ElementTree as ET
    em = env("epub_meta")
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">'
           '<dc:title>Old: Series Book 2</dc:title><dc:creator opf:role="aut">Someone</dc:creator>'
           '<meta name="cover" content="c"/></metadata>'
           '<manifest><item id="c" href="old.png" media-type="image/png"/></manifest></package>')
    src, cover = tmp_path / "b.epub", tmp_path / "cover.jpg"
    _epub(src, opf, [("OEBPS/old.png", b"PNG")])
    cover.write_bytes(b"\xff\xd8JPEG")
    book = {"id": 7, "title": "New & Better", "authors": "A. Writer, B. Writer", "path": src,
            "series": "The Saga", "series_index": 2.0, "cover": cover}
    p = em.prepared(book)
    assert p != src and p.parent == em.CACHE
    z = zipfile.ZipFile(p)
    assert z.namelist()[0] == "mimetype" and z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
    new = z.read("OEBPS/content.opf").decode()
    ET.fromstring(new)  # still well-formed
    assert "<dc:title>New &amp; Better</dc:title>" in new and "Old:" not in new
    assert new.count("<dc:creator") == 2 and "Someone" not in new
    assert 'name="calibre:series" content="The Saga"' in new
    assert 'name="calibre:series_index" content="2"' in new
    assert 'name="cover" content="mp-cover"' in new and 'content="c"' not in new
    assert z.read("OEBPS/mp-cover.jpg") == cover.read_bytes()  # PNG cover: added as a new item
    assert em.prepared(book) == p  # cached
    book["series_index"] = 3.0
    p2 = em.prepared(book)
    assert p2 != p and not p.exists()  # metadata change -> rebuilt, old copy pruned
    assert em.cached_copies() == {p2: 7}


def test_shelf_copy_replaces_jpeg_cover_in_place(env, tmp_path):
    em = env("epub_meta")
    opf = ('<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title></metadata>'
           '<manifest><item id="x" href="img/c.jpg" media-type="image/jpeg" properties="cover-image"/>'
           '</manifest></package>')
    src, cover = tmp_path / "b.epub", tmp_path / "cover.jpg"
    _epub(src, opf, [("OEBPS/img/c.jpg", b"old")])
    cover.write_bytes(b"new")
    p = em.prepared({"id": 1, "title": "T", "authors": "A", "path": src, "cover": cover})
    z = zipfile.ZipFile(p)
    assert z.read("OEBPS/img/c.jpg") == b"new" and "OEBPS/mp-cover.jpg" not in z.namelist()


def test_shelf_copy_falls_back_to_original_on_bad_epub(env, tmp_path):
    em = env("epub_meta")
    bad = tmp_path / "bad.epub"
    bad.write_bytes(b"not a zip")
    assert em.prepared({"id": 2, "title": "T", "authors": "A", "path": bad}) == bad


def test_reading_page_every_edition_without_llm(env, monkeypatch):
    ks = env("koreader_state")
    bc = env("book_companion")
    monkeypatch.setattr(bc.llm, "available", lambda: False)
    monkeypatch.setattr(bc, "author_bio", lambda a: None)  # no network
    now = datetime.now(timezone.utc)
    t = int(now.timestamp())
    ks.record_snapshot({"books": [
        {"md5": "bob", "title": "Blood Over Bright Haven", "authors": "M. L. Wang", "percent": 0.46,
         "page": 452, "pages": 990, "chapter": "Chapter 12", "updated": t - 3600},
        {"md5": "dcc", "title": "Dungeon Crawler Carl", "authors": "Matt Dinniman", "percent": 0.001,
         "updated": t - 60}]})
    bc.COMPANION.mkdir(parents=True, exist_ok=True)
    (bc.COMPANION / "bob.json").write_text(json.dumps({
        "covered_pct": 0.28, "story_so_far": "Sciona studies magic.\nThomil scouts.", "last_day": "2026-11-25",
        "sessions": [{"label": "First catch-up", "from": 0, "to": 0.28, "summary": "Sciona passes her trial."}],
        "last_companion": {"people": [{"name": "Sciona", "note": "a mage"}], "threads": ["Who sent the letter?"]}}))
    assert bc.main() == 0
    arts = sorted(bc.ARTICLES.glob("companion-*.json"))
    assert len(arts) == 1  # the 0.1% book does not crowd out the one being read
    art = json.loads(arts[0].read_text())
    assert art["pin_top"] and art["title"] == "Your reading: Blood Over Bright Haven"
    h = art["html"]
    assert "46% read" in h and "page 452 of 990" in h and "not reachable" in h
    assert "Sciona passes her trial." in h and "Who sent the letter?" in h and "Thomil scouts." in h


def test_error_pages_are_not_articles(tmp_path, monkeypatch):
    import importlib.util
    (tmp_path / "config.json").write_text(json.dumps({"data_dir": str(tmp_path / "data")}))
    monkeypatch.setenv("KOBO_PAPER_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("kdb", SCRIPTS / "kobo-digest-build.py")
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)
    real = "<p>" + "A real paragraph about Stoic practice and daily habits. " * 30 + "</p>"
    assert b.broken_reason("On Stoic habits", real) is None
    assert "error page" in b.broken_reason("IMDb", "<p>JavaScript is disabled. Enable it to continue.</p>")
    assert "error page" in b.broken_reason("Just a moment...", "<p>Checking your browser</p>")
    assert "too short" in b.broken_reason("Home", "<p>Welcome to our site. Sign in.</p>")
    # a long real article that merely mentions such a phrase is kept
    assert b.broken_reason("Why sites say access denied", "<p>access denied</p>" + real * 3) is None
