#!/usr/bin/env python3
"""The recommendations page at the back of the paper: books, podcasts, movies & TV, blogs.

The LLM suggests titles from what you are reading, the podcasts and sources you follow, and
your topics. Models invent plausible titles, so every suggestion is checked against
a real catalogue before it is printed, and dropped if it cannot be found:
  books     Hardcover (with HARDCOVER_API_KEY), else Open Library, else Wikipedia
  podcasts  Apple's podcast directory
  film/TV   Wikipedia
  blogs     the site itself must answer
Past recommendations are remembered (recommendations.json) and never repeated.

config.json: "recommendations": {"enabled": true, "per_type": 3}
"""
import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kobo_config import CFG, DATA_DIR  # noqa: E402
import llm  # noqa: E402

HISTORY = DATA_DIR / "recommendations.json"
UA = {"User-Agent": "Mozilla/5.0 (compatible; morning-paper/1.0)"}
TYPES = ["books", "podcasts", "screen", "blogs"]
e = html.escape


def log(msg):
    print(f"{datetime.now():%F %T} recommend: {msg}", flush=True)


def norm(s: str) -> str:
    s = re.sub(r"\(.*?\)", "", (s or "").lower())
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"^(the|a|an) ", "", " ".join(s.split()))
    return s.split(" subtitle ")[0].strip()


def same_title(a: str, b: str) -> bool:
    """Same work, allowing a subtitle or a possessive prefix ("Dan Carlin's Hardcore History")."""
    x, y = norm(a.split(":")[0]), norm(b.split(":")[0])
    if not x or not y:
        return False
    short, long_ = sorted((x, y), key=len)
    return x == y or (len(short) >= 6 and f" {short} " in f" {long_} ")


# --- catalogue checks ------------------------------------------------------------------------

def wikipedia(title: str, hint: str) -> "dict | None":
    try:
        r = requests.get("https://en.wikipedia.org/w/api.php", headers=UA, timeout=20, params={
            "action": "query", "list": "search", "format": "json", "srlimit": 3,
            "srsearch": f"{title} {hint}"})
        for hit in r.json()["query"]["search"]:
            if same_title(hit["title"], title):
                s = requests.get("https://en.wikipedia.org/api/rest_v1/page/summary/"
                                 + quote(hit["title"].replace(" ", "_")), headers=UA, timeout=20).json()
                return {"title": s.get("title", hit["title"]), "desc": s.get("description", ""),
                        "extract": s.get("extract", "")}
    except (requests.RequestException, ValueError, KeyError):
        return None
    return None


def hardcover(rec: dict) -> "dict | None":
    """Hardcover's GraphQL search (needs HARDCOVER_API_KEY: Settings -> API on hardcover.app)."""
    import os
    key = os.environ.get("HARDCOVER_API_KEY", "").strip()
    if not key:
        return None
    if not key.lower().startswith("bearer "):
        key = f"Bearer {key}"
    q = ("query Find($q: String!) { search(query: $q, query_type: \"Book\", per_page: 5) { results } }")
    try:
        r = requests.post("https://api.hardcover.app/v1/graphql", timeout=20,
                          headers={**UA, "authorization": key, "content-type": "application/json"},
                          json={"query": q, "variables": {"q": f"{rec['title']} {rec.get('creator', '')}".strip()}})
        hits = (((r.json().get("data") or {}).get("search") or {}).get("results") or {}).get("hits", [])
    except (requests.RequestException, ValueError, AttributeError):
        return None
    for h in hits:
        d = h.get("document", {})
        if same_title(d.get("title", ""), rec["title"]):
            rating = d.get("rating")
            return {**rec, "title": d["title"], "creator": ", ".join((d.get("author_names") or [])[:2]) or rec.get("creator"),
                    "year": d.get("release_year") or rec.get("year"),
                    "detail": f"{rating:.1f}\u2605 on Hardcover" if isinstance(rating, (int, float)) and rating else ""}
    return None


