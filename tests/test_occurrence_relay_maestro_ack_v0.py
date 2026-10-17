"""Synthetic MAESTRO receive/ACK loss protocol checks. No real endpoint or credentials."""
import json
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import HTTPSHandler, ProxyHandler

from tests.test_occurrence_relay_v0 import event, relay


class Response:
    def __init__(self, status=202, payload=None, *, content_type="application/json", raw=None):
        self.status = status
        self.content_type = content_type
        self.body = json.dumps(payload if payload is not None else {}).encode() if raw is None else raw

    def getheader(self, name, default=""):
        return self.content_type if name.lower() == "content-type" else default

    def read(self, limit=-1):
        return self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class ContractReceiver:
    """In-memory fake receiver that models durability and idempotency only."""

    def __init__(self):
        self.accepted = {}
        self.calls = []
        self.drop_ack_once = False
        self.unavailable = False

    def open(self, request, timeout):
        if request.full_url != "https://maestro.example.invalid/v1/events":
            raise AssertionError("unexpected endpoint")
        assert timeout == 4
        assert request.headers["Content-type"] == "application/json"
        assert request.headers["Authorization"] == "Bearer " + "y" * 40

        key = request.headers["Idempotency-key"]
        envelope = json.loads(request.data)
        assert envelope["contract_version"] == "occurrence-relay-v0-draft"
        assert envelope["source_system"] == "zabbix"
        assert envelope["tenant_id"] == "synthetic-tenant"
        assert key == "zabbix:" + str(envelope["occurrence_number"])
        self.calls.append(key)

        if self.unavailable:
            raise URLError("synthetic unavailable")
        duplicate = key in self.accepted
        if duplicate:
            assert self.accepted[key] == envelope, "idempotency conflict"
        else:
            # Pretend the consumer durably committed once.
            self.accepted[key] = envelope
        if self.drop_ack_once:
            self.drop_ack_once = False
            raise URLError("synthetic ACK lost after durable commit")
        return Response(
            200 if duplicate else 202,
            {"status": "DUPLICATE" if duplicate else "ACCEPTED",
             "receipt_id": "synthetic-receipt-" + key,
             "occurrence_number": envelope["occurrence_number"]},
        )


class DeliveryContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "relay.db")
        self.store = relay.Store(self.db_path)
        self.receiver = ContractReceiver()

    def tearDown(self):
        self.tmp.cleanup()

    def admit(self, item):
        encoded = json.dumps(item, sort_keys=True).encode()
        data, digest = relay.validate(encoded)
        return self.store.admit(data, digest)

    def app(self):
        return relay.App(
            self.store,
            "x" * 40,
            "synthetic-tenant",
            "zabbix",
            "https://maestro.example.invalid/v1/events",
            "y" * 40,
        )

    def send(self, app):
        with patch.object(relay, "build_opener") as opener:
            opener.return_value.open = self.receiver.open
            return app.dispatch_one()

    def test_receiver_down_never_discards_pending_event(self):
        self.admit(event())
        self.receiver.unavailable = True
        self.assertFalse(self.send(self.app()))
        self.assertEqual(self.store.counts(), {"events": 1, "pending_delivery": 1})
        self.assertEqual(self.receiver.accepted, {})
        self.receiver.unavailable = False
        self.assertTrue(self.send(self.app()))
        self.assertEqual(self.store.counts(), {"events": 1, "pending_delivery": 0})
        self.assertEqual(len(self.receiver.accepted), 1)

    def test_lost_ack_after_accept_retries_same_key_without_duplicate_job(self):
        self.admit(event())
        self.receiver.drop_ack_once = True
        self.assertFalse(self.send(self.app()))
        self.assertEqual(self.store.counts()["pending_delivery"], 1)
        self.assertEqual(len(self.receiver.accepted), 1)
        # Reopen backing database as if the relay process had restarted.
        self.store = relay.Store(self.db_path)
        self.assertTrue(self.send(self.app()))
        self.assertEqual(self.receiver.calls, ["zabbix:1", "zabbix:1"])
        self.assertEqual(len(self.receiver.accepted), 1)
        self.assertEqual(self.store.counts()["pending_delivery"], 0)

    def test_order_and_distinct_recovery_after_restart(self):
        self.admit(event(event_id="problem:100"))
        self.admit(event(event_id="recovery:101", status="RECOVERY",
                         problem_event_id="problem:100"))
        self.store = relay.Store(self.db_path)
        self.assertTrue(self.send(self.app()))
        self.assertTrue(self.send(self.app()))
        self.assertFalse(self.send(self.app()))
        self.assertEqual(self.receiver.calls, ["zabbix:1", "zabbix:2"])
        self.assertEqual(
            self.receiver.accepted["zabbix:2"]["signal"]["problem_event_id"], "problem:100"
        )
        self.assertEqual(self.store.counts(), {"events": 2, "pending_delivery": 0})

    def test_retry_after_no_endpoint_configuration_is_disabled(self):
        self.admit(event())
        relay_app = relay.App(self.store, "x" * 40, "synthetic-tenant", "zabbix")
        with patch.object(relay, "build_opener", side_effect=AssertionError("unexpected call")):
            self.assertFalse(relay_app.dispatch_one())
        self.assertEqual(self.store.counts()["pending_delivery"], 1)

    def test_invalid_or_ambiguous_http_ack_keeps_event_pending(self):
        self.admit(event())
        correct = {"status": "ACCEPTED", "receipt_id": "receipt-1", "occurrence_number": 1}
        malformed = [
            ("proxy_html_200", Response(200, raw=b"<html>OK</html>", content_type="text/html")),
            ("empty_204", Response(204, correct)),
            ("wrong_status_200", Response(200, correct)),
            ("wrong_status_202", Response(202, {**correct, "status": "DUPLICATE"})),
            ("wrong_number", Response(202, {**correct, "occurrence_number": 2})),
            ("boolean_number", Response(202, {**correct, "occurrence_number": True})),
            ("missing_receipt", Response(202, {**correct, "receipt_id": None})),
            ("blank_receipt", Response(202, {**correct, "receipt_id": "  "})),
            ("oversized_receipt", Response(202, {**correct, "receipt_id": "x" * 129})),
            ("extra_key", Response(202, {**correct, "note": "ignored?"})),
            ("duplicate_json_key", Response(202, raw=b'{"status":"ACCEPTED","status":"DUPLICATE","receipt_id":"x","occurrence_number":1}')),
            ("malformed_json", Response(202, raw=b"{not-json}")),
            ("invalid_utf8", Response(202, raw=b"\\xff")),
            ("oversized_body", Response(202, raw=b"x" * (relay.MAX_ACK_BYTES + 1))),
            ("wrong_content_type", Response(202, correct, content_type="text/html")),
        ]
        for name, response in malformed:
            with self.subTest(name=name):
                with patch.object(relay, "build_opener") as opener:
                    opener.return_value.open.return_value = response
                    self.assertFalse(self.app().dispatch_one())
                self.assertEqual(self.store.counts()["pending_delivery"], 1, name)
        self.assertTrue(self.send(self.app()))
        self.assertEqual(self.store.counts()["pending_delivery"], 0)

    def test_receiver_duplicate_receipt_is_reusable_after_lost_ack(self):
        self.admit(event())
        self.receiver.drop_ack_once = True
        self.assertFalse(self.send(self.app()))
        self.assertTrue(self.send(self.app()))
        self.assertEqual(self.store.counts()["pending_delivery"], 0)
        self.assertEqual(self.receiver.calls, ["zabbix:1", "zabbix:1"])

    def test_tls_default_context_and_redirect_guard_remain_enforced(self):
        self.admit(event())
        app = self.app()
        self.assertTrue(app.maestro_tls_context.check_hostname)
        self.assertEqual(app.maestro_tls_context.verify_mode, ssl.CERT_REQUIRED)
        with patch.object(relay, "build_opener") as opener:
            opener.return_value.open = self.receiver.open
            self.assertTrue(app.dispatch_one())
            handlers = opener.call_args.args
            self.assertTrue(any(isinstance(h, relay.NoRedirect) for h in handlers))
            proxies = [h for h in handlers if isinstance(h, ProxyHandler)]
            self.assertEqual(len(proxies), 1)
            self.assertEqual(proxies[0].proxies, {})
            tls = [h for h in handlers if isinstance(h, HTTPSHandler)]
            self.assertEqual(len(tls), 1)
            self.assertIs(tls[0]._context, app.maestro_tls_context)

    def test_invalid_private_ca_refuses_to_start_outbound(self):
        nonexistent = str(Path(self.tmp.name) / "nonexistent-private-ca.pem")
        with self.assertRaises(OSError):
            relay.App(self.store, "x" * 40, "synthetic-tenant", "zabbix",
                      "https://maestro.example.invalid/v1/events", "y" * 40,
                      nonexistent)

    def test_plain_http_destination_cannot_be_enabled(self):
        with self.assertRaises(ValueError):
            relay.App(self.store, "x" * 40, "synthetic-tenant", "zabbix",
                      "http://maestro.example.invalid/v1/events", "y" * 40)

    def test_redirect_policy_is_explicitly_disabled(self):
        self.assertIsNone(relay.NoRedirect().redirect_request(None, None, 302, "", {}, ""))


if __name__ == "__main__":
    unittest.main()
