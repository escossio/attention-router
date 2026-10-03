import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

source = Path(__file__).resolve().parents[2] / "provisioning/andy-ops-panel/server.py"
spec = importlib.util.spec_from_file_location("andy_panel", source)
panel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(panel)


class PanelUrlTests(unittest.TestCase):
    def test_dashboard_query_is_preserved(self):
        url = "https://grafana.example.test/d/roc-network-osi/network?orgId=1&kiosk&refresh=10s"
        with patch.dict(os.environ, {"NETWORK_TEST_URL": url}):
            self.assertEqual(panel._observability_url("NETWORK_TEST_URL"), url)

    def test_credentials_and_encoded_secret_keys_are_rejected(self):
        for url in ["https://user:password@grafana.example.test/", "javascript:alert(1)",
                    "https://grafana.example.test/?%74oken=example", "https://grafana.example.test/?api_key=example"]:
            with self.subTest(url=url), patch.dict(os.environ, {"NETWORK_TEST_URL": url}):
                self.assertIsNone(panel._observability_url("NETWORK_TEST_URL"))


if __name__ == "__main__":
    unittest.main()
