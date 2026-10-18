"""Synthetic-only resolver-witness tests for AR-1002."""
import unittest
from types import SimpleNamespace
from attention_router.observability.visitor_policy_witness import verify_unknown_policy


class WitnessTests(unittest.TestCase):
    def setUp(self):
        self.witness = {"contact_id": "synthetic-visitor-001", "category": "unknown",
                        "active_context": None, "audience": "unknown", "binding_id": None}
        self.resolver = lambda *args, **kwargs: SimpleNamespace(
            winner=SimpleNamespace(identifier="synthetic-policy"))

    def check(self, **changes):
        args = dict(policies=[], resolve=self.resolver, witness=self.witness,
                    expected_policy_id="synthetic-policy", snapshot_consistent=True)
        args.update(changes)
        return verify_unknown_policy(**args)

    def test_matching_witness(self):
        self.assertIs(self.check(), True)

    def test_different_winner(self):
        self.assertIs(self.check(expected_policy_id="other-policy"), False)

    def test_unavailable_snapshot(self):
        self.assertIsNone(self.check(snapshot_consistent=False))

    def test_missing_expected_policy(self):
        self.assertIsNone(self.check(expected_policy_id=None))

    def test_real_contact_never_accepted(self):
        self.assertIsNone(self.check(witness=self.witness | {"contact_id": "real-contact"}))

    def test_owner_or_binding_never_accepted(self):
        self.assertIsNone(self.check(witness=self.witness | {"audience": "owner"}))
        self.assertIsNone(self.check(witness=self.witness | {"binding_id": "synthetic-binding"}))

    def test_unresolved_is_unknown(self):
        def missing(*args, **kwargs):
            raise ValueError("no policy matched")
        self.assertIsNone(self.check(resolve=missing))


if __name__ == "__main__":
    unittest.main()
