#!/usr/bin/env python3
"""Build interests.json: the curation input for the morning paper.

Read-only on every source. Writes one file, atomically, into data_dir (see config.json).
The curation step (an LLM, see README) reads it and picks about 10 articles into queue.txt.

Inputs and base weights (override with config "interest_weights"):
  books_reading  35%  what you are reading (KOReader plugin or Kobo sync, + Audiobookshelf)
                      and the authors of those books
  topics         45%  config "topics": the subjects you want covered
  own_media      12%  recent additions to your media folders (config "media_dirs")
  tiktok          5%  public collections listed with yt-dlp (config list)
  kickstarter     3%  backed projects (IMAP, readonly), at most one item
  conversation, instagram, notes: 0%

Every source is optional. An input that is not configured, fails, or has no entries gives
its weight to the others in proportion, and interests.json says why. curate.py then sets
aside followed_sources_weight of the paper for the feeds you follow and scales these down.
"""
import imaplib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from pathlib import Path
from urllib.request import Request, urlopen

from kobo_config import CFG, DATA_DIR

OUT = DATA_DIR / "interests.json"
CALIBRE_APP_DB = CFG["calibre_web_app_db"]
CALIBRE_META_DB = str(Path(CFG["calibre_library"]) / "metadata.db")
YTDLP = CFG["ytdlp"]

BASE_WEIGHTS = {"books_reading": 0.35, "topics": 0.45, "own_media": 0.12,
                "tiktok": 0.05, "kickstarter": 0.03}
ZERO_WEIGHT = ["conversation_topics", "instagram_saves", "notes_app"]
READING_DAYS = 30

MEDIA_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".m4b", ".mp3", ".m4a", ".flac", ".epub"}

RULES = [
    "Curate from the entries below. Weights say how many of the ~10 articles each input earns.",
    "No generic movies/TV or entertainment-news items. Cover only the named titles, franchises, creators and universes.",
    "Kickstarter: at most one item.",
    "topics: find substantial pieces (essays, research news, interviews, practical guides by people "
    "who know the subject), not homepages, category or tag pages, product listings or SEO filler.",
    "own_media: titles recently added to the reader's media library; news, essays or interviews about "
    "that exact title or its makers only.",
    "Items at most max_age_days old, one primer per topic, queue.txt and pinned.txt always included.",
    "Books marked spoiler_sensitive are being read right now: never pick anything that could reveal "
    "plot, twists or endings past progress_pct. Author interviews, craft essays and adaptation news "
    "that stay clear of the plot are fine.",
]


def today():
    return datetime.now(timezone.utc).date().isoformat()


def load_config():
    return dict(CFG)


