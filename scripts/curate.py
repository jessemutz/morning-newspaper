#!/usr/bin/env python3
"""Pick today's articles from candidates.jsonl and append their URLs to queue.txt.

This is the curation step that curate.example.sh describes, using the local LLM:
  1. Spoiler guard: candidates that mention a book you are reading right now are dropped,
     unless the LLM (told how far you are) judges them clearly spoiler-free.
  2. Scoring: the LLM rates every candidate 0-10 against interests.json and names the
     interest input it serves.
  3. Selection: per-input quotas from the interest weights, at most 2 per site, best first.
Without the LLM (the Mac is asleep) it scores by keyword overlap and drops every
candidate that mentions a book being read.
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kobo_config import CFG, DATA_DIR  # noqa: E402
import llm  # noqa: E402
from gather_candidates import interest_summary  # noqa: E402

CANDIDATES = DATA_DIR / "candidates.jsonl"
INTERESTS = DATA_DIR / "interests.json"
QUEUE = DATA_DIR / "queue.txt"
REPORT = DATA_DIR / "curation.json"
STOP = set("the a an of and or to in on for with by at from is are was be this that it as your you "
           "how what why new news".split())
MAX_PER_SITE = 2
MIN_SCORE = 3  # below this a candidate never makes the paper, even on a thin day


def log(msg):
    print(f"{datetime.now():%F %T} curate: {msg}", flush=True)


def words(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9']+", (s or "").lower()) if w not in STOP and len(w) > 2}


def text_of(c: dict) -> str:
    return f"{c.get('title', '')} {c.get('snippet', '')} {c.get('url', '')}"


# --- spoiler guard ------------------------------------------------------------------------

def sensitive_books(interests: dict) -> list[dict]:
    return [e for e in interests.get("inputs", {}).get("books_reading", {}).get("entries", [])
            if e.get("spoiler_sensitive")]


def mentions(c: dict, book: dict) -> bool:
    """True if the candidate names the book (or the author's surname with a title word)."""
    t = text_of(c).lower()
    title = book["name"].lower()
    if len(title) >= 6 and title in t:
        return True
    title_words = words(title)
    surnames = {a.split()[-1].lower() for a in book.get("creator", "").split(",") if a.strip()}
    cw = words(t)
    return bool(surnames & cw) and bool(title_words & cw) or (len(title_words) >= 2 and title_words <= cw)


SPOILER_SCHEMA = {"type": "object", "properties": {
    "safe": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["safe", "reason"], "additionalProperties": False}


def spoiler_safe(c: dict, book: dict, use_llm: bool) -> "tuple[bool, str]":
    if not use_llm:
        return False, "mentions a book being read; no LLM to check"
    prompt = (
        f"A reader is {book.get('progress_pct', 0)}% through '{book['name']}' by {book.get('creator') or 'unknown'}"
        f"{' (currently at: ' + book['chapter'] + ')' if book.get('chapter') else ''}. "
        "Could reading the article below reveal ANY plot event, twist, death, reveal, or ending from "
        "later in the book or its sequels? Adaptation casting news, author interviews about craft, "
        "and reviews that stay vague are usually safe; recaps, analyses, 'ending explained', fan "
        "theories and episode guides are not. When unsure, answer safe=false.\n\n"
        f"Title: {c.get('title')}\nURL: {c.get('url')}\nSnippet: {c.get('snippet')}")
    try:
        out = llm.chat_json([{"role": "user", "content": prompt}], SPOILER_SCHEMA, task="rank", think=True,
                            max_tokens=2048)
        return bool(out.get("safe")), out.get("reason", "")
    except llm.LLMError as e:
        return False, f"spoiler check failed: {e}"


def guard(cands: list[dict], books: list[dict], use_llm: bool) -> "tuple[list, list]":
    kept, dropped = [], []
    for c in cands:
        hit = next((b for b in books if mentions(c, b)), None)
        if not hit:
            kept.append(c)
            continue
        ok, why = spoiler_safe(c, hit, use_llm)
        (kept if ok else dropped).append({**c, "spoiler_check": {"book": hit["name"], "safe": ok, "reason": why}})
    return kept, dropped


# --- scoring --------------------------------------------------------------------------------

def with_followed(interests: dict, cands: list[dict]) -> dict:
    """Add the sources you follow (feeds, blogs, podcasts) as an interest input of their own.

    Following a site is itself a signal: without this, a profile built from books and genres
    scores everything from a general feed (Hacker News, The Verge) as irrelevant."""
    sources = sorted({c.get("source", "") for c in cands if c.get("kind") in ("feed", "blog", "podcast")} - {""})
    w = float(CFG["followed_sources_weight"])
    if not sources or w <= 0:
        return interests
    out = json.loads(json.dumps(interests))
    inputs = out.setdefault("inputs", {})
    for v in inputs.values():
        v["weight"] = round(v.get("weight", 0) * (1 - w), 3)
    inputs["followed_sources"] = {"weight": w, "entries": [
        {"kind": "source", "name": s, "why": "a source the reader chose to follow"} for s in sources]}
    return out


def score_schema(inputs: list[str]) -> dict:
    return {"type": "object", "properties": {"scores": {"type": "array", "items": {
        "type": "object", "properties": {
            # "why" comes first on purpose: under a JSON grammar the model writes fields in
            # order, and a reason before the number stops it copying the previous row's score.
            "i": {"type": "integer"}, "why": {"type": "string"},
            "interest": {"type": "string", "enum": inputs + ["none"]}, "score": {"type": "integer"}},
        "required": ["i", "why", "interest", "score"], "additionalProperties": False}}},
        "required": ["scores"], "additionalProperties": False}


def llm_scores(cands: list[dict], interests: dict, batch: int = 40) -> None:
    inputs = list(interests.get("inputs", {}).keys()) or ["general"]
    rules = "\n".join(f"- {r}" for r in interests.get("rules", []))
    for start in range(0, len(cands), batch):
        chunk = cands[start:start + batch]
        listing = "\n".join(
            f"{i}. [{c.get('kind', 'feed')}: {c.get('source', '')}] {c.get('title') or '(untitled)'} | {c.get('snippet', '')[:250]} | {c['url']}"
            for i, c in enumerate(chunk))
        prompt = (
            "You are the editor of a one-reader morning newspaper. Score each candidate 0-10 for how "
            "much this reader would want to read it today (10 = must read). Penalize clickbait, "
            "listicles, press releases, paywalled teasers, thin aggregator pages and near-duplicates "
            "of another candidate. For each: `why` in at most 12 words, then `interest` (the input it "
            "serves best, or none), then `score`.\n"
            "followed_sources: the reader chose to follow these sources, so their best pieces "
            "(substantial, original, well made) score well under followed_sources even when they "
            "match no other input; their filler (deals, gift guides, minor product news) does not. "
            "Candidates marked search must match one of the other inputs.\n\n"
            f"Reader interests:\n{interest_summary(interests)}\n\nRules:\n{rules}\n\nCandidates:\n{listing}")
        try:
            out = llm.chat_json([{"role": "user", "content": prompt}], score_schema(inputs), task="rank",
                                max_tokens=8192)
        except llm.LLMError as e:
            log(f"scoring batch {start} failed: {e}; keyword fallback for it")
            keyword_scores(chunk, interests)
            continue
        for s in out.get("scores", []):
            if 0 <= s.get("i", -1) < len(chunk):
                chunk[s["i"]]["score"] = max(0, min(10, int(s["score"])))
                chunk[s["i"]]["interest"] = s.get("interest", "none")


def keyword_scores(cands: list[dict], interests: dict) -> None:
    vocab = {}
    for name, inp in interests.get("inputs", {}).items():
        for e in inp.get("entries", []):
            for w in words(f"{e.get('name', '')} {e.get('creator', '')}"):
                vocab.setdefault(w, name)
    for c in cands:
        hits = [vocab[w] for w in words(text_of(c)) if w in vocab]
        c["score"] = min(10, 3 * len(hits)) if hits else 1
        c["interest"] = max(set(hits), key=hits.count) if hits else "none"


# --- selection ------------------------------------------------------------------------------

def select(cands: list[dict], interests: dict, n: int) -> list[dict]:
    weights = {k: v.get("weight", 0) for k, v in interests.get("inputs", {}).items()}
    quota = {k: round(w * n) for k, w in weights.items()}
    ranked = sorted((c for c in cands if c.get("score", 0) >= MIN_SCORE), key=lambda c: c["score"], reverse=True)
    picked, per_site = [], {}

    def take(c):
        site = urlparse(c["url"]).netloc.removeprefix("www.")
        if per_site.get(site, 0) >= MAX_PER_SITE or c in picked:
            return False
        per_site[site] = per_site.get(site, 0) + 1
        picked.append(c)
        return True

    for c in ranked:  # first pass: honour each input's quota
        k = c.get("interest", "none")
        if len(picked) < n and quota.get(k, 0) > 0 and take(c):
            quota[k] -= 1
    for c in ranked:  # second pass: fill what is left with the best of the rest
        if len(picked) >= n:
            break
        take(c)
    return picked


def main() -> int:
    if not CANDIDATES.exists():
        log("no candidates.jsonl; run gather_candidates.py first")
        return 1
    cands = [json.loads(ln) for ln in CANDIDATES.read_text().splitlines() if ln.strip()]
    try:
        interests = json.loads(INTERESTS.read_text())
    except (OSError, ValueError):
        interests = {}
    use_llm = llm.available()
    log(f"{len(cands)} candidates, LLM {'up' if use_llm else 'DOWN (keyword fallback)'}")
    cands, dropped = guard(cands, sensitive_books(interests), use_llm)
    interests = with_followed(interests, cands)
    log(f"spoiler guard dropped {len(dropped)}")
    if use_llm:
        llm_scores(cands, interests)
    else:
        keyword_scores(cands, interests)
    # Queue ~50% extra, best first: some picks turn out stale or unextractable once fetched,
    # and the build stops at articles_per_edition in queue order.
    n = int(CFG["articles_per_edition"])
    picked = select(cands, interests, n + (n + 1) // 2)
    with QUEUE.open("a") as f:
        f.write(f"# curated {datetime.now():%F %T}\n")
        for c in picked:
            f.write(c["url"] + "\n")
    scored = sorted(({"score": c.get("score"), "interest": c.get("interest"), "title": c.get("title"),
                      "url": c["url"]} for c in cands), key=lambda c: -(c["score"] or 0))
    REPORT.write_text(json.dumps({"picked": picked, "spoiler_dropped": dropped, "scored": scored,
                                  "llm": use_llm}, indent=2, ensure_ascii=False))
    log(f"queued {len(picked)} articles -> {QUEUE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
