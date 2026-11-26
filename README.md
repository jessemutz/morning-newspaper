# kobo-morning-paper: a morning newspaper for your e-reader

Every morning a fresh EPUB of about 10 articles appears on your Kobo. You tap sync. No cable, no app.

![System design](preview.png)

This repo is the code behind that pipeline. You run it on your own server. Nothing here calls a hosted service except the sites you read and, optionally, your own LLM.

## Kindle + KOReader delivery (default)

With `"delivery": "koreader"` the pipeline publishes one dated EPUB per day
(`Morning Paper YYYY-MM-DD.epub`) and a small server hands it to a KOReader plugin. Nothing
goes through Calibre: the library is only read, for book sync and book companions.

```
Kindle (KOReader + morningpaper.koplugin) --progress / manifest / files--> paper_server.py (TrueNAS)
                                                                              | reads CWA library + app.db (read-only)
                                                                              | Kagi API -> SearXNG fallback
                                                                              +-> llama-server on the Mac (OpenAI API)
```

The morning run (`scripts/run_daily.py`, 05:15):

1. `kobo-interests.py` builds `interests.json`. Reading activity comes from what KOReader reported.
   Books you are reading are marked `spoiler_sensitive`.
2. `gather_candidates.py` collects candidates from `feeds`, `blog_pages` (feed discovered or links
   scraped), `podcasts`, and web search. The LLM writes the search queries; Kagi runs them, and
   SearXNG takes over when Kagi fails or is not configured.
3. `curate.py` drops anything that could spoil a book you are reading (the LLM checks it
   against your progress; with no LLM it drops every mention). It scores the rest 0-10 and picks
   about `articles_per_edition` with per-interest quotas and at most 2 per site. Sources you follow
   get their own share (`followed_sources_weight`).
4. `book_companion.py`: for each book you read since the last run, it finds the EPUB in the library
   by KOReader's partial MD5 and cuts the text from the last companion up to just before your position.
   The LLM writes "Previously in ..." from that text only: a recap, who's who, terms, open threads,
   reading tips, and a running story so far. The LLM never sees text past your position.
5. `kobo-digest-build.py` fetches the articles and builds the edition (companion pages first).
   Shipped URLs go to `seen.jsonl`, so they never repeat.

If the Mac is asleep, the paper still ships: scoring falls back to keyword overlap and the companion
waits a day. The next day's companion covers both days.

### Setup

1. **Library on TrueNAS.** Install Calibre-Web-Automated. Quit Mac Calibre, `rsync -a` the library
   into CWA's library dataset, and from then on manage books only in CWA. Never open that library
   in desktop Calibre while CWA runs (one writer only), and don't keep the live library in Dropbox.
   In CWA, create a shelf named `Kindle`: every book on it downloads to the Kindle on its next wake.
2. **SearXNG (fallback search).** Install the TrueNAS app and add `json` to `search.formats` in
   its `settings.yml`.
