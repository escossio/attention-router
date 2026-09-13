import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts/client/v1/client-api.openapi.json"
CHALLENGES = "/api/v1/auth/google/challenges"
VERIFY = CHALLENGES + "/{challenge_id}/verify"


def load_contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def test_human_identity_contract_has_only_approved_auth_paths():
    document = load_contract()
    assert document["openapi"] == "3.1.0"
    assert set(document["paths"]) == {CHALLENGES, VERIFY}
    for path in document["paths"].values():
        assert set(path) == {"post"}


def test_pre_session_human_auth_paths_require_no_bearer_session():
    for path in load_contract()["paths"].values():
        assert path["post"]["security"] == []


def test_human_identity_contract_excludes_forbidden_authority_fields():
    raw = CONTRACT.read_text(encoding="utf-8")
    for field in {
        "tenant_id", "device_id", "session_id", "access_token", "refresh_token",
        "membership", "integration_credential", "sub", "google_sub", "subject", "email",
    }:
        assert f'"{field}"' not in raw


def test_verify_request_contains_only_id_token():
    request = load_contract()["components"]["schemas"]["GoogleHumanIdentityVerifyRequest"]
    assert request["required"] == ["id_token"]
    assert set(request["properties"]) == {"id_token"}
    assert request["additionalProperties"] is False


def test_challenge_response_contains_only_challenge_nonce_and_expiry():
    response = load_contract()["components"]["schemas"]["HumanAuthChallengeResponse"]
    assert response["required"] == ["challenge_id", "nonce", "expires_at"]
    assert set(response["properties"]) == {"challenge_id", "nonce", "expires_at"}
    assert response["properties"]["expires_at"] == {"type": "string", "format": "date-time"}


def test_validated_response_exposes_opaque_identity_not_google_subject():
    response = load_contract()["components"]["schemas"]["HumanIdentityValidatedResponse"]
    assert response["required"] == ["status", "human_identity_id"]
    assert set(response["properties"]) == {"status", "human_identity_id"}
    assert response["properties"]["status"]["const"] == "HUMAN_IDENTITY_VALIDATED"


@pytest.mark.parametrize(
    ("schema_name", "field", "prefix"),
    [
        ("HumanAuthChallengeResponse", "challenge_id", "hac_"),
        ("HumanIdentityValidatedResponse", "human_identity_id", "hid_"),
    ],
)
def test_ids_require_opaque_namespaced_references(schema_name, field, prefix):
    schema = load_contract()["components"]["schemas"][schema_name]["properties"][field]
    assert schema == {"type": "string", "pattern": f"^{prefix}[A-Za-z0-9_-]{{20,}}$"}
    validator = Draft202012Validator(schema)
    assert validator.is_valid(prefix + "aB0_-" * 4)
    for invalid in ("", prefix, prefix + "a" * 19, "google:12345678901234567890",
                    "person@example.invalid", "12345678901234567890", 123):
        assert not validator.is_valid(invalid)


@pytest.mark.parametrize(
    ("schema_name", "payload"),
    [
        ("HumanAuthChallengeResponse", {
            "challenge_id": "hac_abcdefghijklmnopqrst",
            "nonce": "n" * 32,
            "expires_at": "2026-09-13T00:05:00Z",
        }),
        ("GoogleHumanIdentityVerifyRequest", {"id_token": "synthetic-credential-" + "x" * 32}),
        ("HumanIdentityValidatedResponse", {
            "status": "HUMAN_IDENTITY_VALIDATED",
            "human_identity_id": "hid_abcdefghijklmnopqrst",
        }),
        ("HumanAuthErrorResponse", {"code": "HUMAN_AUTH_CREDENTIAL_REJECTED"}),
    ],
)
def test_wire_objects_validate_and_reject_extra_or_missing_properties(schema_name, payload):
    schema = load_contract()["components"]["schemas"][schema_name]
    Draft202012Validator.check_schema(schema)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    assert validator.is_valid(payload)
    assert not validator.is_valid({**payload, "unexpected": True})
    for field in payload:
        assert not validator.is_valid({key: value for key, value in payload.items() if key != field})


def test_operations_reference_the_frozen_wire_schemas():
    document = load_contract()
    create = document["paths"][CHALLENGES]["post"]
    verify = document["paths"][VERIFY]["post"]
    assert "requestBody" not in create
    assert verify["requestBody"]["required"] is True
    assert verify["requestBody"]["content"] == {
        "application/json": {"schema": {
            "$ref": "#/components/schemas/GoogleHumanIdentityVerifyRequest",
        }},
    }
    assert verify["parameters"] == [{
        "name": "challenge_id", "in": "path", "required": True,
        "schema": {"type": "string", "pattern": "^hac_[A-Za-z0-9_-]{20,}$"},
    }]
    for operation, success, schema_name, codes in (
        (create, "201", "HumanAuthChallengeResponse", {"201", "503"}),
        (verify, "200", "HumanIdentityValidatedResponse",
         {"200", "401", "404", "409", "410", "503"}),
    ):
        assert set(operation["responses"]) == codes
        for code, response in operation["responses"].items():
            target = schema_name if code == success else "HumanAuthErrorResponse"
            assert response["content"] == {
                "application/json": {"schema": {"$ref": f"#/components/schemas/{target}"}},
            }


def test_errors_expose_only_the_seven_approved_semantic_codes():
    schemas = load_contract()["components"]["schemas"]
    assert set(schemas) == {
        "HumanAuthChallengeResponse", "GoogleHumanIdentityVerifyRequest",
        "HumanIdentityValidatedResponse", "HumanAuthErrorResponse",
    }
    error = schemas["HumanAuthErrorResponse"]
    assert error["required"] == ["code"]
    assert set(error["properties"]) == {"code"}
    assert error["properties"]["code"] == {"type": "string", "enum": [
        "HUMAN_AUTH_DISABLED",
        "HUMAN_AUTH_CHALLENGE_NOT_FOUND",
        "HUMAN_AUTH_CHALLENGE_EXPIRED",
        "HUMAN_AUTH_CHALLENGE_CONSUMED",
        "HUMAN_AUTH_CREDENTIAL_REJECTED",
        "HUMAN_AUTH_NONCE_MISMATCH",
        "HUMAN_AUTH_PROVIDER_UNAVAILABLE",
    ]}