def check_book(rec: dict) -> "dict | None":
    hc = hardcover(rec)
    if hc:
        return hc
    try:
        r = requests.get("https://openlibrary.org/search.json", headers=UA, timeout=20, params={
            "title": rec["title"], "author": rec.get("creator", ""), "limit": 3,
            "fields": "title,author_name,first_publish_year"})
        if r.status_code == 200:
            for d in r.json().get("docs", []):
                if same_title(d.get("title", ""), rec["title"]):
                    return {**rec, "title": d["title"], "creator": ", ".join(d.get("author_name", [])[:2]) or rec.get("creator"),
                            "year": d.get("first_publish_year")}
    except (requests.RequestException, ValueError):
        pass
    w = wikipedia(rec["title"], f"{rec.get('creator', '')} book")
    if w and re.search(r"novel|book|memoir|non-?fiction|collection|series|trilogy", f"{w['desc']} {w['extract'][:300]}", re.I):
        return {**rec, "title": w["title"].split(" (")[0], "detail": w["desc"]}
    return None


def check_podcast(rec: dict) -> "dict | None":
    try:
        r = requests.get("https://itunes.apple.com/search", headers=UA, timeout=20,
                         params={"media": "podcast", "entity": "podcast", "limit": 5, "term": rec["title"]})
        for d in r.json().get("results", []):
            if same_title(d.get("collectionName", ""), rec["title"]):
                return {**rec, "title": d["collectionName"], "creator": d.get("artistName", rec.get("creator"))}
    except (requests.RequestException, ValueError):
        return None
    return None


def check_screen(rec: dict) -> "dict | None":
    hint = "TV series" if rec.get("kind") == "tv" else f"{rec.get('year') or ''} film"
    w = wikipedia(rec["title"], hint)
    if w and re.search(r"film|series|television|miniseries|documentary|show", w["desc"], re.I):
        return {**rec, "title": w["title"].split(" (")[0], "detail": w["desc"]}
    return None


def check_blog(rec: dict) -> "dict | None":
    url = rec.get("url", "")
    if not url.startswith("http"):
        return None
    try:
        r = requests.get(url, headers=UA, timeout=20)
        return {**rec, "url": r.url} if r.status_code == 200 and "html" in r.headers.get("content-type", "") else None
    except requests.RequestException:
        return None


CHECKS = {"books": check_book, "podcasts": check_podcast, "screen": check_screen, "blogs": check_blog}


# --- LLM ------------------------------------------------------------------------------------------

REC = {"type": "object", "properties": {
    "title": {"type": "string"}, "creator": {"type": "string"}, "year": {"type": "string"},
    "kind": {"type": "string", "enum": ["book", "podcast", "movie", "tv", "blog"]},
    "url": {"type": "string"}, "why": {"type": "string"}},
    "required": ["title", "creator", "year", "kind", "url", "why"], "additionalProperties": False}
SCHEMA = {"type": "object", "properties": {t: {"type": "array", "items": REC} for t in TYPES},
          "required": TYPES, "additionalProperties": False}


def profile() -> str:
    lines = []
    try:
        interests = json.loads((DATA_DIR / "interests.json").read_text())
    except (OSError, ValueError):
        interests = {}
    for name, inp in interests.get("inputs", {}).items():
        for x in inp.get("entries", [])[:10]:
            extra = f" ({x['progress_pct']}% read)" if x.get("progress_pct") is not None else ""
            lines.append(f"- {name}: {x.get('kind')}: {x.get('name')}"
                         f"{' by ' + x['creator'] if x.get('creator') else ''}{extra}")
    if CFG.get("podcasts"):
        lines.append("- podcasts they listen to: " + ", ".join(CFG["podcasts"]))
    if CFG.get("feeds") or CFG.get("blog_pages"):
        lines.append("- sites they follow: " + ", ".join(
            urlparse(u).netloc.removeprefix("www.") for u in CFG.get("feeds", []) + CFG.get("blog_pages", [])))
    try:
        recent = [json.loads(ln).get("title", "") for ln in (DATA_DIR / "seen.jsonl").read_text().splitlines()[-25:]]
        lines.append("- articles they were recently given: " + "; ".join(t for t in recent if t))
    except (OSError, ValueError):
        pass
    return "\n".join(lines)


