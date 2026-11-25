#!/usr/bin/env python3
"""The morning run: interests -> candidates -> curation -> book companion -> edition.

Each step runs as its own process; a failed step is logged and the run continues, so a
sleeping Mac (no LLM) or a dead feed still produces a paper. Exit code is the build's.
"""
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
STEPS = [
    ("fonts", "fetch_fonts.py"),
    ("interests", "kobo-interests.py"),
    ("gather", "gather_candidates.py"),
    ("curate", "curate.py"),
    ("companion", "book_companion.py"),
    ("sports", "sports.py"),
    ("recommend", "recommend.py"),
    ("build", "kobo-digest-build.py"),
]


def main() -> int:
    rc = 0
    for name, script in STEPS:
        t = time.time()
        print(f"{datetime.now():%F %T} === {name} ===", flush=True)
        rc = subprocess.run([sys.executable, str(HERE / script)]).returncode
        print(f"{datetime.now():%F %T} === {name} exit {rc} ({time.time() - t:.0f}s) ===", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
