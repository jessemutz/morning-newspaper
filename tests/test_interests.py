"""interests.json inputs: topics, recent media additions, weight redistribution, and the
search filters that keep homepages out. No network, no LLM."""
import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
MODS = ["kobo_config", "koreader_state", "llm", "gather_candidates", "kobo_interests"]


@pytest.fixture
def mods(tmp_path, monkeypatch):
    cfg = {"data_dir": str(tmp_path / "data"), "edition_title": "The Daily", "topics": ["stoicism"]}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    monkeypatch.setenv("KOBO_PAPER_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    for m in MODS:
        sys.modules.pop(m, None)
    spec = importlib.util.spec_from_file_location("kobo_interests", SCRIPTS / "kobo-interests.py")
    ki = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ki)
    yield ki
    for m in MODS:
        sys.modules.pop(m, None)


@pytest.mark.parametrize("name,kind,want", [
    ("Tombstone 1993 UHD BluRay 2160p DTS-HD MA 5 1 DV HEVC REMUX-FraMeSToR", "movie", ("Tombstone", "1993", "")),
    ("The Wild Robot 2024 UHD BluRay 2160p TrueHD Atmos 7 1 DV HEVC REMUX-FraMeSToR", "movie",
     ("The Wild Robot", "2024", "")),
    ("Ernest et Celestine AKA Ernest and Celestine 2012 1080p", "movie", ("Ernest and Celestine", "2012", "")),
    ("Sonic.the.Hedgehog.2.2022.COMPLETE.BLURAY-INCUBO", "movie", ("Sonic the Hedgehog 2", "2022", "")),
    ("Bluey (2018)", "tv", ("Bluey", "2018", "")),
    ("Star Wars the Clone Wars", "tv", ("Star Wars the Clone Wars", "", "")),
    ("Stephen Graham Jones - The Buffalo Hunter Hunter.m4b", "audiobook",
     ("The Buffalo Hunter Hunter", "", "Stephen Graham Jones")),
    ("Deep Work By Cal Newport (Unabridged).m4b", "audiobook", ("Deep Work", "", "Cal Newport")),
    ("Blood Meridian_ Or the Evening Redness in the West (Unabridged).m4b", "audiobook",
     ("Blood Meridian", "", "")),
])
def test_media_title(mods, name, kind, want):
    assert mods.media_title(name, kind) == want


def test_own_media_recent_by_newest_file(mods, tmp_path):
    movies = tmp_path / "Movies"
    now = time.time()
    for name, age_days in [("Old Film 1990 1080p", 400), ("New Film 2025 2160p", 3), ("Kids Show 2020 1080p", 2)]:
        d = movies / name
        d.mkdir(parents=True)
        f = d / "movie.mkv"
        f.write_text("x")
        os.utime(f, (now - age_days * 86400,) * 2)
        os.utime(d, (now,) * 2)  # folder mtime is noise: a bulk touch makes every folder "new"
    future = movies / "Clock Skew 2022"
    future.mkdir()
    (future / "a.mkv").write_text("x")
    os.utime(future / "a.mkv", (now + 50 * 365 * 86400,) * 2)
    cfg = {**mods.CFG, "media_dirs": {str(movies): "movie"}, "exclude_titles": ["kids show"]}
    names = [e["name"] for e in mods.own_media(cfg)]
    assert names == ["New Film (2025)"]


def test_empty_inputs_give_weight_away(mods):
    src = {"kobo": lambda: [], "abs": lambda: (_ for _ in ()).throw(RuntimeError("off")),
           "own_media": lambda: (_ for _ in ()).throw(RuntimeError("media_dirs not set"))}
    doc = mods.build({**mods.CFG, "topics": ["stoicism"]}, src)
    w = doc["weights"]
    assert w["topics"] == 1.0 and w["books_reading"] == 0 and w["own_media"] == 0
    assert "media_dirs not set" in doc["weight_notes"]["own_media"]


def test_weights_with_books_and_topics(mods):
    book = {"kind": "book", "name": "Blood Over Bright Haven", "creator": "M. L. Wang", "date": "2026-11-25"}
    src = {"kobo": lambda: [book], "abs": lambda: [], "own_media": lambda: []}
    doc = mods.build({**mods.CFG, "topics": ["stoicism"], "interest_weights": {"topics": 0.5}}, src)
    w = doc["weights"]
    assert w["books_reading"] == pytest.approx(0.38 / 0.88, abs=1e-3)
    assert w["topics"] == pytest.approx(0.5 / 0.88, abs=1e-3)
    names = [e["name"] for e in doc["inputs"]["books_reading"]["entries"]]
    assert names == ["Blood Over Bright Haven", "M. L. Wang"]


def test_paper_is_not_a_book_you_are_reading(mods):
    import koreader_state as ks
    now = datetime.now(timezone.utc).timestamp()
    ks.READING.mkdir(parents=True)
    (ks.READING / "state.json").write_text(json.dumps({
        "a": {"title": "The Daily — Nov 25, 2026", "percent": 0.4, "updated": now},
        "b": {"title": "Blood Over Bright Haven", "percent": 0.28, "updated": now}}))
    assert [b["title"] for b in ks.currently_reading()] == ["Blood Over Bright Haven"]


@pytest.mark.parametrize("url,listing", [
    ("https://reactormag.com/", True),
    ("https://collider.com/tag/science-fiction/", True),
    ("https://www.nytimes.com/section/books", True),
    ("https://en.wikipedia.org/wiki/Stoicism", True),
    ("https://aeon.co/essays/what-the-stoics-got-right", False),
    ("https://www.nytimes.com/2026/11/20/books/review/new-fiction.html", False),
    ("https://psyche.co/guides/how-to-plan-your-day-with-adhd", False),
])
def test_is_listing(mods, url, listing):
    import gather_candidates
    assert gather_candidates.is_listing(url) is listing
