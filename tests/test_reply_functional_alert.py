"""Contract: real functional incident stays open until a genuine automatic reply."""
import importlib.util
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / (
    "ops/observability/reply-health/reply_health.py"
)
spec = importlib.util.spec_from_file_location("reply_health", SOURCE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def sample(**kwargs):
    result = {
        "direct_6h": 4, "no_safe_6h": 3, "blueprint_6h": 3,
        "other_failed_6h": 0, "latest_failure_ts": 10000,
        "last_agent_delivery_ts": 100,
        "latest_direct_ts": 10001,
    }
    result.update(kwargs)
    return result


class ReplyAlertContract(unittest.TestCase):
    def test_stall_when_real_direct_failures_with_no_automatic_reply(self):
        s = mod.next_state({"active": False, "opened_at": 0}, sample(), 10010)
        self.assertEqual(s, {"active": True, "opened_at": 10010})

    def test_empty_traffic_does_not_open_new_incident(self):
        s = mod.next_state({"active": False, "opened_at": 0},
                           sample(direct_6h=0, no_safe_6h=0,
                                  blueprint_6h=0), 10010)
        self.assertFalse(s["active"])

    def test_old_done_row_does_not_clear_incident(self):
        s = mod.next_state({"active": True, "opened_at": 10010},
                           sample(direct_6h=0, no_safe_6h=0,
                                  blueprint_6h=0, latest_failure_ts=0), 90000)
        self.assertTrue(s["active"])

    def test_time_passage_never_resolves(self):
        s = mod.next_state({"active": True, "opened_at": 10010},
                           sample(direct_6h=0, no_safe_6h=0, blueprint_6h=0,
                                  latest_failure_ts=0, last_agent_delivery_ts=0),
                           999999)
        self.assertTrue(s["active"])

    def test_new_automatic_done_after_error_resolves(self):
        s = mod.next_state({"active": True, "opened_at": 10010},
                           sample(latest_failure_ts=10012,
                                  last_agent_delivery_ts=10015), 10018)
        self.assertEqual(s, {"active": False, "opened_at": 0})

    def test_reply_before_later_failure_does_not_resolve(self):
        s = mod.next_state({"active": True, "opened_at": 10010},
                           sample(latest_failure_ts=10025,
                                  last_agent_delivery_ts=10020), 10028)
        self.assertTrue(s["active"])

    def test_future_error_type_triggers_same_general_alarm(self):
        s = mod.next_state({"active": False, "opened_at": 0},
                           sample(no_safe_6h=0, blueprint_6h=0,
                                  other_failed_6h=2), 10010)
        self.assertTrue(s["active"])

    def test_source_does_not_emit_message_text(self):
        self.assertIn("agent_execution_text", mod.SQL)
        self.assertIn("agent_execution_voice", mod.SQL)
        self.assertIn("max(o.completed_at)", mod.SQL)
        self.assertIn("o.completed_at IS NOT NULL", mod.SQL)
        self.assertNotIn("max(o.created_at) AS last_delivery", mod.SQL)
        self.assertIn("payload->>'actor_id'", mod.SQL)
        self.assertNotIn("SELECT payload", mod.SQL)
        self.assertIn("BEGIN READ ONLY", mod.SQL)


if __name__ == "__main__":
    unittest.main()
