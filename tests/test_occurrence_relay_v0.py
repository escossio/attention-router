"""Offline, dependency-free checks for the Zabbix occurrence relay."""
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

MODULE_PATH = Path(__file__).parents[1] / "ops" / "occurrence-relay" / "relay.py"
spec = importlib.util.spec_from_file_location("occurrence_relay_v0", MODULE_PATH)
relay = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = relay
spec.loader.exec_module(relay)


def event(**extra):
    data = {
        "schema_version": "1",
        "event_id": "evt-001",
        "occurred_at": "2026-10-17T10:00:00Z",
        "asset_ref": "synthetic:router-one",
        "signal_code": "PORT_DOWN",
        "status": "PROBLEM",
        "interface_ref": "eth-synthetic",
        "severity": 4,
    }
    data.update(extra)
    return data


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dbfile = str(Path(self.tmp.name) / "data" / "relay.db")
        self.store = relay.Store(self.dbfile)

    def tearDown(self):
        self.tmp.cleanup()

    def admit(self, data):
        raw = json.dumps(data, separators=(",", ":")).encode()
        record, digest = relay.validate(raw)
        return self.store.admit(record, digest)

    def test_number_replay_persistence_and_conflict(self):
        self.assertEqual(self.admit(event()), (1, True))
        self.assertEqual(self.admit(event()), (1, False))
        self.assertEqual(relay.Store(self.dbfile).counts(), {"events": 1, "pending_delivery": 1})
        with self.assertRaises(ValueError):
            self.admit(event(severity=5))
        self.assertEqual(self.admit(event(event_id="evt-002")), (2, True))

    def test_recovery_is_new_record_with_parent(self):
        self.admit(event())
        self.assertEqual(self.admit(event(
            event_id="evt-rec", status="RECOVERY", problem_event_id="evt-001"
        )), (2, True))

    def test_concurrent_retry_does_not_duplicate(self):
        numbers = []
        lock = threading.Lock()
        def write():
            result = self.admit(event())
            with lock:
                numbers.append(result)
        threads = [threading.Thread(target=write) for _ in range(9)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual({number for number, _ in numbers}, {1})
        self.assertEqual(sum(1 for _, created in numbers if created), 1)

    def test_validation_rejects_unknown_duplicates_naive_time(self):
        samples = [
            b'{"event_id":"1","event_id":"2"}',
            json.dumps(event(unexpected="new")).encode(),
            json.dumps(event(occurred_at="2026-10-17T12:00:00")).encode(),
            json.dumps(event(severity=True)).encode(),
            json.dumps(event(status="RECOVERY")).encode(),
        ]
        for raw in samples:
            with self.subTest(raw=raw[:50]), self.assertRaises(ValueError):
                relay.validate(raw)

    def test_outbound_disabled_preserves_pending(self):
        self.admit(event())
        app = relay.App(self.store, "x"*40, "synthetic-tenant", "zabbix")
        self.assertFalse(app.dispatch_one())
        self.assertEqual(self.store.counts()["pending_delivery"], 1)

    def test_dispatch_schema_and_idempotent_key(self):
        self.admit(event())
        app = relay.App(self.store, "x"*40, "synthetic-tenant", "zabbix",
                        "https://maestro.example.invalid/v1/events", "y"*40)
        class Resp:
            status = 202
            def getheader(self, name, default=""):
                return "application/json" if name.lower() == "content-type" else default
            def read(self, limit=-1):
                body = json.dumps({
                    "status": "ACCEPTED",
                    "receipt_id": "synthetic-receipt-1",
                    "occurrence_number": 1,
                }).encode()
                return body[:limit]
            def __enter__(self): return self
            def __exit__(self, *_): return False
        with patch.object(relay, "build_opener") as opener:
            opener.return_value.open.return_value = Resp()
            self.assertTrue(app.dispatch_one())
        self.assertEqual(self.store.counts()["pending_delivery"], 0)
        args = opener.return_value.open.call_args[0][0]
        self.assertEqual(args.headers["Idempotency-key"], "zabbix:1")
        body = json.loads(args.data)
        self.assertEqual(body["occurrence_number"], 1)
        self.assertEqual(body["signal"]["asset_ref"], "synthetic:router-one")


class HttpTests(StoreTests):
    def setUp(self):
        super().setUp()
        app = relay.App(self.store, "x"*40, "synthetic-tenant", "zabbix")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), relay.handler_factory(app))
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/zabbix/events"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=3)
        super().tearDown()

    def request(self, token=None, data=None):
        req = Request(self.url, data=json.dumps(data or event()).encode("utf-8"),
                      method="POST",
                      headers={"Content-Type": "application/json", **({} if token is None else {"Authorization": "Bearer "+token})})
        try:
            with urlopen(req, timeout=3) as r:
                return r.status, json.load(r)
        except HTTPError as err:
            return err.code, json.loads(err.read())

    def test_unauthenticated_is_denied_without_write(self):
        self.assertEqual(self.request()[0], 401)
        self.assertEqual(self.store.counts()["events"], 0)

    def test_post_receipts_and_idempotence(self):
        self.assertEqual(self.request("x"*40)[0], 201)
        status, result = self.request("x"*40)
        self.assertEqual(status, 200)
        self.assertEqual(result["occurrence_number"], 1)

    def test_api_rejects_conflict_without_side_effect(self):
        self.assertEqual(self.request("x"*40)[0], 201)
        self.assertEqual(self.request("x"*40, event(severity=0))[0], 422)
        self.assertEqual(self.store.counts()["events"], 1)

    def raw_post(self, custom_headers):
        body = json.dumps(event()).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.putrequest("POST", "/v1/zabbix/events")
            connection.putheader("Authorization", "Bearer " + "x" * 40)
            connection.putheader("Content-Type", "application/json")
            for key, value in custom_headers:
                connection.putheader(key, value)
            connection.endheaders(body)
            response = connection.getresponse()
            status = response.status
            response.read()
            return status
        finally:
            connection.close()

    def test_duplicate_length_cannot_choose_first(self):
        body_length = len(json.dumps(event()).encode("utf-8"))
        self.assertEqual(self.raw_post([
            ("Content-Length", str(body_length)),
            ("Content-Length", str(body_length - 1)),
        ]), 400)
        self.assertEqual(self.store.counts()["events"], 0)

    def test_transfer_encoding_with_length_is_rejected(self):
        body_length = len(json.dumps(event()).encode("utf-8"))
        self.assertEqual(self.raw_post([
            ("Transfer-Encoding", "chunked"),
            ("Content-Length", str(body_length)),
        ]), 400)
        self.assertEqual(self.store.counts()["events"], 0)

    def test_duplicate_auth_header_is_rejected(self):
        body_length = len(json.dumps(event()).encode("utf-8"))
        self.assertEqual(self.raw_post([
            ("Authorization", "Bearer " + "x" * 40),
            ("Content-Length", str(body_length)),
        ]), 400)
        self.assertEqual(self.store.counts()["events"], 0)

    def test_disallowed_content_type_is_rejected(self):
        payload = json.dumps(event()).encode()
        request = Request(self.url, data=payload, method="POST",
                          headers={"Authorization":"Bearer " + "x"*40,
                                   "Content-Type":"text/plain"})
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=3)
        self.assertEqual(raised.exception.code, 400)
        self.assertEqual(self.store.counts()["events"], 0)


if __name__ == "__main__":
    unittest.main()
