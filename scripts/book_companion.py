#!/usr/bin/env python3
"""Reading companion: spoiler-safe pages that help you follow and remember the books you read.

For each book KOReader reported progress on since the last run, one "Previously in ..." page:
  * Previously: a recap of what you read since the last edition (the first time: the story so far)
  * Your recent sessions: a short log of your last few sessions, with pages and minutes
  * Key moments / key ideas, people, places & terms, open threads / questions to consider
  * Passages worth remembering: verbatim quotes, checked against the book text
  * Test your recall: a few questions, answers at the end of the page
  * About the author: from Wikipedia (not the model's memory)
  * The story so far: the running summary
and, when web search is configured, up to two "Behind the book" articles a day: interviews,
the author on the book's inspiration, craft essays. Every one is fetched and read by the LLM
alongside the story so far, and dropped if it could reveal anything you have not read.

Spoiler safety: the LLM only ever sees the book's text up to just before your position, and is
told to use nothing else. Facts about the author come from Wikipedia, and outside articles
must pass the spoiler check.

On a day without a fresh recap (no new reading, or the LLM is down) the book you are reading still
gets a page: progress, pages read since the last recap, where you left off, who's who and the
story so far from the last companion. Every edition has one.

State per book: reading/companion/<md5>.json.
"""
import hashlib
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
import koreader_state as ks  # noqa: E402
import llm  # noqa: E402

COMPANION = DATA_DIR / "reading" / "companion"
ARTICLES = DATA_DIR / "articles"
MIN_NEW_PCT = 0.002          # skip books with less than ~0.2% new reading
SESSIONS_SHOWN = 4
BEHIND_PER_DAY = 2
BEHIND_SEARCH_EVERY_DAYS = 7
UA = {"User-Agent": "Mozilla/5.0 (compatible; morning-paper/1.0)"}
# Sites whose business is plot: summaries, study guides, fan wikis, discussion threads.
SPOILER_SITES = ("sparknotes.", "litcharts.", "cliffsnotes.", "gradesaver.", "supersummary.",
                 "bookey.", "shmoop.", "fandom.com", "wikipedia.org", "reddit.com", "goodreads.com",
                 "bookbrowse.", "enotes.", "coursehero.", "tvtropes.", "quora.com")


def log(msg):
    print(f"{datetime.now():%F %T} companion: {msg}", flush=True)


# --- book text -----------------------------------------------------------------------------

def book_text(path: Path) -> str:
    """Plain text of the EPUB in reading (spine) order, paragraphs separated by blank lines."""
    import ebooklib
    from ebooklib import epub
    from lxml import html as lh
    book = epub.read_epub(str(path), options={"ignore_ncx": True})
    parts = []
    for idref, _linear in book.spine:
        item = book.get_item_with_id(idref)
        if not item or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        try:
            tree = lh.fromstring(item.get_content())
        except Exception:
            continue
        for bad in tree.xpath("//script|//style"):
            bad.drop_tree()
        blocks = [" ".join(el.text_content().split())
                  for el in tree.xpath("//p|//h1|//h2|//h3|//h4|//li|//blockquote")]
        parts.append("\n\n".join(b for b in blocks if b))
    return "\n\n".join(p for p in parts if p)


def slice_text(text: str, start_pct: float, end_pct: float) -> str:
    """Text between two reading positions (0..1), snapped to paragraph boundaries."""
    n = len(text)
    a, b = int(max(0.0, start_pct) * n), int(min(1.0, end_pct) * n)
    if b <= a:
        return ""
    if a > 0:
        nxt = text.find("\n\n", a)
        a = nxt + 2 if 0 <= nxt < b else a
    prev = text.rfind("\n\n", a, b)
    b = prev if prev > a else b  # never include a partial paragraph past the cut
    return text[a:b].strip()


def chunks(text: str, size: int) -> list[str]:
    out, cur = [], 0
    while cur < len(text):
        end = min(len(text), cur + size)
        if end < len(text):
            brk = text.rfind("\n\n", cur, end)
            end = brk if brk > cur else end
        out.append(text[cur:end])
        cur = end
    return out


