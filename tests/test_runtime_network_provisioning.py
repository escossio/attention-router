from pathlib import Path
import re
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
ROOT = REPO_ROOT / "ops" / "historical" / "whatsapp-host-native" / "runtime-network"
CANDIDATE = REPO_ROOT / "ops" / "whatsapp-container"
PRIVATE_IPV4 = re.compile(
    r"(?:192\\.168\\.|10\\.\\d+\\.\\d+\\.\\d+|172\\.(?:1[6-9]|2\\d|3[01])\\.\\d+\\.\\d+)"
)


class RuntimeNetworkContractTests(unittest.TestCase):
    def test_browser_parent_is_explicit(self):
        script = (ROOT / "bin" / "andy-netns-vlan").read_text()
        self.assertIn('PARENT="$6"', script)
        self.assertNotRegex(script, r"enp\\d")

    def test_transport_topology_is_parameterized(self):
        script = (ROOT / "bin" / "andy-transport-netns").read_text()
        for token in ('PARENT="$2"', 'VLAN="$4"', 'ADDR="$5"', 'GW="$6"'):
            self.assertIn(token, script)
        self.assertNotRegex(script, r"enp\\d")

    def test_transport_offload_workaround_is_explicit_and_gated(self):
        script = (ROOT / "bin" / "andy-transport-netns").read_text()
        self.assertIn('DISABLE_TX_OFFLOADS="$7"', script)
        self.assertIn("ethtool -K eth0 tx off tso off gso off", script)
        example = (ROOT / "env" / "agt-network-namespace.env.example").read_text()
        self.assertIn("ANDY_TRANSPORT_DISABLE_TX_OFFLOADS=false", example)

    def test_units_read_private_host_environment(self):
        for name in (
            "andy-browser-netns.service",
            "andy-browser-cdp-edge.service",
            "andy-transport-netns.service",
            "andy-transport-cdp-proxy.service",
        ):
            unit = (ROOT / "systemd" / name).read_text()
            self.assertIn(
                "EnvironmentFile=/etc/attention-router/agt-network-namespace.env",
                unit,
            )

    def test_retired_services_keep_rollback_namespace_attachment(self):
        browser = (
            ROOT / "systemd/attention-whatsapp-browser.service.d/zz-network-namespace.conf"
        ).read_text()
        transport = (
            ROOT
            / "systemd/attention-whatsapp-transport.service.d/zzzzzzzz-network-namespace.conf"
        ).read_text()
        self.assertIn("NetworkNamespacePath=/run/netns/andy-browser", browser)
        self.assertIn("NetworkNamespacePath=/run/netns/andy-transport", transport)

    def test_public_package_has_no_private_topology_literals(self):
        for path in (*ROOT.rglob("*"), *CANDIDATE.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            text = path.read_text(errors="ignore")
            self.assertIsNone(PRIVATE_IPV4.search(text), path)
            self.assertNotRegex(text, r"\benp\d", path)

    def test_container_target_replaces_manual_namespace_units(self):
        self.assertFalse((REPO_ROOT / "ops/provisioning/runtime-network").exists())
        self.assertIn("RETIRED_HISTORICAL", (ROOT / "README.md").read_text())
        compose = (CANDIDATE / "compose.yaml").read_text()
        self.assertIn("driver: macvlan", compose)
        self.assertIn("internal: true", compose)
        self.assertIn("network_mode: service:browser", compose)
        self.assertNotIn("NetworkNamespacePath", compose)


if __name__ == "__main__":
    unittest.main()
