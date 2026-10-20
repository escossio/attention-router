"""Synthetic MAESTRO receive/ACK loss protocol checks. No real endpoint or credentials."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from tests.test_occurrence_relay_v0 import event, relay


class Response:
    status = 202

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
        if key in self.accepted:
            assert self.accepted[key] == envelope, "idempotency conflict"
        else:
            # Pretend the consumer durably committed once.
            self.accepted[key] = envelope
        if self.drop_ack_once:
            self.drop_ack_once = False
            raise URLError("synthetic ACK lost after durable commit")
        return Response()


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

    def test_redirect_policy_is_explicitly_disabled(self):
        self.assertIsNone(relay.NoRedirect().redirect_request(None, None, 302, "", {}, ""))


if __name__ == "__main__":
    unittest.main()
