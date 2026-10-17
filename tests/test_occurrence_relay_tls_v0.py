"""Loopback-only TLS identity proof for the Relay MAESTRO client.

All certificates and SQLite files are disposable. Does not contact AGT services.
"""
import json
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from tests.test_occurrence_relay_v0 import event, relay


class LocalReceiptHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        if self.path != "/api/v1/ingress/maestro/occurrences":
            self.send_error(404)
            return
        size = int(self.headers["Content-Length"])
        envelope = json.loads(self.rfile.read(size))
        self.server.seen.append(envelope)
        key = self.headers["Idempotency-Key"]
        was_accepted = key in self.server.accepted
        if was_accepted:
            assert self.server.accepted[key] == envelope
        else:
            self.server.accepted[key] = envelope
        if self.server.ack_lost_once:
            self.server.ack_lost_once = False
            self.close_connection = True
            self.connection.close()
            return
        response = json.dumps({
            "status": "DUPLICATE" if was_accepted else "ACCEPTED",
            "receipt_id": "synthetic-tls-receipt-1",
            "occurrence_number": envelope["occurrence_number"],
        }).encode("utf-8")
        self.send_response(200 if was_accepted else 202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, *_args):
        pass


@unittest.skipUnless(shutil.which("openssl"), "OpenSSL executable unavailable")
class LiveLoopbackTLSValidation(unittest.TestCase):
    def test_no_trust_bad_hostname_and_trusted_hostname(self):
        with tempfile.TemporaryDirectory() as root:
            cert = Path(root) / "server.pem"
            key = Path(root) / "server.key"
            subprocess.run([
                "openssl", "req", "-x509", "-newkey", "rsa:2048",
                "-nodes", "-sha256", "-days", "1", "-subj", "/CN=localhost",
                "-addext", "subjectAltName=DNS:localhost",
                "-keyout", str(key), "-out", str(cert),
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=12)
            server = HTTPServer(("127.0.0.1", 0), LocalReceiptHandler)
            tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            tls.minimum_version = ssl.TLSVersion.TLSv1_2
            tls.load_cert_chain(str(cert), str(key))
            server.socket = tls.wrap_socket(server.socket, server_side=True)
            server.seen = []
            server.accepted = {}
            server.ack_lost_once = False
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                store = relay.Store(str(Path(root) / "events.sqlite3"))
                row, digest = relay.validate(json.dumps(event()).encode())
                store.admit(row, digest)
                url = f"https://localhost:{server.server_port}/api/v1/ingress/maestro/occurrences"
                def app(target, ca=""):
                    return relay.App(store, "x" * 40, "synthetic-tenant", "zabbix",
                                     target, "y" * 40, ca)

                self.assertFalse(app(url).dispatch_one())
                self.assertEqual(store.counts()["pending_delivery"], 1)
                self.assertEqual(server.seen, [])

                wrong = url.replace("localhost", "127.0.0.1")
                self.assertFalse(app(wrong, str(cert)).dispatch_one())
                self.assertEqual(store.counts()["pending_delivery"], 1)
                self.assertEqual(server.seen, [])

                server.ack_lost_once = True
                self.assertFalse(app(url, str(cert)).dispatch_one())
                self.assertEqual(store.counts()["pending_delivery"], 1)
                self.assertEqual(len(server.accepted), 1)
                self.assertEqual(len(server.seen), 1)

                # Receiver accepted, but the TCP ACK response was lost. A
                # real client must retry the same key and accept 200 DUPLICATE.
                self.assertTrue(app(url, str(cert)).dispatch_one())
                self.assertEqual(store.counts()["pending_delivery"], 0)
                self.assertEqual(len(server.seen), 2)
                self.assertEqual(server.seen[0], server.seen[1])
                self.assertEqual(len(server.accepted), 1)
                self.assertEqual(server.seen[0]["occurrence_number"], 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
