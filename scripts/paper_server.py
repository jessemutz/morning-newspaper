#!/usr/bin/env python3
"""HTTP endpoint for the KOReader plugin (koreader/morningpaper.koplugin).

  POST /api/progress        reading snapshot from the Kindle -> reading/state.json
  GET  /api/manifest        today's and recent editions + books on the CWA "Kindle" shelf
  GET  /files/edition/NAME  an edition EPUB
  GET  /files/book/ID       a shelf book EPUB
  GET  /health              no auth

Every endpoint but /health needs "Authorization: Bearer $PAPER_TOKEN". Run it on the LAN or
Tailscale only. Stdlib only, so the container needs nothing extra.
"""
import hashlib
import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kobo_config import CFG, DATA_DIR  # noqa: E402
import koreader_state as ks  # noqa: E402
import epub_meta  # noqa: E402

EDITIONS = DATA_DIR / "editions"
TOKEN = os.environ.get("PAPER_TOKEN", "")
_SHA_CACHE: dict = {}


def sha256(path: Path) -> str:
    st = path.stat()
    key = (str(path), st.st_mtime, st.st_size)
    if key not in _SHA_CACHE:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        _SHA_CACHE[key] = h.hexdigest()
    return _SHA_CACHE[key]


def safe_filename(name: str) -> str:
    keep = "".join(c if c.isalnum() or c in " -_.,'()" else "_" for c in name).strip()
    return keep[:120] or "book"


def editions() -> list[Path]:
    eds = sorted(EDITIONS.glob("*.epub"), key=lambda p: p.name, reverse=True)
    return eds[: int(CFG["editions_keep"])]


def manifest() -> dict:
    items = []
    for p in editions():
        items.append({"id": f"edition:{p.name}", "kind": "edition", "folder": CFG["edition_title"],
                      "filename": p.name, "size": p.stat().st_size, "sha256": sha256(p),
                      "url": f"/files/edition/{quote(p.name)}"})  # names have spaces
    try:
        shelf = ks.shelf_books()
    except Exception as e:  # a library problem must not stop the paper from syncing
        sys.stderr.write(f"shelf lookup failed: {type(e).__name__}: {e}\n")
        shelf = []
    for b in shelf:
        fname = safe_filename(f"{b['title']} - {b['authors']}") + ".epub"
        path = epub_meta.prepared(b)  # library metadata + cover written in
        items.append({"id": f"book:{b['id']}", "kind": "book", "folder": "Books",
                      "filename": fname, "size": path.stat().st_size,
                      "sha256": sha256(path), "url": f"/files/book/{b['id']}"})
    return {"edition_prefix": CFG["edition_title"], "keep_editions": int(CFG["editions_keep"]),
            "items": items}


class Handler(BaseHTTPRequestHandler):
    server_version = "MorningPaper/1"

    def _authed(self) -> bool:
        got = self.headers.get("Authorization", "")
        if TOKEN and hmac.compare_digest(got, f"Bearer {TOKEN}"):
            return True
        self._send(401, {"error": "unauthorized"})
        return False

    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: "Path | None") -> None:
        if not path or not path.is_file():
            return self._send(404, {"error": "not found"})
        self.send_response(200)
        self.send_header("Content-Type", "application/epub+zip")
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        with path.open("rb") as f:
            while chunk := f.read(1 << 16):
                self.wfile.write(chunk)

    def do_GET(self):  # noqa: N802
        path = unquote(self.path.split("?", 1)[0])
        if path == "/health":
            return self._send(200, {"ok": True})
        if not self._authed():
            return
        if path == "/api/manifest":
            return self._send(200, manifest())
        if path.startswith("/files/edition/"):
            name = path.rsplit("/", 1)[1]
            match = next((p for p in editions() if p.name == name), None)  # no path traversal
            return self._send_file(match)
        if path.startswith("/files/book/"):
            try:
                bid = int(path.rsplit("/", 1)[1])
            except ValueError:
                return self._send(404, {"error": "not found"})
            match = next((epub_meta.prepared(b) for b in ks.shelf_books() if b["id"] == bid), None)
            return self._send_file(match)
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self._authed():
            return
        if self.path != "/api/progress":
            return self._send(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 2_000_000:
            return self._send(400, {"error": "bad length"})
        try:
            snap = json.loads(self.rfile.read(n))
        except ValueError:
            return self._send(400, {"error": "bad json"})
        state = ks.record_snapshot(snap)
        self._send(200, {"ok": True, "books": len(state)})

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")


def main() -> int:
    if not TOKEN:
        print("PAPER_TOKEN is not set; refusing to start (the plugin must authenticate).", file=sys.stderr)
        return 1
    EDITIONS.mkdir(parents=True, exist_ok=True)
    addr = (CFG["server_host"], int(CFG["server_port"]))
    print(f"morning-paper server on http://{addr[0]}:{addr[1]}  data_dir={DATA_DIR}")
    ThreadingHTTPServer(addr, Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
