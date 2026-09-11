"""Run only against the installed wheel in a fresh, isolated virtual environment."""

import copy
from datetime import datetime
import importlib.util
from importlib.resources import files
import json
from pathlib import Path
import unittest

import andy_integration_sdk as sdk

ROOT = Path(__file__).resolve().parents[3]
CONTRACT = ROOT / "contracts/integration/v1"
CASES = json.loads((CONTRACT / "conformance-cases.json").read_text())["cases"]


class InstalledSDKTests(unittest.TestCase):
    def test_is_independent_and_bundles_schema_and_typing_marker(self):
        for dependency in ("attention_router", "pydantic", "sqlalchemy"):
            self.assertIsNone(importlib.util.find_spec(dependency))
        package = files(sdk)
        self.assertEqual(package.joinpath("_schema.json").read_bytes(),
                         (CONTRACT / "integration-contract.schema.json").read_bytes())
        self.assertTrue(package.joinpath("py.typed").is_file())
        self.assertNotIn(str(ROOT), str(package))

    def test_schema_copy_cannot_change_validator(self):
        schema = sdk.get_schema()
        schema.clear()
        self.assertEqual(sdk.get_schema()["$id"], sdk.SCHEMA_ID)
        self.assertFalse(sdk.is_contract({}))

    def test_no_aliasing_or_defaults(self):
        original = copy.deepcopy(CASES[0]["message"])
        original.pop("artifact_ids", None)
        before = copy.deepcopy(original)
        validated = sdk.validate_contract(original)
        self.assertEqual(original, before)
        self.assertEqual(validated, before)
        validated["source"]["instance_id"] = "changed"
        self.assertEqual(original, before)

    def test_rejects_native_values_and_cycles(self):
        cycle = {}
        cycle["cycle"] = cycle
        for invalid in (float("nan"), float("inf"), datetime.now(), {1}, (1,), {1: "key"}, cycle):
            message = copy.deepcopy(CASES[0]["message"])
            message["payload_ref"] = {"nested": invalid}
            with self.subTest(kind=type(invalid).__name__):
                with self.assertRaises(sdk.ContractValidationError):
                    sdk.validate_contract(message)
                self.assertFalse(sdk.is_contract(message))

    def test_json_errors_do_not_echo_values(self):
        for text in ('{"synthetic-canary":', "NaN", "Infinity", "1e999", None):
            with self.subTest(text=text):
                with self.assertRaises(sdk.ContractValidationError) as caught:
                    sdk.parse_contract(text)
                self.assertNotIn("synthetic-canary", str(caught.exception))
                self.assertEqual(caught.exception.issues[0].keyword, "json")

    def test_validation_errors_are_structured_without_payload_values(self):
        value = copy.deepcopy(CASES[0]["message"])
        value["schema_version"] = "synthetic-canary"
        with self.assertRaises(sdk.ContractValidationError) as caught:
            sdk.serialize_contract(value)
        self.assertNotIn("synthetic-canary", str(caught.exception))
        self.assertTrue(caught.exception.issues)
        self.assertTrue(all(isinstance(issue.path, str) and issue.keyword
                            for issue in caught.exception.issues))


def conformance_test(case):
    def test(self):
        original = copy.deepcopy(case["message"])
        text = json.dumps(original, ensure_ascii=True)
        self.assertEqual(sdk.is_contract(original), case["wire_valid"])
        if case["wire_valid"]:
            self.assertEqual(sdk.validate_contract(original), original)
            self.assertEqual(sdk.parse_contract(text), original)
            self.assertEqual(sdk.parse_contract(sdk.serialize_contract(original)), original)
        else:
            for action, value in ((sdk.validate_contract, original),
                                  (sdk.parse_contract, text),
                                  (sdk.serialize_contract, original)):
                with self.assertRaises(sdk.ContractValidationError):
                    action(value)
        self.assertEqual(original, case["message"])
    return test


for case in CASES:
    setattr(InstalledSDKTests, "test_wire_" + case["id"], conformance_test(case))
