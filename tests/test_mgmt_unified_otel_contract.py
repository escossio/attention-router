"""Offline contract for MGMT / ROC E2E integration (no network or secrets)."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PORTAL = ROOT / "ops/provisioning/andy-ops-panel"


class MgmtUnifiedContract(unittest.TestCase):
    def test_views_and_navigation_exist(self):
        html = (PORTAL / "index.html").read_text()
        for name in ("flow-e2e", "live-message", "network", "goaccess", "dozzle"):
            self.assertIn(f'data-tab="{name}"', html)
            self.assertIn(f'id="{name}-frame"', html)
        self.assertIn('id="goaccess-domain-open"', html)

    def test_embeds_are_configured_and_truthful(self):
        js = (PORTAL / "app.js").read_text()
        server = (PORTAL / "server.py").read_text()
        for key in ("flow_e2e_url", "live_message_url"):
            self.assertIn(key, js)
            self.assertIn(key, server)
        self.assertIn("CARREGADO · VERIFICAR LOGIN", js)
        self.assertIn("'/observability/goaccess/'", server.replace('"', "'"))
        self.assertIn("'/observability/dozzle/'", server.replace('"', "'"))
        self.assertNotIn('status.textContent = "AO VIVO"', js)

    def test_proxy_order_auth_websocket_and_html_rewrite(self):
        text = (PORTAL / "mgmt-otel-apache-vhost.example.conf").read_text()
        ws = text.index("ProxyPass        /observability/goaccess/ws/")
        web = text.index("ProxyPass        /observability/goaccess/ ${MGMT_GOACCESS_HTTP_ORIGIN}")
        dozzle = text.index("ProxyPass        /observability/dozzle/")
        fallback = text.index("ProxyPass        / ${MGMT_PORTAL_ORIGIN}")
        self.assertLess(ws, web)
        self.assertLess(web, fallback)
        self.assertLess(dozzle, fallback)
        self.assertLess(text.index("ProxyPass /observability/goaccess/dominios/ !"), web)
        self.assertIn("Alias /dominios/ ${MGMT_DOMAINS_DIR}/", text)
        self.assertIn("Options -Indexes", text)
        self.assertIn("Require valid-user", text)
        self.assertIn("RequestHeader unset Authorization", text)
        self.assertIn("RewriteCond %{HTTP:X-Forwarded-Proto} ^http$", text)
        self.assertIn("[R=308,L,NE]", text)
        self.assertLess(text.index("RewriteEngine On"), text.index("AuthType Basic"))
        self.assertIn("${MGMT_GOACCESS_WS_ORIGIN}", text)
        self.assertIn("${MGMT_PORTAL_ORIGIN}", text)
        self.assertNotIn("10.45.0.2", text)
        self.assertIn("SubstituteMaxLineLength 5m", text)
        self.assertIn("port", text)
        self.assertIn("443", text)

    def test_dozzle_uses_filtered_socket_and_base_path(self):
        compose = (PORTAL / "compose.mgmt-dozzle.yaml").read_text()
        self.assertIn("DOZZLE_BASE: /observability/dozzle", compose)
        self.assertIn("attention-router-dozzle-v1_dozzle_filtered_socket", compose)
        self.assertIn("read_only: true", compose)
        self.assertNotIn("/var/run/docker.sock", compose)
        self.assertIn("127.0.0.1:18081:8080", compose)


if __name__ == "__main__":
    unittest.main()
