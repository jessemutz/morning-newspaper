#!/usr/bin/env python3
"""Collect today's candidate articles into candidates.jsonl (curate.py picks from these).

Sources (each optional, each failure isolated):
  feeds        RSS/Atom URLs from config
  blog_pages   sites without a known feed: discover their feed, else scrape article-like links
  podcasts     podcast RSS: recent episodes (transcript URL kept when the feed publishes one)
  web search   the LLM writes queries from interests.json -> Kagi API, falling back to SearXNG

Every candidate: {url, title, snippet, source, kind, published, query?}.
Already-shipped URLs (seen.jsonl) and anything older than max_age_days are dropped.
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kobo_config import CFG, DATA_DIR  # noqa: E402
import llm  # noqa: E402

OUT = DATA_DIR / "candidates.jsonl"
SEEN = DATA_DIR / "seen.jsonl"
PREBUILT = DATA_DIR / "prebuilt"   # articles made from feed content (podcasts); the build uses them as-is
INTERESTS = DATA_DIR / "interests.json"
UA = {"User-Agent": "Mozilla/5.0 (compatible; morning-paper/1.0)"}
TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ref_src|cmpid|smid)", re.I)


def log(msg):
    print(f"{datetime.now():%F %T} gather: {msg}", flush=True)


def normalize(url: str) -> str:
    u = urlparse(url.strip())
    q = [(k, v) for k, v in parse_qsl(u.query) if not TRACKING.match(k)]
    host = u.netloc.lower().removeprefix("www.")
    return urlunparse((u.scheme.lower() or "https", host, u.path.rstrip("/") or "/", "", urlencode(q), ""))


def strip_html(s: str, n: int = 400) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", s or "")).split())[:n]


def seen_urls() -> set:
    out = set()
    if SEEN.exists():
        for ln in SEEN.read_text().splitlines():
            try:
                out.add(normalize(json.loads(ln)["url"]))
            except (ValueError, KeyError):
                continue
    return out


def _date(entry) -> "str | None":
    for k in ("published_parsed", "updated_parsed"):
        t = entry.get(k)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc).date().isoformat()
    return None


# --- feeds / podcasts -------------------------------------------------------------------

def from_feed(url: str, kind: str = "feed", limit: "int | None" = None) -> list[dict]:
    import feedparser
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    fp = feedparser.parse(r.content)
    name = fp.feed.get("title") or urlparse(url).netloc
    feed_image = (fp.feed.get("image") or {}).get("href")
    out = []
    for e in fp.entries[: limit or CFG["candidates_per_feed"]]:
        link = e.get("link")
        if not link:
            continue
        c = {"url": link, "title": strip_html(e.get("title", ""), 200), "source": name, "kind": kind,
             "snippet": strip_html(e.get("summary", "")), "published": _date(e)}
        if kind == "podcast":
            c = podcast_episode(e, c, name, feed_image)
        out.append(c)
    return out


def podcast_episode(e, c: dict, show: str, show_image: "str | None" = None) -> dict:
    """Podcast items usually link to the show's home page, not the episode. Give each episode
    its own URL (its audio file) and prebuild its article from the feed's show notes, so the
    build never fetches the home page in its place."""
    import hashlib
    audio = next((l.get("href") for l in e.get("links", []) if l.get("rel") == "enclosure"), None)
    url = audio or e.get("id") or c["url"]
    notes = (e.get("content") or [{}])[0].get("value") or e.get("summary", "")
    dur = e.get("itunes_duration", "")
    listen = f'<p><a href="{audio}">Listen to the episode</a>{f" ({dur})" if dur else ""}</p>' if audio else ""
    art = {"url": url, "title": f"{show}: {c['title']}", "domain": show,
           "published": c["published"] or datetime.now(timezone.utc).date().isoformat(),
           "saved": datetime.now(timezone.utc).isoformat(),
           "image": (e.get("image") or {}).get("href") or show_image,
           "html": f"<p><em>Podcast episode</em></p>{notes}{listen}"}
    PREBUILT.mkdir(parents=True, exist_ok=True)
    (PREBUILT / f"{hashlib.sha1(url.encode()).hexdigest()[:16]}.json").write_text(json.dumps(art))
    return {**c, "url": url, "title": art["title"]}


def podcast_feed(entry: str) -> str:
    """A podcast entry is an RSS URL or a show name. Names are looked up once in Apple's
    public podcast directory and cached in podcast_feeds.json."""
    if entry.startswith(("http://", "https://")):
        return entry
    cache_file = DATA_DIR / "podcast_feeds.json"
    try:
        cache = json.loads(cache_file.read_text())
    except (OSError, ValueError):
        cache = {}
    if entry not in cache:
        r = requests.get("https://itunes.apple.com/search",
                         params={"media": "podcast", "entity": "podcast", "limit": 1, "term": entry},
                         headers=UA, timeout=30)
        r.raise_for_status()
        hits = [h for h in r.json().get("results", []) if h.get("feedUrl")]
        if not hits:
            raise RuntimeError(f"no podcast found named {entry!r}; put its RSS URL in config")
        cache[entry] = hits[0]["feedUrl"]
        log(f"podcast {entry!r} -> {hits[0].get('collectionName')} {cache[entry]}")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache, indent=2))
    return cache[entry]


# --- blogs without a known feed ------------------------------------------------------------

ARTICLE_PATH = re.compile(r"/(19|20)\d\d/|/[a-z0-9]+(-[a-z0-9]+){3,}/?$", re.I)


def from_blog(url: str) -> list[dict]:
    from trafilatura.feeds import find_feed_urls
    links = find_feed_urls(url) or []
    if links:
        return [{"url": u, "title": "", "source": urlparse(url).netloc, "kind": "blog",
                 "snippet": "", "published": None} for u in links[: CFG["candidates_per_feed"]]]
    # No feed: take same-site links that look like articles, with their anchor text.
    html = requests.get(url, headers=UA, timeout=30).text
    host = urlparse(url).netloc
    out, seen = [], set()
    for href, text in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.S | re.I):
        full = urljoin(url, href)
        title = strip_html(text, 200)
        if urlparse(full).netloc != host or not ARTICLE_PATH.search(urlparse(full).path) or len(title) < 15:
            continue
        if full not in seen:
            seen.add(full)
            out.append({"url": full, "title": title, "source": host, "kind": "blog",
                        "snippet": "", "published": None})
    return out[: CFG["candidates_per_feed"]]


# --- web search -----------------------------------------------------------------------------

def kagi(query: str, n: int) -> list[dict]:
    key = os.environ.get("KAGI_API_KEY")
    if not key or not CFG["kagi_enabled"]:
        raise RuntimeError("Kagi not configured")
    r = requests.get("https://kagi.com/api/v0/search", params={"q": query, "limit": n},
                     headers={"Authorization": f"Bot {key}"}, timeout=30)
    r.raise_for_status()
    return [{"url": d["url"], "title": d.get("title", ""), "snippet": strip_html(d.get("snippet", "")),
             "published": (d.get("published") or "")[:10] or None}
            for d in r.json().get("data", []) if d.get("t") == 0 and d.get("url")]


def searxng(query: str, n: int) -> list[dict]:
    base = CFG["searxng_url"].rstrip("/")
    if not base:
        raise RuntimeError("SearXNG not configured")
    r = requests.get(f"{base}/search", params={"q": query, "format": "json", "time_range": "week"},
                     headers=UA, timeout=30)
    r.raise_for_status()
    return [{"url": d["url"], "title": d.get("title", ""), "snippet": strip_html(d.get("content", "")),
             "published": (d.get("publishedDate") or "")[:10] or None}
            for d in r.json().get("results", [])[:n] if d.get("url")]


INDEX_ENDINGS = {"news", "latest", "articles", "stories", "features", "reviews", "videos", "podcasts"}
LISTING_SEGMENTS = {"tag", "tags", "category", "categories", "topic", "topics", "section", "sections",
                    "author", "authors", "search", "page", "archive", "archives", "collections", "shop",
                    "products", "store", "genre", "genres", "label", "labels", "hub", "browse"}


def is_listing(url: str) -> bool:
    """Homepages, tag/category/section pages and other indexes: not articles.
    An article URL nearly always has a slug with hyphens or an id with digits."""
    segs = [s for s in urlparse(url).path.lower().split("/") if s]
    if not segs or segs[-1] in ("index.html", "index.php", "home"):
        return True
    if segs[0] in LISTING_SEGMENTS and len(segs) <= 2:  # /tag/science-fiction, /section/books
        return True
    if segs[-1] in INDEX_ENDINGS:  # imdb.com/title/tt0458290/news/
        return True
    slugless = not any("-" in x or any(ch.isdigit() for ch in x) for x in segs)
    if segs[0] in ("news", "latest") and slugless:  # sciencedaily.com/news/health_medicine/mental_health/
        return True
    return len(segs) <= 2 and not any("-" in s or "_" in s or any(ch.isdigit() for ch in s) for s in segs)


QUERY_SCHEMA = {"type": "object", "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
                "required": ["queries"], "additionalProperties": False}


def interest_summary(interests: dict, max_entries: int = 8) -> str:
    lines = []
    for name, inp in interests.get("inputs", {}).items():
        if not inp.get("entries"):
            continue
        lines.append(f"## {name} (weight {inp.get('weight', 0):.0%})")
        for e in inp["entries"][:max_entries]:
            extra = f" [spoiler-sensitive: reader is {e['progress_pct']}% through]" if e.get("spoiler_sensitive") else ""
            lines.append(f"- {e.get('kind')}: {e.get('name')}"
                         f"{' by ' + e['creator'] if e.get('creator') else ''}{extra}")
    return "\n".join(lines)


def write_queries(interests: dict) -> list[str]:
    n = int(CFG["search_queries_max"])
    if llm.available():
        prompt = (
            f"Today is {datetime.now():%A %B %-d, %Y}. Write {n} web search queries that would find "
            "fresh (this week), high-quality articles for a personal morning newspaper, matching the "
            "reader's interests below in proportion to their weights. Every topic gets at least one "
            "query when there are enough. Write each query the way a good article on it would be "
            "titled or described, with a concrete angle (a new study, an interview, an essay, a "
            "practical method, a named person or book), not a bare subject: 'new study ADHD adults "
            "sleep' rather than 'ADHD'. Vary the angles across days; skip shopping, rankings and "
            "listicle phrasing. Prefer specific names over generic topics. For spoiler-sensitive "
            "books, only search for the author, adaptations or craft/context, never the plot or "
            "ending.\n\n" + interest_summary(interests))
        try:
            out = llm.chat_json([{"role": "user", "content": prompt}], QUERY_SCHEMA, task="rank")
            qs = [q.strip() for q in out.get("queries", []) if q.strip()]
            if qs:
                return qs[:n]
        except llm.LLMError as e:
            log(f"query writing failed ({e}); using entry names")
    # No LLM: search the top named entries directly.
    names = [e["name"] for inp in interests.get("inputs", {}).values()
             for e in inp.get("entries", [])[:3] if e.get("kind") != "book"]
    return [f"{x} news" for x in names][:n]


def from_search(interests: dict) -> list[dict]:
    if not ((os.environ.get("KAGI_API_KEY") and CFG["kagi_enabled"]) or CFG["searxng_url"]):
        log("web search skipped: set KAGI_API_KEY and/or searxng_url")
        return []
    out = []
    per = int(CFG["search_results_per_query"])
    for q in write_queries(interests):
        res = None
        for engine in (kagi, searxng):
            try:
                res = engine(q, per)
                break
            except Exception as e:
                log(f"{engine.__name__} failed for {q!r}: {e}")
        for d in res or []:
            if is_listing(d["url"]):
                continue
            out.append({**d, "source": urlparse(d["url"]).netloc.removeprefix("www."),
                        "kind": "search", "query": q})
    return out


# --- main -----------------------------------------------------------------------------------

def gather(interests: dict, now: "datetime | None" = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=int(CFG["max_age_days"]))).date().isoformat()
    jobs = [(f"feed {u}", lambda u=u: from_feed(u)) for u in CFG["feeds"]]
    jobs += [(f"blog {u}", lambda u=u: from_blog(u)) for u in CFG["blog_pages"]]
    jobs += [(f"podcast {u}", lambda u=u: from_feed(podcast_feed(u), "podcast", 3)) for u in CFG["podcasts"]]
    jobs += [("web search", lambda: from_search(interests))]
    seen, out, keys = seen_urls(), [], set()
    for label, fn in jobs:
        try:
            items = fn()
        except Exception as e:
            log(f"{label} failed: {type(e).__name__}: {str(e)[:150]}")
            continue
        kept = 0
        for c in items:
            k = normalize(c["url"])
            if k in seen or k in keys or (c.get("published") and c["published"] < cutoff):
                continue
            keys.add(k)
            out.append(c)
            kept += 1
        log(f"{label}: {kept}/{len(items)} kept")
    return out


def main() -> int:
    try:
        interests = json.loads(INTERESTS.read_text())
    except (OSError, ValueError):
        log(f"no {INTERESTS}; run kobo-interests.py first. Continuing with feeds only.")
        interests = {}
    cands = gather(interests)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUT.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cands))
    log(f"wrote {len(cands)} candidates -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
