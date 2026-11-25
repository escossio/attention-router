"""Offline tests of the actual OpsHandler, not the host sampler."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "ops/provisioning/andy-ops-panel"


def load_panel():
    sys.path.insert(0, str(PANEL))
    try:
        spec = importlib.util.spec_from_file_location(
            "tcpbrain_mgmt_http_server", PANEL / "server.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(PANEL))


class HttpTests(unittest.TestCase):
    def test_projection_and_existing_routes(self):
        panel = load_panel()
        payload = {"schema_version": "tcpbrain.mgmt.v1", "sources": {}, "flow": {}}
        with patch.object(panel, "sample_tcpbrain", return_value=payload) as sample:
            http = ThreadingHTTPServer(("127.0.0.1", 0), panel.OpsHandler)
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{http.server_port}"
            try:
                for path in (
                    "/healthz",
                    "/api/config",
                    "/api/status",
                    "/api/tcpbrain/status?path=/etc/passwd",
                    "/tcpbrain-mgmt.js",
                    "/tcpbrain-mgmt.css",
                    "/",
                ):
                    with urlopen(base + path, timeout=3) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                        self.assertIn("no-store", response.headers["Cache-Control"])
                        self.assertIn(
                            "script-src 'self'", response.headers["Content-Security-Policy"]
                        )
                        raw = response.read()
                        if path.startswith("/api/tcpbrain/status"):
                            self.assertEqual(json.loads(raw), payload)
                        elif path == "/":
                            self.assertIn(b'id="tcpbrain-panel"', raw)
                            self.assertIn(b'id="network-frame"', raw)
                            self.assertIn(b'data-tab="compute"', raw)
                sample.assert_called_once_with()
            finally:
                http.shutdown()
                http.server_close()
                thread.join(timeout=2)
