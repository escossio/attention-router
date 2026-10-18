"""Synthetic read-only AR-1002 adapter tests (no database)."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from attention_router.infrastructure.hashing import stable_hash
from attention_router.observability.visitor_readiness_adapter import collect


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.spec = {"behavior": {"visitor_default_contract": "text-only-v1"}, "domain": "general",
                     "audiences": ["unknown"], "channels": ["whatsapp"]}
        self.bp = SimpleNamespace(id="synthetic-bp", tenant_id="synthetic-tenant",
                                  status="published", current_version_id="synthetic-version")
        self.version = SimpleNamespace(blueprint_id="synthetic-bp", is_immutable=True,
                                       checksum=stable_hash(self.spec), spec=self.spec)
        self.session = Mock()
        self.session.get.side_effect = [self.bp, self.version]
        self.runtime = {"worker_healthy": True, "visitor_flag_enabled": True, "guard_installed": True}
        self.policy = dict.fromkeys(("policy_active", "policy_immutable", "policy_checksum_valid",
                                     "policy_text_only", "capabilities_empty", "disclosures_empty"), True)

    def run_collect(self, **kwargs):
        args = dict(session=self.session, default_blueprint_id="synthetic-bp",
                    tenant_id="synthetic-tenant", runtime=self.runtime,
                    policy=self.policy, collector_ok=True, snapshot_consistent=True,
                    blueprint_model=type("Blueprint", (), {}), version_model=type("Version", (), {}))
        args.update(kwargs)
        return collect(**args)

    def test_ready_only_with_all_attestations(self):
        self.assertEqual(self.run_collect()["state"], "READY")
        self.session.commit.assert_not_called()
        self.session.flush.assert_not_called()

    def test_missing_policy_evidence_not_ready(self):
        self.assertEqual(self.run_collect(policy={})["state"], "DEGRADED")

    def test_no_default_is_broken(self):
        self.assertEqual(self.run_collect(default_blueprint_id=None)["state"], "BROKEN")
        self.session.get.assert_not_called()

    def test_wrong_tenant_is_broken(self):
        self.assertEqual(self.run_collect(tenant_id="other-tenant")["state"], "BROKEN")

    def test_bad_checksum_is_broken(self):
        self.version.checksum = "invalid"
        self.assertEqual(self.run_collect()["state"], "BROKEN")

    def test_missing_version_is_broken(self):
        self.session.get.side_effect = [self.bp, None]
        self.assertEqual(self.run_collect()["state"], "BROKEN")

    def test_unavailable_collector_is_unknown(self):
        self.assertEqual(self.run_collect(collector_ok=False)["state"], "UNKNOWN")
        self.session.get.assert_not_called()

    def test_inconsistent_snapshot_is_unknown(self):
        self.assertEqual(self.run_collect(snapshot_consistent=False)["state"], "UNKNOWN")
        self.session.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
