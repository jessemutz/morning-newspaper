# CLAUDE.md: working notes for this repo

A daily personalized "newspaper" EPUB for e-readers. Originally Kobo + Calibre-Web Kobo Sync
(`"delivery": "kobo"`, still works). The active path is **`"delivery": "koreader"`**: a jailbroken
Kindle running KOReader with a custom plugin, a small HTTP server, a local LLM. See
`CLAUDE.local.md` (gitignored) for the owner's environment and current status.

## Pipeline (`scripts/run_daily.py` runs these in order, each step failure-isolated)
1. `fetch_fonts.py`: downloads UnifrakturMaguntia (blackletter masthead) + PT Serif into `<data_dir>/fonts` if missing.
2. `kobo-interests.py`: builds `interests.json` (REGENERATED every run; users edit `config.json`, never this).
   `koreader_reading()` reads `reading/state.json`; current books get `spoiler_sensitive: true`.
   Inputs: `books_reading` (+ their authors; the paper's own editions are skipped), `topics`
   (config; no `genres` any more), `own_media` (`media_dirs` recent additions, dated by newest
   file inside: folder mtimes were bulk-touched), tiktok, kickstarter. Empty inputs give their
   weight away in proportion; `interest_weights` overrides. `avoid_topics`/`extra_rules` become rules.
3. `gather_candidates.py` → `candidates.jsonl`: `feeds`, `blog_pages` (feed discovery, else link scrape),
   `podcasts` (RSS URL or show name → Apple lookup cached in `podcast_feeds.json`; episodes are
   **prebuilt** from show notes into `prebuilt/` because feed links point at the show homepage),
   web search (LLM writes queries → Kagi `KAGI_API_KEY` → SearXNG fallback; skipped if neither;
   `is_listing()` drops homepages/tag/section pages). Dedupes against `seen.jsonl`.
4. `curate.py` → appends URLs to `queue.txt` (~1.5× `articles_per_edition`, rank order; build caps).
   Spoiler guard first (LLM check vs progress; no LLM = drop every mention). Adds a synthetic
   `followed_sources` interest (`followed_sources_weight`) or general feeds all score 0.
   Report in `curation.json` (includes all scores).
5. `book_companion.py`: per book read since last run: find EPUB (KOReader partial MD5, else title
   match ignoring subtitle; Calibre-sent copies never md5-match), slice text from `covered_pct` to
   `percent - companion_safety_pct`, fold long catch-ups into a running summary, one LLM call
   (thinking on) → recap, session summary, key points, people, terms, threads, quotes, recall Q&A,
   tips. Quotes verified verbatim (`verified_quotes`, near-miss repaired via difflib). Author bio
   from Wikipedia only (never model memory: it hallucinated). "Behind the book": search
   (Kagi/SearXNG) → fetch → LLM spoiler check against story-so-far → max 2/day, pool refreshed weekly.
   State: `reading/companion/<md5>.json` (covered_pct, story_so_far, sessions, author_bio, behind_*).
6. `sports.py`: config `sports: [{"league","follow":[...]}, ...]`. ESPN public site API for team
   leagues (`LEAGUES` map; `"espn": "sport/league"` override), Jolpica/Ergast for F1 + ESPN F1 news.
   Name matching is substring of follow-in-name; abbreviations only by exact match (the "LA" in
   "avalanche" bug).
7. `recommend.py`: LLM (thinking OFF; with thinking it burned the token budget and returned
   nothing) suggests books/podcasts/screen/blogs; each routed by its own `kind` (model misfiles)
   and verified: books Hardcover (`HARDCOVER_API_KEY`, GraphQL `search`, untested) → Open Library →
   Wikipedia; podcasts Apple; film/TV Wikipedia (Apple returns nothing for these); blogs live fetch.
   History in `recommendations.json`, never repeated.
8. `kobo-digest-build.py`: fetch/extract (trafilatura, images on), build
   `editions/<edition_title> YYYY-MM-DD.epub`, record `seen.jsonl`, clear queue, prune editions.
   Edition order via `_section_key`: `pin_top` (companion `pin_order` 0, behind-the-book 1) → news
   in queue order → `pin_bottom` (sports 0, recommendations 1). Images: `embed_images()` → grayscale
   JPEG ≤1000×1300, min side 200px, ≤6/article; local paths allowed (book covers).

Special article JSON (same shape as fetched ones, in `articles/`): url `morning-paper://...`,
`pin_top`/`pin_bottom`, `pin_order`, `published` = today (only today's specials enter an edition).

## Delivery
- `scripts/paper_server.py` (stdlib, needs `PAPER_TOKEN`): `POST /api/progress`, `GET /api/manifest`,
  `GET /files/edition/<urlquoted name>`, `GET /files/book/<id>` (CWA shelf `kindle_shelf`), `/health`.
  Manifest URLs MUST be URL-quoted (KOReader sends raw spaces → broken request; that bug happened).
- `koreader/morningpaper.koplugin/main.lua`: snapshot progress on close/suspend → outbox; on
  wake/Wi-Fi (≥2h apart) flush + download manifest items to `<home>/<edition_title>/` and `Books/`,
  prune old editions. Menu: Status, Check connection, Open latest edition, Sync now, auto-sync
  toggle, Wi-Fi-on-wake, Server URL, Token. All entry points wrapped by `guard()` (a Lua error in a
  menu callback kills KOReader). JSON via rapidjson→json fallback; `extend`→`new` fallback.
  Syntax check: `luajit -bl main.lua`. No KOReader emulator here: test on the device; errors land
  in `koreader/crash.log` (grep `MorningPaper`).

## Library access gotcha
CWA keeps `metadata.db`/`app.db` in **WAL mode**; read-only SQLite over SMB fails ("unable to open
database file") and `immutable=1` would skip the WAL. `koreader_state._ro()` copies db + `-wal`
into `<data_dir>/dbcache/` when mtime/size change and reads the copy. Never write to the library.

## LLM
`scripts/llm.py`: OpenAI-compatible (llama-server router mode), `LLAMA_API_KEY` optional,
`chat_template_kwargs.enable_thinking` per call, `json_schema` response_format. Under a JSON
grammar put a reason field BEFORE a score (else the model copies the previous row's score:
the all-zeros bug). Empty content + finish_reason=length → thinking ate `max_tokens`.
Models are configured per task in `llm_models` (`rank`, `write`).

## Config / run / test
- Config lookup: `KOBO_PAPER_CONFIG` → `./config.json` → repo root. Defaults + comments: `scripts/kobo_config.py`.
- Local run: `KOBO_PAPER_CONFIG=$PWD/config.mac.json .venv/bin/python scripts/run_daily.py`
- Single steps: run any script directly with the same env var.
- Rebuild today's edition after a code change without re-curating: copy URLs from today's
  `seen.jsonl` rows back into `queue.txt`, empty `seen.jsonl`, run `kobo-digest-build.py`.
- Tests: `.venv/bin/python -m pytest -q` (no network/LLM). `tests/test_koreader.py` uses an `env`
  fixture that re-imports modules against a temp data_dir.
- Deploy: `deploy/truenas/` (Dockerfile + supercronic crontab 05:15 + compose with server and
  scheduler services, library mounted read-only), `deploy/mac/` (llama-server LaunchAgent,
  05:06 caffeinate; pair with `sudo pmset repeat wakeorpoweron MTWRFSU 05:05:00`).
  Inside Docker, `.local` hostnames usually don't resolve: use IPs.

## Ideas queued by the owner (not built)
Interest sources: Jellyfin (watched + unwatched), Navidrome/Feishin and/or Spotify listening, Immich
"on this day" photos on the front page. Keep sources pluggable like `kobo-interests.py` `run()`.