def _norm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("—", "-").replace("–", "-").replace("…", "...")
    return " ".join(s.lower().split())


def _closest_passage(quote: str, source: str) -> "str | None":
    """The run of 1-3 sentences in `source` that best matches a misremembered quote, if close."""
    from difflib import SequenceMatcher
    sents = re.split(r'(?<=[.!?”"])\s+', source)
    target = _norm(quote)
    best, best_r = None, 0.0
    for i in range(len(sents)):
        for k in (1, 2, 3):
            cand = " ".join(sents[i:i + k])
            nc = _norm(cand)
            if not (0.6 * len(target) <= len(nc) <= 1.6 * len(target)):
                continue
            sm = SequenceMatcher(None, target, nc)
            if sm.real_quick_ratio() < 0.8 or sm.quick_ratio() < 0.8:
                continue
            r = sm.ratio()
            if r > best_r:
                best, best_r = cand, r
    return best.strip() if best and best_r >= 0.85 else None


def verified_quotes(quotes: list[dict], source: str, near: str = "") -> list[dict]:
    """Quotes as they really appear in the book. Models misquote: an exact match is kept, a
    close paraphrase of a real passage (searched in `near`, the new text) is replaced by the
    passage itself, anything else is dropped."""
    hay = _norm(source)
    out = []
    for q in quotes:
        text = q.get("quote", "").strip().strip('"“”')
        if len(text) < 20:
            continue
        if _norm(text) in hay:
            out.append({**q, "quote": text})
        elif near and (real := _closest_passage(text, near)):
            out.append({**q, "quote": real.strip('"“”')})
    return out


# --- LLM -------------------------------------------------------------------------------------

GROUND_RULES = (
    "Use ONLY the book text provided here and the running summary. Do not use anything you may "
    "know about this book, its sequels or adaptations: the reader has not read further, and "
    "anything beyond the provided text is a spoiler. Do not foreshadow, hint or speculate about "
    "what happens next.")

FOLD_SCHEMA = {"type": "object", "properties": {"story_so_far": {"type": "string"}},
               "required": ["story_so_far"], "additionalProperties": False}

ITEM = {"type": "object", "properties": {"name": {"type": "string"}, "note": {"type": "string"}},
        "required": ["name", "note"], "additionalProperties": False}
STRS = {"type": "array", "items": {"type": "string"}}
COMPANION_SCHEMA = {"type": "object", "properties": {
    "kind": {"type": "string", "enum": ["fiction", "nonfiction"]},
    "recap": STRS,
    "session_summary": {"type": "string"},
    "story_so_far": {"type": "string"},
    "key_points": STRS,
    "people": {"type": "array", "items": ITEM},
    "terms": {"type": "array", "items": ITEM},
    "threads": STRS,
    "quotes": {"type": "array", "items": {"type": "object", "properties": {
        "quote": {"type": "string"}, "note": {"type": "string"}},
        "required": ["quote", "note"], "additionalProperties": False}},
    "recall": {"type": "array", "items": {"type": "object", "properties": {
        "q": {"type": "string"}, "a": {"type": "string"}},
        "required": ["q", "a"], "additionalProperties": False}},
    "tips": STRS,
}, "required": ["kind", "recap", "session_summary", "story_so_far", "key_points", "people", "terms",
                "threads", "quotes", "recall", "tips"], "additionalProperties": False}


def fold(summary: str, text: str, title: str) -> str:
    """Extend the running summary with one chunk of text (used for long catch-ups)."""
    prompt = (f"{GROUND_RULES}\n\nBook: {title}\n\nRunning summary so far:\n{summary or '(start of book)'}\n\n"
              f"Next part of the book:\n{text}\n\nReturn the updated running summary of the WHOLE "
              "book so far (at most 800 words, past tense; for nonfiction: the argument and key ideas).")
    out = llm.chat_json([{"role": "user", "content": prompt}], FOLD_SCHEMA, task="write",
                        think=False, max_tokens=4096)
    return out["story_so_far"]


