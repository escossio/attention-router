"""Unit tests for strict Zabbix Server passive source authorization."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch
import tempfile
import unittest

FILE = Path(__file__).parents[1] / "ops/observability/docker-inventory/reconcile_agent2_origin.py"
SPEC = importlib.util.spec_from_file_location("reconcile", FILE)
reconcile = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reconcile)
CID = "a" * 64


def server(state="running", project="attention-router-roc-e2e", service="roc-zabbix-server"):
    return {"Id": CID, "State": state,
            "Labels": {"com.docker.compose.project": project,
                       "com.docker.compose.service": service}}


def inspection(ip="192.168.16.4", status="running", project="attention-router-roc-e2e"):
    return {"Id": CID, "State": {"Status": status},
            "Config": {"Labels": {"com.docker.compose.project": project,
                                  "com.docker.compose.service": "roc-zabbix-server"}},
            "NetworkSettings": {"Networks": {
                "andy-roc-edge": {"IPAddress": ip, "Gateway": "192.168.16.1"},
                "andy-roc-monitoring": {"IPAddress": "172.30.0.10", "Gateway": ""},
            }}}


class OriginReconciliationTests(unittest.TestCase):
    def test_exact_unique_identity_and_edge(self):
        self.assertEqual(reconcile.allowed_origin([server()], inspection()), "192.168.16.4")

    def test_wrong_compose_owner_and_ambiguous_identity(self):
        with self.assertRaises(RuntimeError):
            reconcile.allowed_origin([server(project="other")], inspection())
        with self.assertRaises(RuntimeError):
            reconcile.allowed_origin([server(), server()], inspection())
        with self.assertRaises(RuntimeError):
            reconcile.allowed_origin([server(state="exited")], inspection())

    def test_wrong_network_or_routing_refused(self):
        for ip in ("192.168.88.4", "172.30.0.12", "0.0.0.0", "bad"):
            with self.assertRaises((ValueError, RuntimeError)):
                reconcile.allowed_origin([server()], inspection(ip=ip))
        item = inspection()
        item["NetworkSettings"]["Networks"]["andy-roc-monitoring"]["Gateway"] = "172.30.0.1"
        with self.assertRaises(RuntimeError):
            reconcile.allowed_origin([server()], item)

    def test_only_explicit_single_ip_not_subnet(self):
        txt = "StartAgents=3\nServer=127.0.0.1,192.168.16.5\nServerActive=127.0.0.1\n"
        updated, changed = reconcile.proposed_config(txt, "192.168.16.4")
        self.assertTrue(changed)
        self.assertIn("Server=127.0.0.1,192.168.16.4\n", updated)
        self.assertIn("ServerActive=127.0.0.1\n", updated)
        self.assertEqual(reconcile.proposed_config(updated, "192.168.16.4"), (updated, False))
        lockdown, changed = reconcile.proposed_config(updated, None)
        self.assertTrue(changed)
        self.assertEqual(reconcile.current_origin(lockdown), "127.0.0.1")

    def test_refuse_unmanaged_existing_allowlist(self):
        for line in ("Server=0.0.0.0/0", "Server=192.168.88.1",
                     "Server=127.0.0.1,192.168.88.1",
                     "Server=127.0.0.1,192.168.16.5,192.168.16.6",
                     "Server=127.0.0.1\nServer=127.0.0.1"):
            with self.assertRaises((ValueError, RuntimeError)):
                reconcile.proposed_config(line, "192.168.16.4")

    def test_simulated_recapture_writes_nothing_by_default(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = Path(root) / "agent.conf"
            original = "Server=127.0.0.1,192.168.16.5\n"
            cfg.write_text(original)
            with patch.object(reconcile, "view", side_effect=[[server()], inspection()]):
                status, source = reconcile.reconcile(str(cfg), socket_path="mock", apply=False)
            self.assertEqual((status, source), ("DRIFT", "192.168.16.4"))
            self.assertEqual(cfg.read_text(), original)

    def test_missing_identity_requests_loopback_only(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = Path(root) / "agent.conf"
            cfg.write_text("Server=127.0.0.1,192.168.16.5\n")
            with patch.object(reconcile, "view", return_value=[]):
                state, source = reconcile.reconcile(str(cfg), "mock", False)
            self.assertEqual((state, source), ("FAIL_CLOSED_REQUIRED", None))
            self.assertIn("192.168.16.5", cfg.read_text())

    def test_apply_synthetic_ip_change_restarts_only_agent2(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = Path(root) / "agent.conf"
            cfg.write_text("Server=127.0.0.1,192.168.16.5\n")
            calls = []
            def command(*args):
                calls.append(args)
            with patch.object(reconcile, "view", side_effect=[[server()], inspection(), inspection()]), \
                 patch.object(reconcile, "check", side_effect=command):
                state, source = reconcile.reconcile(str(cfg), "mock", apply=True)
            self.assertEqual((state, source), ("UPDATED", "192.168.16.4"))
            self.assertEqual(cfg.read_text(), "Server=127.0.0.1,192.168.16.4\n")
            self.assertEqual(len(calls), 3)
            self.assertEqual(calls[1], ("systemctl", "restart", "zabbix-agent2"))

    def test_source_changes_during_restart_revokes_authorization(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = Path(root) / "agent.conf"
            cfg.write_text("Server=127.0.0.1,192.168.16.5\n")
            with patch.object(reconcile, "view",
                              side_effect=[[server()], inspection(),
                                           inspection(ip="192.168.16.6")]), \
                 patch.object(reconcile, "check"):
                with self.assertRaisesRegex(RuntimeError, "source changed"):
                    reconcile.reconcile(str(cfg), "mock", apply=True)
            self.assertEqual(cfg.read_text(), "Server=127.0.0.1\n")

    def test_missing_server_apply_revokes_old_remote_source(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = Path(root) / "agent.conf"
            cfg.write_text("Server=127.0.0.1,192.168.16.5\n")
            with patch.object(reconcile, "view", return_value=[]), \
                 patch.object(reconcile, "check"):
                state, source = reconcile.reconcile(str(cfg), "mock", apply=True)
            self.assertEqual((state, source), ("LOCKED_DOWN", None))
            self.assertEqual(cfg.read_text(), "Server=127.0.0.1\n")

    def test_failed_synthetic_restart_rolls_back_to_loopback_only(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = Path(root) / "agent.conf"
            cfg.write_text("Server=127.0.0.1,192.168.16.5\n")
            calls = []
            def command(*args):
                calls.append(args)
                if len(calls) == 2:
                    raise RuntimeError("synthetic Agent2 restart failed")
            with patch.object(reconcile, "view", side_effect=[[server()], inspection()]), \
                 patch.object(reconcile, "check", side_effect=command):
                with self.assertRaises(RuntimeError):
                    reconcile.reconcile(str(cfg), "mock", apply=True)
            self.assertEqual(cfg.read_text(), "Server=127.0.0.1\n")
            self.assertEqual(len(calls), 4)

    def test_unsupported_malformed_config_must_never_mutate(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = Path(root) / "agent.conf"
            cfg.write_text("Server=127.0.0.1,192.168.88.9\n")
            with patch.object(reconcile, "view", side_effect=[[server()],inspection()]):
                with self.assertRaises(RuntimeError):
                    reconcile.reconcile(str(cfg), "mock", False)
            self.assertEqual(cfg.read_text(), "Server=127.0.0.1,192.168.88.9\n")


if __name__ == "__main__":
    unittest.main()
