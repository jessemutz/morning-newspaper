#!/usr/bin/env python3
"""Download the cover/front-page fonts into fonts_dir if they are missing.

UnifrakturMaguntia (the blackletter masthead) and PT Serif are both under the SIL Open Font
License and served from the Google Fonts repository. Runs as the first step of run_daily.py;
safe to run any time (it skips fonts that are already there).
"""
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kobo_config import CFG, DATA_DIR  # noqa: E402

BASE = "https://github.com/google/fonts/raw/main/ofl"
SOURCES = {
    "UnifrakturMaguntia-Book.ttf": f"{BASE}/unifrakturmaguntia/UnifrakturMaguntia-Book.ttf",
    "PT_Serif-Web-Regular.ttf": f"{BASE}/ptserif/PT_Serif-Web-Regular.ttf",
    "PT_Serif-Web-Bold.ttf": f"{BASE}/ptserif/PT_Serif-Web-Bold.ttf",
}


def fonts_dir() -> Path:
    d = Path(CFG["fonts_dir"]).expanduser()
    return d if d.is_absolute() else DATA_DIR / d


def main() -> int:
    d = fonts_dir()
    d.mkdir(parents=True, exist_ok=True)
    wanted = {CFG["masthead_font"], CFG["serif_bold_font"], CFG["serif_regular_font"]}
    for name in sorted(wanted):
        dest = d / name
        if dest.exists() and dest.stat().st_size > 10_000:
            continue
        url = SOURCES.get(name)
        if not url:
            print(f"fonts: no download source for {name}; put the TTF in {d} yourself")
            continue
        try:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            dest.write_bytes(r.content)
            print(f"fonts: downloaded {name}")
        except requests.RequestException as e:
            print(f"fonts: {name} failed ({e}); the build falls back to a plain font")
    return 0


if __name__ == "__main__":
    sys.exit(main())
