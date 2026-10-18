"""Synthetic regression for embedding AR-1001 into the single existing probe."""
from pathlib import Path
import unittest

from ops.observability.failure_catalog.ar1001_reply_health_build import build


ROOT = Path(__file__).resolve().parents[1]
SQL = ROOT / "ops/observability/failure-catalog/queries/ar-1001-integrated-readonly.v0.sql"
PROJECTION = ROOT / "ops/observability/failure_catalog/ar1001_metric_projection.py"
LEGACY = '''SQL = r"""BEGIN READ ONLY;
SELECT 1;
COMMIT;
"""

def probe():
    result = run(SQL)
    rows = [line for line in result.stdout.splitlines() if line]
    if len(rows) != 1:
        raise RuntimeError("bad rows")
    return {"direct_6h": int(rows[0])}


def next_state(previous, evidence, now):
    return previous

def sender_lines(metrics):
    return str(metrics)

def send(metrics):
    return metrics

def load_state():
    return {"active": True, "opened_at": 42}

def save_state(state):
    return state

def main():
    try:
        evidence = probe()
        current = next_state(load_state(), evidence, 100)
    except Exception:
        send({"andy.reply.probe_ok": 0})
        return
    metrics = {"andy.reply.probe_ok": 1}
    reply = send(metrics)
    save_state(current)
    return reply
'''


class Ar1001ProbeBuilderTests(unittest.TestCase):
    def test_embedded_probe_publishes_valid_row_and_fails_closed(self):
        source = build(LEGACY, SQL.read_text(), PROJECTION.read_text())
        calls = []
        class Result:
            stdout = "11|11|11|0|120|90|110|11|11|11|11|0|6|5\n"
        namespace = {"run": lambda query: Result()}
        exec(compile(source, "synthetic-ar1001-probe", "exec"), namespace)
        namespace["send"] = lambda values: calls.append(values.copy()) or values
        metrics = namespace["main"]()
        self.assertEqual(len(metrics), 8)  # minimal synthetic legacy fixture + 7 new
        self.assertEqual(metrics["andy.reply.grace_canceled_6h"], 5)
        self.assertEqual(metrics["andy.reply.probe_ok"], 1)
        calls.clear()
        Result.stdout = "invalid|row\n"
        namespace["main"]()
        self.assertEqual(calls, [{"andy.reply.probe_ok": 0}])

    def test_guard_rejects_unguarded_sql(self):
        with self.assertRaisesRegex(ValueError, "SQL_GUARD_MISSING"):
            build(LEGACY, "SELECT 1;", PROJECTION.read_text())


if __name__ == "__main__":
    unittest.main()
