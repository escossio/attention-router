"""Offline tests for CPU hot-core and Zabbix sender; no host mutation."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "ops/observability/functional-alerts/collect.py"
spec = importlib.util.spec_from_file_location("andy_ops_cpu_collector", MODULE)
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


class CpuAlertContract(unittest.TestCase):
    def test_parse_core_counters_and_delta(self):
        before = monitor.decode_cpu(
            "cpu0 12 1 2 50 0 0 0 0 0 0\n"
            "cpu1 25 0 2 50 0 0 0 0 0 0\n")
        after = {core: (total + 100, idle + 10) for core, (total, idle) in before.items()}
        self.assertEqual(monitor.percent(before, after), {0: 90.0, 1: 90.0})

    def test_one_hot_core_while_others_idle(self):
        metrics = monitor.cpu_metrics({0: 99.0, 1: 1, 2: 2, 3: 0})
        self.assertEqual(metrics["andy.cpu.hot_idle"], 1)
        self.assertEqual(metrics["andy.cpu.hot_id"], 0)
        self.assertEqual(metrics["andy.cpu.probe_ok"], 1)

    def test_legitimate_full_machine_load_not_idle(self):
        metrics = monitor.cpu_metrics({0: 99, 1: 90, 2: 95, 3: 88})
        self.assertEqual(metrics["andy.cpu.hot_idle"], 0)

    def test_no_high_core_not_alert(self):
        metrics = monitor.cpu_metrics({0: 22, 1: 34, 2: 5, 3: 2})
        self.assertEqual(metrics["andy.cpu.hot_idle"], 0)

    def test_host_keys_quoted_for_sender(self):
        lines = list(monitor.sender_lines({"Andy Engine - AGT01": {"andy.cpu.hot_idle": 0}}))
        self.assertEqual(lines, ['"Andy Engine - AGT01" andy.cpu.hot_idle 0'])


if __name__ == "__main__":
    unittest.main()
