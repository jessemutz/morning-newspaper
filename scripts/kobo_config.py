"""Shared configuration for the morning-paper scripts.

Settings come from a JSON file. Lookup order:
  1. the path in the KOBO_PAPER_CONFIG environment variable
  2. ./config.json (the current directory)
  3. config.json in the repo root (next to scripts/)
Every key has a default, so the scripts run with no config file at all.
Secrets (the optional IMAP login) are read from environment variables, never from the file.
"""
import json
import os
from pathlib import Path

DEFAULTS = {
    # Where the pipeline keeps its files: queue.txt, articles/, interests.json, the EPUB, logs.
    "data_dir": "~/kobo-articles",
    # The paper itself.
    "paper_title": "The Morning Paper",
    "paper_tagline": "All the Reads Worth Your Time",
    "book_title": "Read Later",          # the single Calibre book that is refreshed daily
    "book_author": "Articles",
    "max_age_days": 7,                    # drop articles older than this (pinned.txt bypasses)
    # Fonts for the generated cover and front page (TTF files). See README, "Fonts".
    "fonts_dir": "fonts",                 # relative to data_dir unless absolute
    "masthead_font": "UnifrakturMaguntia-Book.ttf",
    "serif_bold_font": "PT_Serif-Web-Bold.ttf",
    "serif_regular_font": "PT_Serif-Web-Regular.ttf",
    # Calibre / Calibre-Web (all optional: without them the build stops after writing the EPUB).
    "calibre_enabled": True,
    "calibre_library": "/path/to/calibre/library",   # folder that holds metadata.db
    "calibre_docker_image": "lscr.io/linuxserver/calibre:latest",
    "calibre_web_container": "calibre-web",
    "calibre_web_app_db": "/path/to/calibre-web/config/app.db",
    "kobo_user_id": 1,                    # Calibre-Web user id that owns the Kobo
    # Interest model sources.
    "audiobookshelf_container": "",       # docker container name; empty = skip Audiobookshelf
    "audiobookshelf_db": "/config/absdatabase.sqlite",
    "media_watch_file": "",               # optional JSONL of media requests; empty = skip
    "media_requester": "me",              # value of requested_by that counts as yours
    "seerr_url": "",                      # e.g. http://localhost:5055 ; empty = skip Seerr
    "seerr_settings": "",                 # path to Seerr settings.json (holds the API key)
    "seerr_user": "",
    "ytdlp": "yt-dlp",                    # path or name of the yt-dlp binary
    "tiktok_collections": [],             # public TikTok collection URLs
    "exclude_titles": [],
    "exclude_requesters": [],
    "genres": ["fiction", "fantasy"],
    "topics": [],                         # non-book subjects you want covered, e.g. "woodworking"
    "avoid_topics": [],                   # subjects to keep out (or cap), e.g. "crypto prices"
    "extra_rules": [],                    # extra plain-English rules appended to interests.json
    "lead_image_avoid_faces": True,       # skip photos with a prominent face for lead images
    "article_images": True,               # embed article / podcast / book-cover images in the edition
    "library_owner": "1000:1000",         # uid:gid that owns your Calibre library
    "kickstarter_enabled": False,         # needs KOBO_IMAP_USER / KOBO_IMAP_PASSWORD
    "imap_host": "imap.gmail.com",
    "imap_folder": "[Gmail]/All Mail",

    # --- KOReader delivery ------------------------------------------------------------
    # "koreader": dated editions served to the KOReader plugin (server/paper_server.py).
    # "kobo": the original single Calibre book refreshed over Calibre-Web Kobo Sync.
    "delivery": "koreader",
    "edition_title": "Morning Paper",     # file name prefix: "Morning Paper 2026-11-25.epub"
    "editions_keep": 7,                   # editions kept on the server and listed in the manifest
    "articles_per_edition": 10,
    "server_host": "0.0.0.0",
    "server_port": 8765,
    # Calibre-Web-Automated (the library the Kindle's books come from). Paths are as the
    # pipeline sees them (inside the container: mount the library read-only).
    "cwa_url": "http://truenas.local:30437",
    "cwa_app_db": "",                     # CWA's app.db (shelves); empty = no shelf sync
    "kindle_shelf": "Kindle",             # books on this CWA shelf sync to the Kindle

    # --- LLM (llama-server on the Mac, OpenAI-compatible) ---------------------------------
    "llm_base_url": "http://127.0.0.1:1234/v1",   # API key comes from LLAMA_API_KEY
    "llm_models": {"rank": "qwen3.6-35b-a3b-ud-q4_k_xl", "write": "qwen3.6-35b-a3b-ud-q4_k_xl"},
    "llm_timeout": 600,

    # --- Candidate sources ---------------------------------------------------------------
    "feeds": [],                          # RSS/Atom URLs
    "blog_pages": [],                     # sites without a known feed (feed is discovered, else links scraped)
    "podcasts": [],                       # podcast RSS URLs (transcripts used when published)
    "search_queries_max": 8,              # LLM-written web searches per day
    "search_results_per_query": 6,
    "kagi_enabled": True,                 # needs KAGI_API_KEY; falls back to SearXNG
    "searxng_url": "",                    # e.g. http://truenas.local:8080 (json format enabled)
    "candidates_per_feed": 15,
    "followed_sources_weight": 0.3,       # share of the paper for feeds/blogs/podcasts you follow

    # --- Sports & recommendations ------------------------------------------------------------
    # [{"league": "NHL", "follow": ["Avalanche"]}, {"league": "F1", "follow": ["Max Verstappen", "Ferrari"]}]
    "sports": [],
    "recommendations": {"enabled": True, "per_type": 3},

    # --- Book companion ---------------------------------------------------------------------
    "companion_enabled": True,
    "companion_max_chars": 120000,        # cap on the slice of book text sent per recap
    "companion_safety_pct": 0.5,          # stop this many percent before the reader's position
}


def config_path() -> Path:
    env = os.environ.get("KOBO_PAPER_CONFIG")
    if env:
        return Path(env).expanduser()
    for p in (Path.cwd() / "config.json", Path(__file__).resolve().parent.parent / "config.json"):
        if p.exists():
            return p
    return Path("config.json")


def load() -> dict:
    cfg = dict(DEFAULTS)
    p = config_path()
    if p.exists():
        cfg.update({k: v for k, v in json.loads(p.read_text()).items() if not k.startswith("_")})
    return cfg


CFG = load()
DATA_DIR = Path(CFG["data_dir"]).expanduser()