3. **Mac LLM.** `~/bin/start-llama-server.sh --host 0.0.0.0` with `LLAMA_API_KEY` set. (Or bind to
   the Mac's Tailscale IP.) To wake the Mac for the run:
   `sudo pmset repeat wakeorpoweron MTWRFSU 05:05:00`, then load `deploy/mac/*.plist` into
   `~/Library/LaunchAgents` (llama-server at login, and `caffeinate` from 05:06 to 06:06).
4. **Pipeline on TrueNAS.** Clone this repo onto a dataset. Copy `config.example.json` to
   `<config dataset>/config.json` and edit it. Edit the paths in `deploy/truenas/docker-compose.yml`
   and install it ("Install via YAML"), with `PAPER_TOKEN`, `LLAMA_API_KEY` and `KAGI_API_KEY` set.
5. **Kindle.** Copy `koreader/morningpaper.koplugin` to `koreader/plugins/` on the Kindle and restart
   KOReader. Under Tools -> Morning Paper, set the server URL (`http://truenas.local:8765` or the
   Tailscale name) and the token, then run Sync now. Turn on "Turn on Wi-Fi to sync on wake" if
   Wi-Fi is usually off.
6. **First run by hand.**
   `docker compose exec scheduler python /app/scripts/run_daily.py`, then Sync now on the Kindle.

### Plugin behaviour

- When you close a book or the Kindle sleeps, the plugin saves your progress (md5, percent,
  chapter) and two days of KOReader statistics to an outbox. It sends them when online.
- On wake (at most every 2 hours) and when Wi-Fi connects, it uploads the outbox, then downloads
  new editions to `documents/Morning Paper/` and shelf books to `documents/Books/`. Editions older
  than `editions_keep` are deleted. Books are never deleted.

### Edition layout

1. **Reading companion** (`book_companion.py`), one page per book you read since the last edition:
   - a recap (the first time: the story so far), and a log of your recent sessions with pages and minutes
   - key moments, or key ideas for nonfiction
   - verbatim passages worth remembering: each quote is checked against the book text, and a
     close paraphrase is replaced by the real sentence
   - who's who, places and terms, open threads (or questions to consider)
   - recall questions, with the answers at the end of the page, and tips for the next session
   - the author's bio from Wikipedia. When there is no article it is left out, never guessed.

   With web search configured, it also adds up to two **Behind the book** articles a day:
   interviews and the author on the book's inspiration. The LLM reads each one alongside your
   story so far and drops anything that could reveal more than you have read. Study-guide, wiki
   and forum sites are never used.
2. **The news**, curated as above.
3. **Sports** (`sports.py`): for each team, the last result, the next game with TV listings,
   record and standing, and headlines that name your teams. For F1: the last race's podium, your
   drivers' finishes, standings, the next race, and headlines. Data comes from ESPN's public API
   and Jolpica (Ergast); no keys needed.
   ```json
   "sports": [
     {"league": "NHL", "follow": ["Avalanche"]},
     {"league": "F1", "follow": ["Max Verstappen", "Kimi Antonelli", "Ferrari"]}
   ]
   ```
   Built-in leagues: NHL, NFL, NBA, WNBA, MLB, MLS, EPL, La Liga, Serie A, Bundesliga, Champions
   League, NWSL, NCAAF, NCAAM, NCAAW, F1. Any other ESPN league works with
   `"espn": "<sport>/<league>"`. A constructor in `follow` covers both of its drivers.
4. **Recommendations** (`recommend.py`): books, podcasts, movies & TV, and blogs, based on what you
   read, listen to and follow. Every pick is checked against a real catalogue (Hardcover if
   `HARDCOVER_API_KEY` is set, Open Library, Wikipedia, Apple Podcasts, or the site itself)
   and dropped if it can't be found. Picks are never repeated, never include sequels of books
   you are reading, and books you already own are marked.
   `"recommendations": {"enabled": true, "per_type": 3}`

The cover and front page use a blackletter masthead (UnifrakturMaguntia) and PT Serif.
`fetch_fonts.py` downloads them (OFL) on the first run. Article, podcast and book-cover images
are embedded as grayscale JPEGs sized for e-ink (`"article_images": false` turns this off).

## Kobo delivery (original)

Everything below describes `"delivery": "kobo"`.

## How it works

1. **Interest model** (`scripts/kobo-interests.py`, 5:15 AM) reads up to five sources, read-only, and writes one file: `interests.json`.
   - Reading 35%: what you are reading (the KOReader plugin's progress, or Kobo progress from Calibre-Web, plus Audiobookshelf) and those books' authors.
   - Topics 45%: the `topics` list in `config.json`. Web search writes its queries mostly from these.
   - Your media 12%: recent additions to the folders in `media_dirs` (newest file inside each item, last `media_recent_days`, minus `exclude_titles`).
   - TikTok 5%: public TikTok collections, read with yt-dlp.
   - Kickstarter 3%: backed projects, at most one item, from "You just backed" emails over IMAP.
   - An input that is not configured, fails, or has nothing in it gives its weight to the others in proportion, and `interests.json` says why. Override the split with `interest_weights`. Curation then reserves `followed_sources_weight` (default 0.3) of the paper for the feeds you follow.
2. **Curation** (5:30 AM): an LLM reads `interests.json` and picks about 10 articles into `queue.txt`. This step is **yours to plug in**. See `curate.example.sh` for the contract. No curation agent ships in this repo.
3. **Fetch + extract** (`scripts/kobo-digest-build.py`, 6:00 AM): trafilatura turns each URL into clean article text.
4. **Build edition**: ebooklib writes one EPUB, newest first, one chapter per article, with a generated cover and a tappable front page.
5. **Calibre**: `calibredb add_format` replaces the format on the same book in place, then `scripts/kepub_ensure.py` adds a KEPUB.
6. **Calibre-Web, Kobo Sync**: Calibre-Web offers the new edition over the Kobo Sync protocol.
7. **Kobo**: tap sync.

`scripts/kobo-queue.py` is a small intake helper. It reads a message, finds lines that look like `kobo: https://...`, and appends them to `queue.txt`. Anything that can run a command can feed it (a chat bot, an SSH shortcut).

## What you need

- A server that runs scheduled jobs (cron) and Docker
- Calibre and Calibre-Web, with Kobo Sync turned on
- A Kobo e-reader
- An LLM, or any tool, for the curation step
- Python 3.10 or newer

## Setup

1. **Install.**
   ```
   git clone https://github.com/jstriblet/kobo-morning-paper.git
   cd kobo-morning-paper
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
   `opencv-python-headless` and `numpy` are only for the lead-image face filter. `yt-dlp` is only for TikTok collections.

2. **Configure.** `cp config.example.json config.json`, then edit it. The keys that matter first:
   - `data_dir`: where the pipeline keeps `queue.txt`, `articles/`, `interests.json` and the EPUB.
   - `paper_title`, `paper_tagline`: printed on the cover and front page.
   - `calibre_library`: the folder that holds Calibre's `metadata.db`.
   - `calibre_web_app_db`: Calibre-Web's `app.db`.
   - `kobo_user_id`: the Calibre-Web user id that owns the Kobo.
   - Source keys (`audiobookshelf_container`, `media_dirs`, `tiktok_collections`, `topics`, `kickstarter_enabled`). Leave a source empty to skip it.

   Scripts find the file through `KOBO_PAPER_CONFIG`, then `./config.json`, then the repo root. `config.json` is in `.gitignore`.

   For the Kickstarter source, set `KOBO_IMAP_USER` and `KOBO_IMAP_PASSWORD` in the environment. The password never goes in the config file.

3. **Fonts (optional).** The cover uses UnifrakturMaguntia and PT Serif TTF files. They are not in this repo. Put them in `<data_dir>/fonts/` (names are set by `masthead_font`, `serif_bold_font`, `serif_regular_font`). Without them the build still works and uses Pillow's built-in font.

4. **Calibre-Web Kobo Sync shelf (required).** Kobo Sync only offers books that are on a shelf with Kobo Sync turned on.
   - In Calibre-Web, create a shelf and turn on "Sync this shelf with Kobo".
   - Put the paper book on it. The build script re-adds the book to the first `kobo_sync` shelf on every run and clears Calibre-Web's "already synced" marker, so each refresh is offered again.
   - Calibre-Web must run with Kobo Sync enabled, and the Kobo must be pointed at it (`api_endpoint` in the Kobo's config).

5. **KEPUB note.** A Kobo handles a KEPUB better than a plain EPUB (for example, highlights survive). `scripts/kepub_ensure.py` converts the EPUB with `kepubify` inside the Calibre-Web container (the linuxserver `calibre-web` image includes it) and adds the KEPUB format to the same book. Set `library_owner` to the uid:gid that owns your library.

6. **Run once by hand.**
   ```
   .venv/bin/python scripts/kobo-interests.py          # writes interests.json
   cp curate.example.sh curate.sh                       # edit it: your LLM step
   printf 'https://example.com/an-article\n' >> ~/kobo-articles/queue.txt
   .venv/bin/python scripts/kobo-digest-build.py       # builds the EPUB, updates Calibre
   ```
   Then sync the Kobo. To build only the EPUB without Calibre, set `"calibre_enabled": false`, or run `kobo-digest-build.py --dry-build DIR` against a folder that holds `articles/*.json`.

7. **Schedule.** `crontab.example` has the 5:15 / 5:30 / 6:00 AM lines. Edit the paths and add them to your cron table.

## Files

- `scripts/kobo-interests.py`: the interest model.
- `scripts/kobo-queue.py`: `kobo:` URL intake.
- `scripts/kobo-digest-build.py`: fetch, extract, build the EPUB, update Calibre.
- `scripts/kepub_ensure.py`: KEPUB conversion.
- `scripts/kobo_config.py`: shared config loader and defaults.
- `scripts/run_daily.py`: the morning run (KOReader delivery).
- `scripts/gather_candidates.py`, `scripts/curate.py`: candidates and LLM curation.
- `scripts/book_companion.py`: spoiler-safe "Previously in ..." pages.
- `scripts/koreader_state.py`, `scripts/paper_server.py`: reading state and the plugin's server.
- `scripts/llm.py`: OpenAI-compatible client for llama-server.
- `koreader/morningpaper.koplugin/`: the KOReader plugin.
- `deploy/truenas/`, `deploy/mac/`: container, schedule, Mac wake and llama-server agents.
- `curate.example.sh`: the curation contract.
- `tests/test_smoke.py`, `fixtures/`: smoke test, no network and no Calibre.

## Test

```
.venv/bin/python -m pytest
```

The smoke test builds an EPUB from two fixture articles.

## Open-source parts

trafilatura, ebooklib, Calibre and Calibre-Web, yt-dlp, Kobo Sync.

## License

MIT
