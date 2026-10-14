#!/usr/bin/env python3
"""Small LAN-only static server for the Andy Engine Build Map."""

from __future__ import annotations

import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
LISTEN_ADDRESS = os.environ.get("ENGINE_MAP_LISTEN_ADDRESS", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("ENGINE_MAP_LISTEN_PORT", "18120"))


class EngineMapHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PROJECT_DIR), **kwargs)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'",
        )
        if self.path.split("?", 1)[0].endswith(".json"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:  # noqa: N802 - inherited HTTP method name
        if self.path.split("?", 1)[0] == "/healthz":
            payload = json.dumps({"status": "ok", "service": "andy-engine-build-map"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        super().do_GET()


if __name__ == "__main__":
    server = ThreadingHTTPServer((LISTEN_ADDRESS, LISTEN_PORT), EngineMapHandler)
    print(f"Andy Engine Build Map listening on http://{LISTEN_ADDRESS}:{LISTEN_PORT}", flush=True)
    server.serve_forever()
