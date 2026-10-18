"""AR-1001 MGMT candidate: sanitized Zabbix projection and additive panel patch."""
from __future__ import annotations

from pathlib import Path
import importlib.util
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "ops/provisioning/andy-ops-panel"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SRC / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mon = load("ar1001_observability")
stage = load("ar1001_stage")


def raw(now=1792296000, *, stale=False):
    stamp = now - (720 if stale else 60)
    rows = [
        f"M|{key}|{stamp}|{1 if key == 'andy.reply.probe_ok' else 1 if key == 'andy.reply.functional_stalled' else 12}"
        for key in mon.EXPECTED_KEYS
    ]
    rows += [
        "T|ANDY_WA_AUTO_REPLY_STALLED|1",
        "T|ANDY_WA_REPLY_OBSERVABILITY_LOST|0",
    ]
    return "\n".join(rows)


class CardProjectionTests(unittest.TestCase):
    def test_all_15_metrics_are_numeric_and_fresh(self):
        p = mon.project(raw(), now=1792296000)
        self.assertEqual(p["status"], "OBSERVED_FAILURE")
        self.assertEqual(len(p["metrics"]), 15)
        self.assertEqual(len(p["facets"]), 11)
        self.assertEqual(p["metrics"]["andy.reply.probe_ok"]["value"], 1)
        for f in p["facets"]:
            if f["dimension"] in ("network", "physical", "data_link", "tracing"):
                self.assertEqual(f["state"], "NOT_OBSERVED")
            if f["dimension"] == "delivery":
                self.assertEqual(f["state"], "UNKNOWN")
        self.assertEqual(p["source"], "ROC_ZABBIX_READONLY")

    def test_stale_probe_fails_closed_without_false_green(self):
        p = mon.project(raw(stale=True), now=1792296000)
        self.assertEqual(p["status"], "UNKNOWN")
        self.assertEqual(p["reason_code"], "PROBE_STALE_OR_UNAVAILABLE")
        self.assertFalse(p["metrics"]["andy.reply.probe_ok"]["fresh"])
        self.assertEqual(p["facets"][0]["state"], "UNKNOWN")

    def test_probe_down_invalidates_all_dependent_metric_freshness(self):
        record = raw().replace("M|andy.reply.probe_ok|1792295940|1",
                               "M|andy.reply.probe_ok|1792295940|0")
        p = mon.project(record, now=1792296000)
        self.assertEqual(p["status"], "UNKNOWN")
        self.assertFalse(p["metrics"]["andy.reply.functional_stalled"]["fresh"])
        self.assertFalse(p["metrics"]["andy.reply.direct_processed_6h"]["fresh"])

    def test_absent_probe_fails_closed(self):
        p = mon.project("\n".join(x for x in raw().splitlines()
                                  if "andy.reply.probe_ok" not in x), now=1792296000)
        self.assertEqual(p["status"], "UNKNOWN")
        self.assertEqual(p["reason_code"], "PROBE_STALE_OR_UNAVAILABLE")

    def test_exotic_data_and_unexpected_trigger_rejected(self):
        full = raw()
        for fragment in [
            full.replace("|12", "|-1", 1),
            full.replace("M|andy.reply.probe_ok", "M|andy.reply.actor_id", 1),
            full + "\nM|andy.reply.probe_ok|1792295900|1",
            full.replace("T|ANDY_WA_AUTO_REPLY_STALLED|1", "T|OTHER|1"),
            full.replace("T|ANDY_WA_AUTO_REPLY_STALLED|1", ""),
        ]:
            with self.subTest(fragment=fragment[-40:]):
                with self.assertRaises(ValueError):
                    mon.project(fragment, now=1792296000)

    def test_failed_subprocess_yields_unknown_not_metrics(self):
        def fail(*_, **__):
            raise subprocess.TimeoutExpired("docker", 4)
        result = mon.sample(run=fail, now_fn=lambda: 1792296000)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["metrics"], {})

    def test_sql_is_bounded_read_only_and_fixed_source(self):
        self.assertIn("BEGIN TRANSACTION READ ONLY", mon.SQL)
        self.assertIn("statement_timeout = '2s'", mon.SQL)
        self.assertIn("history_uint", mon.SQL)
        self.assertEqual(mon.DB_CMD[0:2], ("docker", "exec"))
        self.assertIn("timeout=4", (SRC/"ar1001_observability.py").read_text())
        for verb in ("UPDATE ", "DELETE ", "INSERT ", "DROP ", "ALTER "):
            self.assertNotIn(verb, mon.SQL)

    def test_additive_patch_preserves_existing_tabs(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            h = folder/"index.html"
            py = folder/"server.py"
            h.write_text('''<html><head></head><body><nav>
    <button class="tab" data-tab="compute" type="button">COMPUTE</button>
    <button class="tab" data-tab="chat" type="button">CHAT / CONSOLE</button>
    <button class="tab" data-tab="investigator" type="button">INVESTIGADOR</button>
    </nav><main>
    <section id="compute-view" class="view"></section>
    </main></body></html>''')
            py.write_text('''from message_tracing import sample_message_traces
class Test:
    def do_GET(self):
        if path == "/api/config":
            pass
''')
            report = stage.stage(folder)
            self.assertEqual(report["original_tabs"], 3)
            self.assertEqual(report["candidate_tabs"], 4)
            self.assertTrue((folder/"ar1001-card.js").is_file())
            self.assertIn('"/api/ar1001"', py.read_text())
            self.assertIn('data-tab="investigator"', h.read_text())
            with self.assertRaisesRegex(ValueError, "ALREADY_INSTALLED"):
                stage.stage(folder)


if __name__ == "__main__":
    unittest.main()
