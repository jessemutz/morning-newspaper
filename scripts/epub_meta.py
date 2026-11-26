"""Shelf books as sent to the Kindle: the library EPUB with its library metadata written in.

Calibre-Web/CWA keep the truth in metadata.db and cover.jpg; the EPUB file on disk often still
carries whatever it was imported with (CWA's embed step fails on some setups, e.g. a network
share). KOReader and its plugins only read the file, so before sending a book we rewrite its OPF
(title, authors, series, series index) and its cover from the library. The original is never
touched: the result is cached in <data_dir>/bookcache/<id>-<key>.epub and rebuilt only when the
EPUB, the cover or the metadata change.
"""
import hashlib
import json
import re
import shutil
import zipfile
from html import escape
from pathlib import Path
from posixpath import dirname, join, normpath

from kobo_config import DATA_DIR

CACHE = DATA_DIR / "bookcache"
_VERSION = 1  # bump when the rewrite changes, to rebuild every cached copy


def _key(book: dict) -> str:
    parts = [_VERSION, book["title"], book["authors"], book.get("series") or "",
             book.get("series_index") or ""]
    for p in (book["path"], book.get("cover")):
        if p and Path(p).exists():
            st = Path(p).stat()
            parts += [str(p), st.st_mtime_ns, st.st_size]
    return hashlib.sha1(json.dumps(parts, default=str).encode()).hexdigest()[:16]