def write_companion(title: str, authors: str, chapter: str, prior: str, new_text: str, first: bool) -> dict:
    what = ("everything the reader has read so far (the running summary plus the text below)"
            if first else "what the reader read since the last edition (the text below)")
    prompt = (
        f"{GROUND_RULES}\n\nYou write the reading-companion page of a reader's morning paper for "
        f"'{title}' by {authors or 'unknown'}. The reader is now at: {chapter or 'unknown chapter'}. "
        "The reader wants to follow the book closely and remember it better. Work out whether it "
        "is fiction or nonfiction and write accordingly.\n\n"
        f"recap: 2-5 short paragraphs recapping {what}. Concrete and vivid, no padding.\n"
        "session_summary: the same in 2-3 sentences, for the reader's session log.\n"
        "story_so_far: the running summary of the WHOLE book so far, updated with the new text "
        "(at most 800 words; nonfiction: the argument and key ideas so far).\n"
        "key_points: 3-6 bullets. Fiction: the key moments in the new text. Nonfiction: the main "
        "ideas, claims and evidence in the new text, each in one clear sentence.\n"
        "people: characters (or for nonfiction: people and thinkers) in the new text, one line each "
        "on who they are so far.\n"
        "terms: places, objects, concepts or unfamiliar words worth a gloss (may be empty).\n"
        "threads: fiction: open questions the text itself has raised, phrased as the text left "
        "them, no guesses. Nonfiction: 2-3 questions worth thinking about.\n"
        "quotes: 3-5 of the best or most important passages from the new text, copied EXACTLY "
        "word for word (1-3 sentences each), each with a one-line note on why it matters.\n"
        "recall: 3-5 questions that test memory and understanding of the new text, with short "
        "answers.\n"
        "tips: 1-3 tips for the next session, based only on what has been read (a motif, a "
        "narrator quirk, a timeline to track, an argument to test).\n\n"
        f"Running summary so far:\n{prior or '(start of book)'}\n\n"
        f"New text:\n{new_text}")
    return llm.chat_json([{"role": "user", "content": prompt}], COMPANION_SCHEMA, task="write",
                         think=True, max_tokens=16000, temperature=0.5)


# --- about the author (Wikipedia) ---------------------------------------------------------------

WRITERLY = re.compile(r"writer|author|novelist|poet|journalist|historian|essayist|scholar|"
                      r"professor|theologian|philosopher|scientist|psychologist|editor", re.I)


def author_bio(authors: str) -> "dict | None":
    """Wikipedia's summary of the (first) author: real facts, no plot. None if not found."""
    name = (authors or "").split(",")[0].split("&")[0].strip()
    if not name:
        return None
    def summary(page):
        r = requests.get("https://en.wikipedia.org/api/rest_v1/page/summary/" + quote(page.replace(" ", "_")),
                         headers=UA, timeout=20)
        return r.json() if r.status_code == 200 else None

    try:
        d = summary(name)
        if not d:  # name written differently on Wikipedia ("M.L. Wang", middle names...)
            r = requests.get("https://en.wikipedia.org/w/api.php", headers=UA, timeout=20, params={
                "action": "query", "list": "search", "format": "json", "srlimit": 3,
                "srsearch": f"{name} author"})
            surname = name.split()[-1].lower()
            hit = next((x["title"] for x in r.json()["query"]["search"]
                        if surname in x["title"].lower()), None)
            d = summary(hit) if hit else None
        if not d:
            return None
    except (requests.RequestException, ValueError, KeyError):
        return None
    if d.get("type") == "disambiguation" or not WRITERLY.search(f"{d.get('description', '')} {d.get('extract', '')}"):
        return None
    return {"text": d.get("extract", ""), "url": d.get("content_urls", {}).get("desktop", {}).get("page", "")}


# --- behind the book (web search + spoiler check) ---------------------------------------------

