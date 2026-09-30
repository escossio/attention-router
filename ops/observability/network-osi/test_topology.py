import unittest

from topology import evidence


class TopologyTests(unittest.TestCase):
    def test_declared_trunk_in_another_bridge_is_not_verified(self):
        vlans = [{"name": f"ANDY{x}-GW", "vlan-id": x, "interface": "runtime-bridge"}
                 for x in range(210, 218)]
        result = evidence(vlans, [{"interface": "trunk", "bridge": "other-bridge"}],
                          {"status": "no-link"}, "trunk")
        self.assertFalse(result["consistent"])
        self.assertIn("unproven", result["note"])

    def test_missing_or_incorrect_vlan_never_verifies(self):
        vlans = [{"name": f"ANDY{x}-GW", "vlan-id": x, "interface": "runtime-bridge"}
                 for x in range(210, 218)]
        ports = [{"interface": "trunk", "bridge": "runtime-bridge"}]
        self.assertTrue(evidence(vlans, ports, {}, "trunk")["consistent"])
        vlans[0]["vlan-id"] = 999
        self.assertFalse(evidence(vlans, ports, {}, "trunk")["consistent"])
        self.assertFalse(evidence(vlans[1:], ports, {}, "trunk")["consistent"])


if __name__ == "__main__":
    unittest.main()