def _index(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    return str(int(f)) if f == int(f) else str(f)


def rewrite_opf(opf: str, book: dict, cover_href: "str | None") -> str:
    """Replace title/authors/series in an OPF document, and point its cover at cover_href."""
    m = re.search(r"(<(?:\w+:)?metadata\b[^>]*>)(.*?)(</(?:\w+:)?metadata>)", opf, re.S)
    if not m:
        return opf
    body = m.group(2)
    drop = [
        r"<dc:title\b[^>]*>.*?</dc:title>",
        r"<dc:creator\b[^>]*>.*?</dc:creator>",  # every creator: Calibre's authors are the list
        r"<meta\b[^>]*name=\"calibre:(?:series|series_index|title_sort)\"[^>]*/>",
        r"<meta\b[^>]*property=\"belongs-to-collection\"[^>]*>.*?</meta>",
        r"<meta\b[^>]*property=\"(?:collection-type|group-position)\"[^>]*>.*?</meta>",
        r"<meta\b[^>]*property=\"(?:title-type|file-as|role|display-seq)\"[^>]*>.*?</meta>",
    ]
    if cover_href:
        drop.append(r"<meta\b[^>]*name=\"cover\"[^>]*/>")
    for pat in drop:
        body = re.sub(pat, "", body, flags=re.S)
    add = [f"<dc:title>{escape(book['title'], quote=False)}</dc:title>"]
    for a in [a.strip() for a in book["authors"].split(",") if a.strip()] or ["Unknown"]:
        add.append(f'<dc:creator opf:role="aut">{escape(a, quote=False)}</dc:creator>')
    if book.get("series"):
        add.append(f'<meta name="calibre:series" content="{escape(book["series"])}"/>')
        idx = _index(book.get("series_index"))
        if idx:
            add.append(f'<meta name="calibre:series_index" content="{idx}"/>')
    if cover_href:
        add.append('<meta name="cover" content="mp-cover"/>')
    body = "\n    " + "\n    ".join(add) + body
    out = opf[: m.start()] + m.group(1) + body + m.group(3) + opf[m.end():]
    # opf:role needs the opf prefix declared somewhere
    if "xmlns:opf=" not in out:
        out = re.sub(r"(<(?:\w+:)?package\b)", r'\1 xmlns:opf="http://www.idpf.org/2007/opf"', out, count=1)
    if cover_href:
        out = re.sub(r'\s*<item\b[^>]*\bid="mp-cover"[^>]*/>', "", out)
        out = re.sub(r'(<item\b[^>]*\bproperties=")([^"]*)\bcover-image\b\s*', r"\1\2", out)
        out = out.replace('properties=""', "")
        epub3 = re.search(r'<(?:\w+:)?package\b[^>]*\bversion="3', out) is not None
        props = ' properties="cover-image"' if epub3 else ""
        item = f'<item id="mp-cover" href="{escape(cover_href)}" media-type="image/jpeg"{props}/>'
        out = re.sub(r"(</(?:\w+:)?manifest>)", item + r"\1", out, count=1)
    return out


def _old_cover(opf: str, opf_dir: str) -> "tuple[str | None, str | None]":
    """(zip path, media type) of the cover image the OPF names, if any."""
    items = {}
    for it in re.finditer(r"<item\b[^>]*/?>", opf):
        tag = it.group(0)
        get = lambda a: (re.search(rf'\b{a}="([^"]*)"', tag) or [None, None])[1]  # noqa: E731
        items[get("id")] = (get("href"), get("media-type"), get("properties") or "")
    for href, mt, props in items.values():
        if "cover-image" in props.split():
            return normpath(join(opf_dir, href)), mt
    m = re.search(r'<meta\b[^>]*name="cover"[^>]*content="([^"]+)"', opf) or \
        re.search(r'<meta\b[^>]*content="([^"]+)"[^>]*name="cover"', opf)
    if m and m.group(1) in items:
        href, mt, _ = items[m.group(1)]
        return normpath(join(opf_dir, href)), mt
    return None, None


def build(book: dict, dest: Path) -> None:
    src = Path(book["path"])
    cover = Path(book["cover"]) if book.get("cover") and Path(book["cover"]).exists() else None
    with zipfile.ZipFile(src) as zin:
        container = zin.read("META-INF/container.xml").decode("utf-8", "replace")
        opf_path = re.search(r'full-path="([^"]+)"', container).group(1)
        opf = zin.read(opf_path).decode("utf-8")
        opf_dir = dirname(opf_path)
        old_cover, old_type = _old_cover(opf, opf_dir)
        cover_bytes = cover.read_bytes() if cover else None
        # A JPEG cover is replaced in place, so a cover page inside the book shows it too;
        # otherwise the library cover goes in as a new item.
        replace_in_place = bool(cover_bytes and old_cover and old_type == "image/jpeg"
                                and old_cover in zin.namelist())
        new_cover = None
        if cover_bytes and not replace_in_place:
            new_cover = normpath(join(opf_dir, "mp-cover.jpg"))
        href = (Path(new_cover).name if new_cover else None)
        new_opf = rewrite_opf(opf, book, href)
        tmp = dest.with_suffix(".tmp")
        with zipfile.ZipFile(tmp, "w") as zout:
            names = zin.namelist()
            if "mimetype" in names:  # must be first and stored
                zout.writestr(zipfile.ZipInfo("mimetype"), zin.read("mimetype"), zipfile.ZIP_STORED)
            for info in zin.infolist():
                n = info.filename
                if n == "mimetype" or n == new_cover:
                    continue
                if n == opf_path:
                    data = new_opf.encode("utf-8")
                elif replace_in_place and n == old_cover:
                    data = cover_bytes
                else:
                    data = zin.read(n)
                zout.writestr(n, data, zipfile.ZIP_DEFLATED)
            if new_cover:
                zout.writestr(new_cover, cover_bytes, zipfile.ZIP_DEFLATED)
        tmp.replace(dest)


def prepared(book: dict) -> Path:
    """Path of the EPUB to send for a shelf book; falls back to the library file on any error."""
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        dest = CACHE / f"{book['id']}-{_key(book)}.epub"
        if not dest.exists():
            build(book, dest)
            for old in CACHE.glob(f"{book['id']}-*.epub"):
                if old != dest:
                    old.unlink(missing_ok=True)
        return dest
    except Exception as e:  # a broken EPUB must still sync, just as it is
        import sys
        sys.stderr.write(f"epub_meta: book {book.get('id')} sent unmodified: {type(e).__name__}: {e}\n")
        return Path(book["path"])


def cached_copies() -> "dict[Path, int]":
    """Cached copies on disk -> library book id (for matching KOReader's md5 to a book)."""
    out = {}
    for p in CACHE.glob("*-*.epub"):
        try:
            out[p] = int(p.name.split("-", 1)[0])
        except ValueError:
            pass
    return out