def ask(history: list[str], n: int) -> dict:
    prompt = (
        f"Recommend things for one reader, {n + 2} in each category, matched to the profile below. "
        "Only real, existing works you are sure of: exact titles and creators. Mix well-known and "
        "lesser-known picks. `why` is one sentence tying the pick to something specific in the "
        "profile ('Because you're reading ...').\n"
        "books: books (fiction and nonfiction) to read next. NEVER a sequel, prequel or companion "
        "of a book they are reading now, and nothing about those books: that would spoil them.\n"
        "podcasts: podcast shows (not single episodes) they don't already listen to.\n"
        "screen: movies or TV series (kind movie or tv, year = release year).\n"
        "blogs: blogs, newsletters or magazines worth following, with the site's real home-page URL; "
        "not sites they already follow.\n"
        "Use empty strings for fields that do not apply.\n\n"
        f"Profile:\n{profile()}\n\n"
        f"Already recommended before (do not repeat):\n{'; '.join(history[-80:]) or '(none)'}")
    # no thinking: on a list task it can spend the whole token budget before writing the answer
    return llm.chat_json([{"role": "user", "content": prompt}], SCHEMA, task="write", think=False,
                         max_tokens=8000, temperature=0.8)


# --- render & main ----------------------------------------------------------------------------------

HEADINGS = {"books": "Books", "podcasts": "Podcasts", "screen": "Movies &amp; TV", "blogs": "Blogs &amp; sites to follow"}


def in_library(title: str) -> bool:
    try:
        import koreader_state as ks
        return any(same_title(b["title"], title) for b in ks.library_books())
    except Exception:
        return False


def render(picks: dict) -> str:
    out = []
    for t in TYPES:
        if not picks.get(t):
            continue
        out.append(f"<h2>{HEADINGS[t]}</h2>")
        for r in picks[t]:
            year = re.search(r"\b(1[5-9]|20)\d\d\b", str(r.get("year") or ""))
            creators = list(dict.fromkeys(c.strip() for c in str(r.get("creator") or "").split(",") if c.strip()))
            meta = " &middot; ".join(e(x) for x in (", ".join(creators), year.group(0) if year else "",
                                                    r.get("detail") or "") if x)
            site = f" &middot; {e(urlparse(r['url']).netloc.removeprefix('www.'))}" if t == "blogs" else ""
            owned = " <em>(already in your library)</em>" if r.get("owned") else ""
            out.append(f"<p><b>{e(r['title'])}</b>{owned}<br/><span style='color:#555'>{meta}{site}</span>"
                       f"<br/>{e(r.get('why', ''))}</p>")
    return "".join(out)


def main() -> int:
    opts = {"enabled": True, "per_type": 3, **(CFG.get("recommendations") or {})}
    if not opts["enabled"]:
        return 0
    if not llm.available():
        log("LLM unavailable; no recommendations today")
        return 0
    try:
        history = json.loads(HISTORY.read_text())
    except (OSError, ValueError):
        history = []
    n = int(opts["per_type"])
    try:
        raw = ask(history, n)
    except llm.LLMError as ex:
        log(f"no recommendations today: {ex}")
        return 0
    # The model sometimes files a pick under the wrong heading: route each by its own `kind`.
    by_kind = {"book": "books", "podcast": "podcasts", "movie": "screen", "tv": "screen", "blog": "blogs"}
    known = history + list(CFG.get("podcasts", []))
    picks = {t: [] for t in TYPES}
    for listed in TYPES:
        for rec in raw.get(listed, []):
            t = by_kind.get(rec.get("kind"), listed)
            if len(picks[t]) >= n or not rec.get("title") or any(same_title(rec["title"], h) for h in known):
                continue
            ok = CHECKS[t](rec)
            if ok:
                if t == "books":
                    ok["owned"] = in_library(ok["title"])
                picks[t].append(ok)
                known.append(ok["title"])
            else:
                log(f"dropped unverifiable {rec.get('kind') or t}: {rec['title']}")
    if not any(picks.values()):
        return 0
    history += [r["title"] for t in TYPES for r in picks[t]]
    HISTORY.write_text(json.dumps(history[-500:], indent=1, ensure_ascii=False))
    now = datetime.now(timezone.utc)
    day = now.date().isoformat()
    art = {"url": f"morning-paper://recommendations/{day}", "title": "Recommendations",
           "domain": "Picked for you", "published": day, "saved": now.isoformat(), "image": None,
           "pin_bottom": True, "pin_order": 1, "html": render(picks)}
    (DATA_DIR / "articles").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "articles" / f"recommendations-{day}.json").write_text(json.dumps(art, ensure_ascii=False))
    log("wrote recommendations: " + ", ".join(f"{t} {len(picks[t])}" for t in TYPES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