def write_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def ro_connect(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def clean_title(t):
    t = re.sub(r"\s*\((?:19|20)\d\d\)\s*$", "", t.strip())
    t = re.sub(r"\s*\((?:Tunarr|seasons?|S\d)[^)]*\)\s*$", "", t, flags=re.I)
    return t.strip()


# --- books_reading ----------------------------------------------------------------

def kobo_reading(cfg, now=None):
    if not Path(CALIBRE_APP_DB).exists():
        raise RuntimeError("calibre_web_app_db not found; set it in config.json")
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=READING_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    app, meta = ro_connect(CALIBRE_APP_DB), ro_connect(CALIBRE_META_DB)
    rows = app.execute(
        """SELECT s.book_id, s.last_modified,
                  COALESCE(b.progress_percent,0)
           FROM kobo_reading_state s JOIN kobo_bookmark b ON b.kobo_reading_state_id = s.id
           WHERE s.user_id = ? AND s.last_modified >= ?""",
        (cfg["kobo_user_id"], cutoff)).fetchall()
    out = []
    for book_id, synced, progress in rows:
        if not progress or progress <= 0:
            continue
        r = meta.execute("SELECT title, author_sort FROM books WHERE id=?", (book_id,)).fetchone()
        if not r or r[0] == "Read Later":
            continue  # the paper itself is not a book you read
        authors = [a[0] for a in meta.execute(
            "SELECT a.name FROM authors a JOIN books_authors_link l ON l.author=a.id WHERE l.book=?",
            (book_id,))]
        out.append({"kind": "book", "name": r[0], "creator": ", ".join(authors),
                    "date": synced[:10], "progress_pct": round(progress, 1),
                    "why": f"Kobo sync, {round(progress)}% read, last synced {synced[:10]}"})
    return out


def koreader_reading(cfg, now=None):
    """Books reported by the KOReader plugin (reading/state.json via paper_server.py).

    Every book here is marked spoiler_sensitive: curation must not pick articles that
    could reveal what happens past progress_pct."""
    import koreader_state
    out = []
    for b in koreader_state.currently_reading(READING_DAYS, now):
        pct = round((b.get("percent") or 0) * 100, 1)
        date = datetime.fromtimestamp(b["updated"], timezone.utc).date().isoformat()
        out.append({"kind": "book", "name": clean_title(b.get("title") or "Untitled"),
                    "creator": b.get("authors") or "", "date": date, "progress_pct": pct,
                    "chapter": b.get("chapter") or "", "spoiler_sensitive": True,
                    "why": f"KOReader, {round(pct)}% read, last read {date}"})
    return out


def abs_reading(now=None):
    if not CFG["audiobookshelf_container"]:
        raise RuntimeError("audiobookshelf_container not set (source skipped)")
    now = now or datetime.now(timezone.utc)
    js = (f"const s=require('sqlite3');const db=new s.Database('{CFG['audiobookshelf_db']}',s.OPEN_READONLY);"
          "db.all(\"select mp.currentTime t,mp.duration d,mp.isFinished f,mp.updatedAt u,b.title n "
          "from mediaProgresses mp join books b on b.id=mp.mediaItemId where mp.currentTime>60\","
          "(e,r)=>{if(e){console.error(e.message);process.exit(1)}console.log(JSON.stringify(r))});")
    p = subprocess.run(["docker", "exec", CFG["audiobookshelf_container"], "node", "-e", js],
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise RuntimeError("audiobookshelf read failed: " + p.stderr.strip()[:200])
    cutoff = (now - timedelta(days=READING_DAYS)).date().isoformat()
    meta = ro_connect(CALIBRE_META_DB)
    out = []
    for r in json.loads(p.stdout):
        if r["f"] or r["u"][:10] < cutoff:
            continue
        authors = ""
        m = meta.execute("SELECT id FROM books WHERE title=?", (r["n"],)).fetchone()
        if m:
            authors = ", ".join(a[0] for a in meta.execute(
                "SELECT a.name FROM authors a JOIN books_authors_link l ON l.author=a.id WHERE l.book=?",
                (m[0],)))
        pct = round(100 * r["t"] / r["d"], 1) if r["d"] else None
        out.append({"kind": "book", "name": r["n"], "creator": authors, "date": r["u"][:10],
                    "progress_pct": pct, "why": f"Audiobookshelf progress {pct}%, last played {r['u'][:10]}"})
    return out


def merge_books(*lists):
    seen = {}
    for lst in lists:
        for e in lst:
            key = e["name"].lower()
            if key in seen:
                seen[key]["why"] += "; " + e["why"].split(",")[0]
                seen[key]["date"] = max(seen[key]["date"], e["date"])
            else:
                seen[key] = dict(e)
    return sorted(seen.values(), key=lambda e: e["date"], reverse=True)


# --- own_media ----------------------------------------------------------------------
# Recent additions to your media folders. Owning something is a weak signal (a household
# library holds the kids' shows too), so only what arrived in the last media_recent_days counts,
# a few per folder, minus exclude_titles. Folder mtimes are useless (bulk renames and
# permission fixes touch them all), so an item is dated by the newest file inside it.

def excluded_title(title, cfg):
    low = title.lower()
    return any(x.lower() in low for x in cfg["exclude_titles"])


RELEASE_JUNK = re.compile(r"\b(?:2160p|1080p|720p|uhd|blu-?ray|remux|web-?dl|hdr\d*|dv|hevc|x26[45]|"
                          r"complete|repack|multi|unabridged|abridged)\b.*$", re.I)


def media_title(name, kind):
    """("Tombstone 1993 UHD BluRay 2160p ... REMUX-FraMeSToR", "movie") -> ("Tombstone", "1993", "").
    Audiobooks: "Author - Title.m4b" and "Title By Author (Unabridged).m4b" give the author."""
    stem = Path(name).stem if Path(name).suffix.lower() in MEDIA_EXT else name
    s = stem.replace("_ ", ": ").replace("_", " ")
    if s.count(".") >= 3 and " " not in s:
        s = s.replace(".", " ")
    s = re.sub(r"\s*[\[(][^\])]*(?:abridged|tunarr|season|s\d)[^\])]*[\])]", "", s, flags=re.I)
    year, creator = "", ""
    m = re.match(r"^(.*?)[\s(\[]+((?:19|20)\d\d)(?:[\s)\]]|$)", s)
    if m and m.group(1).strip():
        s, year = m.group(1), m.group(2)
    s = RELEASE_JUNK.sub("", s)
    if " AKA " in s:
        s = s.split(" AKA ")[-1]
    if kind == "audiobook":
        m = re.match(r"^(.+?)\s+[Bb]y\s+(.+)$", s)
        if m:
            s, creator = m.group(1), m.group(2)
        elif " - " in s:
            creator, s = s.split(" - ", 1)
    s = re.sub(r"\s+", " ", s).strip(" -.:")
    if kind == "audiobook" and ":" in s:
        s = s.split(":")[0].strip()
    return s, year, creator.strip()


def newest_file(path, now_ts, depth=3):
    """mtime of the newest media file at most `depth` levels down (future dates ignored)."""
    path = Path(path)
    if path.is_file():
        t = path.stat().st_mtime
        return t if t <= now_ts else 0
    best = 0
    base = len(path.parts)
    for root, dirs, files in os.walk(path):
        if len(Path(root).parts) - base >= depth:
            dirs[:] = []
        for f in files:
            if f.startswith(".") or Path(f).suffix.lower() not in MEDIA_EXT:
                continue
            try:
                t = os.stat(os.path.join(root, f)).st_mtime
            except OSError:
                continue
            if t <= now_ts:
                best = max(best, t)
    return best


def own_media(cfg, now=None):
    dirs = cfg.get("media_dirs") or {}
    if not dirs:
        raise RuntimeError("media_dirs not set (source skipped)")
    now = now or datetime.now(timezone.utc)
    cutoff = now.timestamp() - int(cfg["media_recent_days"]) * 86400
    out, missing = [], []
    for folder, kind in dirs.items():
        root = Path(folder)
        if not root.is_dir():
            missing.append(folder)
            continue
        items = []
        for e in root.iterdir():
            if e.name.startswith("."):
                continue
            t = newest_file(e, now.timestamp())
            if t >= cutoff:
                items.append((t, e.name))
        for t, name in sorted(items, reverse=True)[: int(cfg["media_max_per_dir"])]:
            title, year, creator = media_title(name, kind)
            if not title or excluded_title(title, cfg):
                continue
            date = datetime.fromtimestamp(t, timezone.utc).date().isoformat()
            label = {"movie": "movie", "tv": "TV show", "audiobook": "audiobook"}.get(kind, kind)
            e = {"kind": "title", "name": f"{title} ({year})" if year else title, "date": date,
                 "why": f"{label} added to your library {date}"}
            if creator:
                e["creator"] = creator
            out.append(e)
    if missing and not out:
        raise RuntimeError("media_dirs not found: " + ", ".join(missing))
    return dedupe(sorted(out, key=lambda e: e["date"], reverse=True))


def dedupe(entries):
    seen, out = set(), []
    for e in entries:
        k = (e["kind"], e["name"].lower())
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out


# --- topics ---------------------------------------------------------------------------

def topics(cfg):
    return [{"kind": "topic", "name": t, "date": today(), "why": "standing topic from config"}
            for t in cfg.get("topics", []) if t.strip()]


def book_creators(books):
    """Authors of the books you are reading: interviews, essays, adaptation news."""
    out = []
    for b in books:
        for a in filter(None, [x.strip() for x in b.get("creator", "").split(",")]):
            out.append({"kind": "creator", "name": a, "date": b["date"],
                        "why": f"author of '{b['name']}' you are reading: interviews, essays, adaptations"})
    return dedupe(out)


# --- tiktok -----------------------------------------------------------------------------

def tiktok_collection(url):
    p = subprocess.run([YTDLP, "--flat-playlist", "-J", "--no-warnings", url],
                       capture_output=True, text=True, timeout=120)
    if not p.stdout.strip():
        raise RuntimeError("yt-dlp: " + (p.stderr.strip().splitlines() or ["no output"])[-1][:160])
    data = json.loads(p.stdout)
    items = [e for e in data.get("entries", []) if e]
    if not items:
        why = (p.stderr.strip().splitlines() or ["empty listing"])[0][:160]
        raise RuntimeError("collection listing empty: " + why)
    name = data.get("title") or url
    return [{"kind": "recipe" if "recipe" in name.lower() else "title",
             "name": e.get("title") or e.get("id"), "date": today(),
             "why": f"public TikTok collection {name}"} for e in items[:40]]


def tiktok(cfg, lister=tiktok_collection):
    entries, errors = [], []
    if not cfg["tiktok_collections"]:
        return [], {"status": "failed", "reason": "no tiktok_collections configured"}
    for url in cfg["tiktok_collections"]:
        try:
            entries += lister(url)
        except Exception as e:
            errors.append(f"{url}: {e}")
    if errors and not entries:
        return [], {"status": "failed", "reason": " | ".join(errors)}
    st = {"status": "ok"}
    if errors:
        st["partial_errors"] = errors
    return entries, st


# --- kickstarter -----------------------------------------------------------------------

def kickstarter_projects():
    if not CFG["kickstarter_enabled"]:
        raise RuntimeError("kickstarter_enabled is false (source skipped)")
    env = {"user": os.environ.get("KOBO_IMAP_USER"), "password": os.environ.get("KOBO_IMAP_PASSWORD")}
    if not (env["user"] and env["password"]):
        raise RuntimeError("set KOBO_IMAP_USER and KOBO_IMAP_PASSWORD")
    M = imaplib.IMAP4_SSL(CFG["imap_host"])
    try:
        M.login(env["user"], env["password"])
        M.select('"%s"' % CFG["imap_folder"], readonly=True)
        t, d = M.search(None, "FROM", "kickstarter.com", "SUBJECT", '"You just backed"')
        out = []
        for i in d[0].split():
            t, m = M.fetch(i, "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE)])")
            msg = message_from_bytes(m[0][1])
            subj = str(make_header(decode_header(msg["Subject"]))).replace("\r\n", " ")
            name = re.sub(r"^You just backed\s+", "", subj)
            date = datetime.strptime(msg["Date"][:25].strip(), "%a, %d %b %Y %H:%M:%S").date().isoformat()
            out.append({"kind": "project", "name": re.split(r"\s[:|\-]\s|:\s|,\s", name)[0].strip(),
                        "date": date, "why": f"Kickstarter project you backed ({date}); updates only"})
        return sorted(out, key=lambda e: e["date"], reverse=True)
    finally:
        try:
            M.logout()
        except Exception:
            pass


# --- assembly ---------------------------------------------------------------------------

def base_weights(cfg):
    w = dict(BASE_WEIGHTS)
    w.update({k: float(v) for k, v in (cfg.get("interest_weights") or {}).items() if k in w})
    return w


def redistribute(weights, empty):
    """Give the weight of every empty input to the rest, in proportion; sums stay at 1."""
    w = dict(weights)
    for k in empty:
        w[k] = 0.0
    total = sum(w.values())
    return {k: round(v / total, 4) if total else 0.0 for k, v in w.items()}


def build(cfg, src):
    status = {}

    def run(name, fn, default):
        try:
            res = fn()
            status[name] = {"status": "ok"}
            return res
        except Exception as e:
            status[name] = {"status": "failed", "reason": str(e)[:300]}
            return default

    kobo = run("reading", src["kobo"], [])
    abook = run("audiobookshelf", src["abs"], [])
    books = merge_books(kobo, abook)
    tk_entries, status["tiktok"] = src["tiktok"]()

    input_status = {
        "books_reading": status["reading"] if status["reading"]["status"] == "failed"
        and status["audiobookshelf"]["status"] == "failed" else {"status": "ok"},
        "topics": {"status": "ok"},
        "own_media": None,
        "tiktok": status["tiktok"],
        "kickstarter": None,
    }
    inputs = {
        "books_reading": {"entries": books + book_creators(books)},
        "topics": {"entries": topics(cfg)},
        "own_media": {"entries": run("own_media", src["own_media"], [])},
        "tiktok": {"entries": tk_entries},
        "kickstarter": {"max_items": 1, "entries": run("kickstarter", src["kickstarter"], [])},
    }
    input_status["own_media"] = status["own_media"]
    input_status["kickstarter"] = status["kickstarter"]

    base = base_weights(cfg)
    empty = [k for k, v in inputs.items() if not v["entries"]]
    weights = redistribute(base, empty)
    notes = {}
    for k in empty:
        st = input_status[k]
        reason = st.get("reason") if st["status"] == "failed" else "no entries"
        notes[k] = f"{k}: {reason}; its {base[k]:.0%} went to the other inputs in proportion."
    weights.update({k: 0.0 for k in ZERO_WEIGHT})

    for k, v in inputs.items():
        v["weight"] = weights[k]
        v["status"] = input_status[k]
        v["count"] = len(v["entries"])
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "weights": weights,
        "weight_notes": notes,
        "inputs": inputs,
        "excluded_inputs": {k: "weight 0" for k in ZERO_WEIGHT},
        "always_included": [f"{DATA_DIR}/queue.txt (kobo: urls)", f"{DATA_DIR}/pinned.txt"],
        "rules": RULES + ([f"Avoid these subjects: {', '.join(cfg['avoid_topics'])}."]
                          if cfg.get("avoid_topics") else []) + list(cfg.get("extra_rules", [])),
    }


def main():
    cfg = load_config()
    src = {
        "kobo": (lambda: koreader_reading(cfg)) if cfg.get("delivery") == "koreader"
        else (lambda: kobo_reading(cfg)),
        "abs": abs_reading,
        "own_media": lambda: own_media(cfg),
        "tiktok": lambda: tiktok(cfg),
        "kickstarter": kickstarter_projects,
    }
    doc = build(cfg, src)
    write_atomic(OUT, doc)
    print(f"{datetime.now():%F %T} wrote {OUT}: " + ", ".join(
        f"{k}={v['count']}({v['status']['status']})" for k, v in doc["inputs"].items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
