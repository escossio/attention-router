import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import TypeAdapter, ValidationError

from attention_router.contracts.integration import IntegrationContractMessage


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = ROOT / "contracts/integration/v1"
SCHEMA = json.loads((CONTRACT_DIR / "integration-contract.schema.json").read_text())
CORPUS = json.loads((CONTRACT_DIR / "conformance-cases.json").read_text())
WIRE = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
MODELS = TypeAdapter(IntegrationContractMessage)


def test_conformance_corpus_covers_every_family_and_has_unique_cases():
    Draft202012Validator.check_schema(SCHEMA)
    assert CORPUS["schema_id"] == SCHEMA["$id"]
    cases = CORPUS["cases"]
    assert len({item["id"] for item in cases}) == len(cases)
    assert all(not item["wire_valid"] or item["python_model_valid"] for item in cases)
    assert {
        item["message"]["contract_type"] for item in cases if item["wire_valid"]
    } == set(SCHEMA["discriminator"]["mapping"])


@pytest.mark.parametrize("case", CORPUS["cases"], ids=lambda case: case["id"])
def test_python_wire_validator_matches_shared_expectation(case):
    message = copy.deepcopy(case["message"])
    errors = list(WIRE.iter_errors(message))
    assert (not errors) == case["wire_valid"], [error.message for error in errors]
    assert message == case["message"]


@pytest.mark.parametrize("case", CORPUS["cases"], ids=lambda case: case["id"])
def test_native_models_preserve_normalization_and_emit_valid_wire_data(case):
    # Constructors can normalize inputs that are not valid wire JSON. A future SDK
    # must validate the wire schema before constructing models from network data.
    if not case["python_model_valid"]:
        with pytest.raises(ValidationError):
            MODELS.validate_json(json.dumps(case["message"]))
        return
    model = MODELS.validate_json(json.dumps(case["message"]))
    normalized = model.model_dump(mode="json")
    WIRE.validate(normalized)
    assert normalized["tenant_id"] == case["message"]["tenant_id"]
    if "normalized_fields" in case:
        for field, expected in case["normalized_fields"].items():
            assert normalized[field] == expected
