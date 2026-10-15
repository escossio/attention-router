"""Verify that the filtered Docker view never creates unbounded threads."""
import importlib.util
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "ops/observability/dozzle/proxy.py"
spec = importlib.util.spec_from_file_location("bounded_dozzle_proxy", SOURCE)
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


class SlowHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    entered = threading.Event()
    release = threading.Event()

    def do_GET(self):
        if self.path == "/hold":
            self.entered.set()
            self.release.wait(timeout=4)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args):
        pass


def request(path, socket_path):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(4)
        connection.connect(socket_path)
        connection.sendall(f"GET {path} HTTP/1.0\r\nHost: localhost\r\n\r\n".encode())
        return connection.recv(300).split(b"\r\n", 1)[0]


class DozzleBoundedConcurrency(unittest.TestCase):
    def test_at_capacity_rejects_and_recovers(self):
        SlowHandler.entered.clear()
        SlowHandler.release.clear()
        with tempfile.TemporaryDirectory() as temporary:
            socket_path = str(Path(temporary) / "docker.sock")
            with proxy.Server(socket_path, SlowHandler) as server:
                server._capacity = threading.BoundedSemaphore(1)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                result = []
                first = threading.Thread(
                    target=lambda: result.append(request("/hold", socket_path)), daemon=True
                )
                try:
                    first.start()
                    self.assertTrue(SlowHandler.entered.wait(timeout=2))
                    self.assertIn(b"503", request("/_ping", socket_path))
                    SlowHandler.release.set()
                    first.join(timeout=3)
                    self.assertEqual(len(result), 1)
                    self.assertIn(b"200", result[0])
                    self.assertIn(b"200", request("/_ping", socket_path))
                finally:
                    SlowHandler.release.set()
                    server.shutdown()
                    thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
