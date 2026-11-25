"""Reading state reported by the KOReader plugin, and lookups into the CWA library.

The plugin POSTs a snapshot like:
  {"device": "kindle", "sent": 1764050000,
   "books": [{"md5": "...", "title": "...", "authors": "...", "percent": 0.42,
              "page": 120, "pages": 300, "chapter": "Chapter 7", "file": "/mnt/us/...",
              "updated": 1764049000}],
   "stats": [{"md5": "...", "title": "...", "date": "2026-11-24", "pages": 31, "seconds": 2400}]}

We keep:
  reading/history.jsonl   every snapshot, verbatim (append-only)
  reading/state.json      latest view per book md5, with a per-day percent trail

KOReader identifies a book by a "partial MD5" of the file. partial_md5() reproduces it,
so a book on the Kindle can be matched to its file in the Calibre library exactly.
"""
import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from kobo_config import CFG, DATA_DIR

READING = DATA_DIR / "reading"
STATE = READING / "state.json"
HISTORY = READING / "history.jsonl"


def partial_md5(path) -> str:
    """KOReader's util.partialMD5: 1 KiB samples at offsets 0, 1K, 4K, 16K, ... 1024<<20."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for i in range(-1, 11):
            f.seek(0 if i < 0 else 1024 << (2 * i))
            sample = f.read(1024)
            if not sample:
                break
            h.update(sample)
    return h.hexdigest()


def _write_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def record_snapshot(snap: dict, now: "datetime | None" = None) -> dict:
    """Merge one plugin snapshot into state.json. Returns the new state."""
    now = now or datetime.now(timezone.utc)
    READING.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a") as f:
        f.write(json.dumps({"received": now.isoformat(timespec="seconds"), **snap}) + "\n")
    state = load_state()
    for b in snap.get("books", []):
        md5 = b.get("md5")
        if not md5:
            continue
        cur = state.setdefault(md5, {"days": {}, "stats": {}})
        updated = int(b.get("updated") or snap.get("sent") or now.timestamp())
        if updated < cur.get("updated", 0):
            continue  # an older snapshot arriving late from the outbox
        for k in ("title", "authors", "percent", "page", "pages", "chapter", "file"):
            if b.get(k) is not None:
                cur[k] = b[k]
        cur["updated"] = updated
        day = datetime.fromtimestamp(updated).date().isoformat()
        cur["days"][day] = b.get("percent", cur.get("percent"))
    for s in snap.get("stats", []):
        md5 = s.get("md5")
        if md5 and md5 in state and s.get("date"):
            state[md5]["stats"][s["date"]] = {"pages": s.get("pages", 0), "seconds": s.get("seconds", 0)}
    _write_atomic(STATE, state)
    return state


def currently_reading(days: int = 30, now: "datetime | None" = None) -> list[dict]:
    """Books touched in the last `days` days and not finished, most recent first."""
    now = now or datetime.now(timezone.utc)
    cutoff = now.timestamp() - days * 86400
    out = []
    for md5, b in load_state().items():
        if b.get("updated", 0) >= cutoff and (b.get("percent") or 0) < 0.995:
            out.append({"md5": md5, **b})
    return sorted(out, key=lambda b: b["updated"], reverse=True)


# --- Calibre library -------------------------------------------------------------------

def _ro(path):
    """Open a Calibre/CWA database for reading without touching the live file.

    CWA keeps its databases in WAL mode. A read-only open of a WAL database needs shared-memory
    locking, which fails over SMB ("unable to open database file"), and immutable mode would
    skip whatever is still in the -wal file. So copy the database and its -wal into a local
    cache whenever either changes, and read the copy (SQLite folds the WAL in on open)."""
    import shutil
    src = Path(path)
    cache = READING.parent / "dbcache"
    cache.mkdir(parents=True, exist_ok=True)
    stamp_parts = []
    for suffix in ("", "-wal"):
        f = Path(str(src) + suffix)
        if f.exists():
            st = f.stat()
            stamp_parts.append(f"{suffix}:{st.st_mtime_ns}:{st.st_size}")
    stamp = "|".join(stamp_parts)
    dst = cache / src.name
    stamp_file = cache / (src.name + ".stamp")
    if not dst.exists() or not stamp_file.exists() or stamp_file.read_text() != stamp:
        for suffix in ("", "-wal", "-shm"):
            Path(str(dst) + suffix).unlink(missing_ok=True)
        shutil.copyfile(src, dst)
        wal = Path(str(src) + "-wal")
        if wal.exists():
            shutil.copyfile(wal, Path(str(dst) + "-wal"))
        stamp_file.write_text(stamp)
    return sqlite3.connect(dst)


def library_books(library: "str | None" = None) -> list[dict]:
    """Every EPUB in the Calibre library: id, title, authors, path on disk."""
    lib = Path(library or CFG["calibre_library"])
    db = lib / "metadata.db"
    if not db.exists():
        return []
    c = _ro(db)
    rows = c.execute(
        """SELECT b.id, b.title, b.path, d.name, d.format,
                  (SELECT group_concat(a.name, ', ') FROM authors a
                   JOIN books_authors_link l ON l.author = a.id WHERE l.book = b.id)
           FROM books b JOIN data d ON d.book = b.id
           WHERE d.format IN ('EPUB', 'KEPUB')""").fetchall()
    c.close()
    out = []
    for bid, title, rel, name, fmt, authors in rows:
        ext = ".kepub" if fmt == "KEPUB" else ".epub"
        p = lib / rel / f"{name}{ext}"
        if p.exists():
            out.append({"id": bid, "title": title, "authors": authors or "", "path": p, "format": fmt})
    return out


_MD5_CACHE = READING / "md5_cache.json"


def find_book_file(md5: str, title: str = "", authors: str = "", library=None) -> "Path | None":
    """Match KOReader's md5 against the library (cached by path+mtime); fall back to title."""
    try:
        cache = json.loads(_MD5_CACHE.read_text())
    except (OSError, ValueError):
        cache = {}
    books = [b for b in library_books(library) if b["format"] == "EPUB"]
    hit, changed = None, False
    for b in books:
        key = str(b["path"])
        mtime = b["path"].stat().st_mtime
        ent = cache.get(key)
        if not ent or ent["mtime"] != mtime:
            ent = {"mtime": mtime, "md5": partial_md5(b["path"])}
            cache[key] = ent
            changed = True
        if ent["md5"] == md5:
            hit = b["path"]
            break
    if changed:
        _write_atomic(_MD5_CACHE, cache)
    if hit:
        return hit
    # Books sent from Calibre are rewritten on the way, so their md5 differs: match by title
    # (ignoring a subtitle after ":"), preferring the same author.
    def norm(s):
        return " ".join((s or "").lower().split(":")[0].split())
    t = norm(title)
    if t:
        same = [b for b in books if norm(b["title"]) == t]
        same.sort(key=lambda b: norm(authors) not in norm(b["authors"]))
        if same:
            return same[0]["path"]
    return None


def shelf_books(shelf_name: "str | None" = None) -> list[dict]:
    """Books on the CWA/Calibre-Web shelf that syncs to the Kindle."""
    app_db = CFG.get("cwa_app_db")
    if not app_db or not Path(app_db).exists():
        return []
    c = _ro(app_db)
    ids = [r[0] for r in c.execute(
        """SELECT l.book_id FROM book_shelf_link l JOIN shelf s ON s.id = l.shelf
           WHERE lower(s.name) = lower(?) ORDER BY l.[order]""",
        (shelf_name or CFG["kindle_shelf"],))]
    c.close()
    by_id = {}
    for b in library_books():
        # prefer the plain EPUB: KOReader renders it natively, and its md5 is the one we match
        if b["id"] not in by_id or b["format"] == "EPUB":
            by_id[b["id"]] = b
    return [by_id[i] for i in ids if i in by_id]