SPOILER_SCHEMA = {"type": "object", "properties": {
    "safe": {"type": "boolean"}, "worth_reading": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["reason", "safe", "worth_reading"], "additionalProperties": False}


def behind_queries(title: str, authors: str) -> list[str]:
    t = title.split(":")[0].strip()
    a = (authors or "").split(",")[0].strip()
    return [f'"{a}" interview "{t}"', f'"{t}" {a} inspiration behind the book',
            f'{a} on writing "{t}"', f'"{t}" {a} author essay']


def search_behind(title: str, authors: str) -> list[dict]:
    try:
        from gather_candidates import kagi, searxng
    except ImportError:
        return []
    out, seen = [], set()
    for q in behind_queries(title, authors):
        res = None
        for engine in (kagi, searxng):
            try:
                res = engine(q, 6)
                break
            except Exception:
                continue
        for d in res or []:
            u = d["url"]
            host = urlparse(u).netloc.lower()
            if u in seen or any(s in host for s in SPOILER_SITES):
                continue
            seen.add(u)
            out.append({"url": u, "title": d.get("title", ""), "snippet": d.get("snippet", "")})
    return out


def fetch_article(url: str) -> "dict | None":
    import trafilatura
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        if r.status_code != 200:
            return None
    except requests.RequestException:
        return None
    body = trafilatura.extract(r.text, output_format="html", include_images=True, include_links=False)
    if not body:
        return None
    meta = trafilatura.extract_metadata(r.text)
    text = trafilatura.extract(r.text) or ""
    return {"html": body, "text": text, "title": (getattr(meta, "title", None) or url)[:200],
            "image": getattr(meta, "image", None), "date": getattr(meta, "date", None)}


def spoiler_check(art: dict, title: str, authors: str, pct: float, known: str) -> "tuple[bool, str]":
    prompt = (
        f"A reader is {pct * 100:.0f}% through '{title}' by {authors}. This is everything they know "
        f"about it so far:\n{known[:6000]}\n\n"
        "Below is an article about the book or its author. safe=true ONLY if reading it could not "
        "reveal anything beyond what the reader already knows: no later plot events, twists, deaths, "
        "reveals, the ending, or the book's later arguments or conclusions. Hints and 'without "
        "spoiling anything...' teasers that point at later events also count as spoilers. Interviews "
        "about inspiration, research, craft and the author's life are usually safe. When unsure: "
        "safe=false. worth_reading=true if it would deepen the reader's understanding or enjoyment "
        "(not a thin listicle, sales page or bare announcement).\n\n"
        f"Title: {art['title']}\n\n{art['text'][:12000]}")
    try:
        out = llm.chat_json([{"role": "user", "content": prompt}], SPOILER_SCHEMA, task="rank",
                            think=True, max_tokens=4096)
        return bool(out.get("safe")) and bool(out.get("worth_reading")), out.get("reason", "")
    except llm.LLMError as e:
        return False, f"check failed: {e}"


def behind_the_book(st: dict, title: str, authors: str, pct: float, now: datetime) -> list[dict]:
    """Up to BEHIND_PER_DAY vetted outside articles about this book, never repeated."""
    day = now.date().isoformat()
    last = st.get("behind_searched")
    if not last or (now.date() - datetime.fromisoformat(last).date()).days >= BEHIND_SEARCH_EVERY_DAYS:
        found = search_behind(title, authors)
        st["behind_searched"] = day
        if found:
            known = {x["url"] for x in st.get("behind_pool", [])} | set(st.get("behind_shown", []))
            st.setdefault("behind_pool", []).extend(f for f in found if f["url"] not in known)
            log(f"behind the book: {len(found)} search results for '{title}'")
    out = []
    pool = st.get("behind_pool", [])
    while pool and len(out) < BEHIND_PER_DAY:
        cand = pool.pop(0)
        st.setdefault("behind_shown", []).append(cand["url"])  # checked once, never again
        art = fetch_article(cand["url"])
        if not art or len(art["text"]) < 1500:
            continue
        ok, why = spoiler_check(art, title, authors, pct, st.get("story_so_far", ""))
        log(f"behind the book: {'kept' if ok else 'dropped'} {cand['url']} ({why[:100]})")
        if ok:
            out.append({
                "url": cand["url"], "title": f"Behind the book: {art['title']}",
                "domain": urlparse(cand["url"]).netloc.removeprefix("www."),
                "published": day, "saved": now.isoformat(), "image": art["image"],
                "pin_top": True, "pin_order": 1, "html": art["html"]})
    return out


# --- render ----------------------------------------------------------------------------------

def _ul(items, fmt):
    return "<ul>" + "".join(f"<li>{fmt(x)}</li>" for x in items) + "</ul>"


def render(title: str, authors: str, book: dict, c: dict, sessions: list[dict], bio: "dict | None",
           first: bool) -> str:
    e = html.escape
    fiction = c.get("kind") != "nonfiction"
    pct = (book.get("percent") or 0) * 100
    head = f"<p><em>{e(authors)}</em> &middot; {pct:.0f}% read"
    if book.get("chapter"):
        head += f" &middot; now at <em>{e(book['chapter'])}</em>"
    parts = [head + "</p>"]
    parts.append(f"<h2>{'The story so far' if first else 'Previously'}</h2>")
    parts += [f"<p>{e(p)}</p>" for p in c.get("recap", [])]
    if len(sessions) > 1:
        parts.append("<h2>Your recent sessions</h2>" + _ul(sessions[-SESSIONS_SHOWN:][::-1], lambda s: (
            f"<b>{e(s['label'])}</b> ({s['from'] * 100:.0f}% &rarr; {s['to'] * 100:.0f}%"
            f"{', ' + str(s['pages']) + ' pages' if s.get('pages') else ''}"
            f"{', ' + str(s['minutes']) + ' min' if s.get('minutes') else ''}): {e(s['summary'])}")))
    if c.get("key_points"):
        parts.append(f"<h2>{'Key moments' if fiction else 'Key ideas'}</h2>" + _ul(c["key_points"], e))
    if c.get("quotes"):
        parts.append("<h2>Passages worth remembering</h2>" + "".join(
            f"<blockquote><p>&ldquo;{e(q['quote'])}&rdquo;</p></blockquote><p><em>{e(q['note'])}</em></p>"
            for q in c["quotes"]))
    if c.get("people"):
        parts.append(f"<h2>{'Who&rsquo;s who' if fiction else 'People'}</h2>" + _ul(
            c["people"], lambda x: f"<b>{e(x['name'])}</b>: {e(x['note'])}"))
    if c.get("terms"):
        parts.append("<h2>Places, terms &amp; ideas</h2>" + _ul(
            c["terms"], lambda x: f"<b>{e(x['name'])}</b>: {e(x['note'])}"))
    if c.get("threads"):
        parts.append(f"<h2>{'Open threads' if fiction else 'Questions to consider'}</h2>" + _ul(c["threads"], e))
    if c.get("recall"):
        parts.append("<h2>Test your recall</h2><p><em>Answer from memory; answers are at the end.</em></p>"
                     "<ol>" + "".join(f"<li>{e(r['q'])}</li>" for r in c["recall"]) + "</ol>")
    if c.get("tips"):
        parts.append("<h2>For your next session</h2>" + _ul(c["tips"], e))
    if bio and bio.get("text"):
        parts.append(f"<h2>About the author</h2><p>{e(bio['text'])}</p>"
                     f"<p style='color:#333'>From Wikipedia</p>")
    if not first:
        parts.append("<h2>The story so far</h2>" + "".join(
            f"<p>{e(p)}</p>" for p in c.get("story_so_far", "").split("\n") if p.strip()))
    if c.get("recall"):
        parts.append("<h2>Recall answers</h2><ol>" + "".join(f"<li>{e(r['a'])}</li>" for r in c["recall"]) + "</ol>")
    return "".join(parts)


# --- main --------------------------------------------------------------------------------------

def load_state(md5: str) -> dict:
    try:
        return json.loads((COMPANION / f"{md5}.json").read_text())
    except (OSError, ValueError):
        return {"covered_pct": 0.0, "story_so_far": ""}


def session_stats(book: dict, since: "str | None") -> tuple[int, int]:
    """Pages and minutes read (KOReader statistics) on days after `since`."""
    pages = secs = 0
    for day, s in (book.get("stats") or {}).items():
        if not since or day > since:
            pages += s.get("pages", 0)
            secs += s.get("seconds", 0)
    return pages, round(secs / 60)


def save_article(art: dict, name: str) -> None:
    ARTICLES.mkdir(parents=True, exist_ok=True)
    (ARTICLES / name).write_text(json.dumps(art, ensure_ascii=False))


def companion_for(book: dict, now: datetime) -> "dict | None":
    md5, title, authors = book["md5"], book.get("title") or "Untitled", book.get("authors") or ""
    st = load_state(md5)
    target = (book.get("percent") or 0) - float(CFG["companion_safety_pct"]) / 100
    if target - st["covered_pct"] < MIN_NEW_PCT:
        return None
    path = ks.find_book_file(md5, title, authors)
    if not path:
        log(f"'{title}' not found in the library (md5 {md5}); skipped")
        return None
    text = book_text(path)
    read_so_far = slice_text(text, 0, target)
    new = slice_text(text, st["covered_pct"], target)
    if not new:
        return None
    first = st["covered_pct"] == 0
    prior = st["story_so_far"]
    pieces = chunks(new, int(CFG["companion_max_chars"]))
    # Long catch-up (first run mid-book, or days of reading): fold all but the last piece into
    # the running summary, then write the companion from the last piece.
    for p in pieces[:-1]:
        prior = fold(prior, p, title)
    c = write_companion(title, authors, book.get("chapter", ""), prior, pieces[-1], first)
    n_q = len(c.get("quotes", []))
    c["quotes"] = verified_quotes(c.get("quotes", []), read_so_far, near=pieces[-1])
    if n_q != len(c["quotes"]):
        log(f"dropped {n_q - len(c['quotes'])} quote(s) not found verbatim in the book")

    if "author_bio" not in st:
        st["author_bio"] = author_bio(authors)
    day = now.date().isoformat()
    pages, minutes = session_stats(book, st.get("last_day"))
    st.setdefault("sessions", []).append({
        "date": day, "label": "First catch-up" if first else datetime.fromisoformat(day).strftime("%a %b %-d"),
        "from": st["covered_pct"], "to": target, "pages": pages, "minutes": minutes,
        "summary": c.get("session_summary", "")})
    st["sessions"] = st["sessions"][-30:]

    art = {
        "url": f"morning-paper://companion/{md5}/{day}",
        "title": f"{'The story so far' if first else 'Previously'} in {title}",
        "domain": "Your reading",
        "published": day,
        "saved": now.isoformat(),
        # the book's cover from the library folder (embedded by the build, not fetched)
        "image": str(path.parent / "cover.jpg") if (path.parent / "cover.jpg").exists() else None,
        "pin_top": True,
        "pin_order": 0,
        "full": True,  # a real recap: a later run today must not replace it with the short page
        "html": render(title, authors, book, c, st["sessions"], st.get("author_bio"), first),
        # front-page teaser: where you are, then the start of the recap
        "teaser": f"{(book.get('percent') or 0) * 100:.0f}% read"
                  + (f", now at {book['chapter']}" if book.get("chapter") else "") + ". "
                  + " ".join(c.get("recap", [])[:1]),
    }
    save_article(art, f"companion-{md5[:12]}-{day}.json")
    st.update({"covered_pct": target, "story_so_far": c.get("story_so_far") or prior, "title": title,
               "kind": c.get("kind"), "last_day": day, "updated": now.isoformat(timespec="seconds"),
               # reused by the daily page on days without a fresh companion (all spoiler-safe:
               # written from text before covered_pct)
               "last_companion": {k: c.get(k) for k in ("key_points", "people", "terms", "threads", "quotes")},
               "image": art["image"]})
    for extra in behind_the_book(st, title, authors, target, now):
        save_article(extra, f"behind-{hashlib.sha1(extra['url'].encode()).hexdigest()[:12]}.json")
    COMPANION.mkdir(parents=True, exist_ok=True)
    (COMPANION / f"{md5}.json").write_text(json.dumps(st, indent=2, ensure_ascii=False))
    log(f"wrote companion for '{title}' ({len(new)} chars of new text, {len(c['quotes'])} quotes)")
    return art


def daily_page(book: dict, now: datetime, llm_up: bool) -> dict:
    """The reading page on a day without a fresh companion: where you are, what you read since,
    and what the companion already knows (the story so far, people, threads). Needs no LLM and
    never goes past covered_pct, so it is as spoiler-safe as the last companion."""
    e = html.escape
    md5, title, authors = book["md5"], book.get("title") or "Untitled", book.get("authors") or ""
    st = load_state(md5)
    day = now.date().isoformat()
    pct = (book.get("percent") or 0) * 100
    head = f"<p><em>{e(authors)}</em> &middot; {pct:.0f}% read"
    if book.get("page") and book.get("pages"):
        head += f" &middot; page {book['page']} of {book['pages']}"
    if book.get("chapter"):
        head += f" &middot; now at <em>{e(book['chapter'])}</em>"
    parts = [head + "</p>"]

    days = book.get("days") or {}
    stats = book.get("stats") or {}
    recent = sorted(d for d in stats if d < day)[-1:]  # the last day you read before today
    since = st.get("last_day")
    pages, minutes = session_stats(book, since)
    new_pct = (book.get("percent") or 0) - float(CFG["companion_safety_pct"]) / 100 - st.get("covered_pct", 0)
    parts.append("<h2>Since the last recap</h2>")
    if new_pct >= MIN_NEW_PCT:
        bits = [f"You read on from {st.get('covered_pct', 0) * 100:.0f}% to {pct:.0f}%"]
        if pages:
            bits.append(f"{pages} pages")
        if minutes:
            bits.append(f"{minutes} min")
        parts.append(f"<p>{', '.join(bits)}.</p>")
        why = ("the recap writer (the LLM on the Mac) was not reachable this morning" if not llm_up
               else "the recap could not be written this morning")
        parts.append(f"<p><em>No recap of those pages today: {why}. The next edition covers them "
                     "together with whatever you read tonight.</em></p>")
    else:
        last = recent[0] if recent else (max(days) if days else None)
        when = datetime.fromisoformat(last).strftime("%A, %B %-d") if last else "a while"
        parts.append(f"<p>No new reading since {e(when)}. Here is where you left off.</p>")

    sessions = st.get("sessions") or []
    if sessions and sessions[-1].get("summary"):
        s0 = sessions[-1]
        parts.append(f"<h2>Where you left off</h2><p>{e(s0['summary'])}</p>")
    lc = st.get("last_companion") or {}
    fiction = st.get("kind") != "nonfiction"
    if lc.get("people"):
        parts.append(f"<h2>{'Who&rsquo;s who' if fiction else 'People'}</h2>" + _ul(
            lc["people"], lambda x: f"<b>{e(x['name'])}</b>: {e(x['note'])}"))
    if lc.get("threads"):
        parts.append(f"<h2>{'Open threads' if fiction else 'Questions to consider'}</h2>" + _ul(lc["threads"], e))
    if lc.get("terms"):
        parts.append("<h2>Places, terms &amp; ideas</h2>" + _ul(
            lc["terms"], lambda x: f"<b>{e(x['name'])}</b>: {e(x['note'])}"))
    if st.get("story_so_far"):
        parts.append("<h2>The story so far</h2>" + "".join(
            f"<p>{e(p)}</p>" for p in st["story_so_far"].split("\n") if p.strip()))
    if len(sessions) > 0:
        parts.append("<h2>Your reading log</h2>" + _ul(sessions[-SESSIONS_SHOWN:][::-1], lambda x: (
            f"<b>{e(x['label'])}</b> ({x['from'] * 100:.0f}% &rarr; {x['to'] * 100:.0f}%"
            f"{', ' + str(x['pages']) + ' pages' if x.get('pages') else ''}"
            f"{', ' + str(x['minutes']) + ' min' if x.get('minutes') else ''})")))
    if "author_bio" not in st:
        st["author_bio"] = author_bio(authors)
        if st.get("covered_pct") or st.get("author_bio"):
            COMPANION.mkdir(parents=True, exist_ok=True)
            (COMPANION / f"{md5}.json").write_text(json.dumps(st, indent=2, ensure_ascii=False))
    bio = st.get("author_bio")
    if bio and bio.get("text"):
        parts.append(f"<h2>About the author</h2><p>{e(bio['text'])}</p>"
                     f"<p style='color:#333'>From Wikipedia</p>")
    if not st.get("story_so_far"):
        parts.append("<p><em>The companion has not recapped this book yet; the first recap comes "
                     "with the next edition after you read on.</em></p>")

    image = st.get("image")
    if not image or not Path(image).exists():  # paths differ between the NAS and the Mac
        path = ks.find_book_file(md5, title, authors)
        if path and (path.parent / "cover.jpg").exists():
            image = str(path.parent / "cover.jpg")
    return {
        "url": f"morning-paper://companion/{md5}/{day}",
        "title": f"Your reading: {title}",
        "domain": "Your reading",
        "published": day,
        "saved": now.isoformat(),
        "image": image,
        "pin_top": True,
        "pin_order": 0,
        "html": "".join(parts),
        "teaser": f"{pct:.0f}% read" + (f", now at {book['chapter']}" if book.get("chapter") else "")
                  + ". " + (re.sub(r"<[^>]+>", "", parts[2]) if len(parts) > 2 else ""),
    }


def books_for_today(now: datetime) -> "tuple[list[dict], dict | None]":
    """(books read in the last 3 days, the book to feature every day).

    The featured book is the most recently touched one you are properly into (>= 1%), so
    opening a new book for a minute does not push the one you are reading off the page."""
    recent = ks.currently_reading(days=3, now=now)
    month = ks.currently_reading(days=60, now=now)
    started = [b for b in month if (b.get("percent") or 0) >= 0.01]
    return recent, (started or month or [None])[0]


def main() -> int:
    if not CFG["companion_enabled"]:
        return 0
    now = datetime.now(timezone.utc)
    llm_up = llm.available()
    if not llm_up:
        log("LLM unavailable; no fresh recap today (progress is kept, the next one covers both days)")
    recent, featured = books_for_today(now)
    written = set()
    if llm_up:
        for book in recent:
            try:
                if companion_for(book, now):
                    written.add(book["md5"])
            except Exception as e:
                log(f"'{book.get('title')}' failed: {type(e).__name__}: {str(e)[:200]}")
    # Every edition carries the book you are reading, recap or not.
    daily = [b for b in recent if (b.get("percent") or 0) >= 0.01]
    if featured and featured["md5"] not in {b["md5"] for b in daily}:
        daily.insert(0, featured)
    for book in daily:
        if book["md5"] in written:
            continue
        existing = ARTICLES / f"companion-{book['md5'][:12]}-{now.date().isoformat()}.json"
        try:
            if existing.exists() and json.loads(existing.read_text()).get("full"):
                continue  # today's recap was already written by an earlier run
        except ValueError:
            pass
        try:
            art = daily_page(book, now, llm_up)
            save_article(art, f"companion-{book['md5'][:12]}-{now.date().isoformat()}.json")
            log(f"wrote reading page for '{book.get('title')}' (no fresh recap today)")
        except Exception as e:
            log(f"reading page for '{book.get('title')}' failed: {type(e).__name__}: {str(e)[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
