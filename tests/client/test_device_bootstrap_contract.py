import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts/client/v1/client-api.openapi.json"
START = "/api/v1/bootstrap/device/challenges"
COMPLETE = START + "/{bootstrap_challenge_id}/complete"


def load_contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def validator_for(schema_name: str) -> Draft202012Validator:
    document = load_contract()
    return Draft202012Validator(
        document["components"]["schemas"][schema_name],
        format_checker=FormatChecker(),
        resolver=Draft202012Validator(document).resolver,
    )


def test_v03b_adds_only_the_two_device_bootstrap_paths():
    document = load_contract()
    bootstrap_paths = {
        path: item for path, item in document["paths"].items()
        if path.startswith("/api/v1/bootstrap/")
    }
    assert set(bootstrap_paths) == {START, COMPLETE}
    assert bootstrap_paths[START]["post"]["security"] == []
    assert bootstrap_paths[COMPLETE]["post"]["security"] == []


def test_start_request_contains_no_client_selected_authority():
    schema = load_contract()["components"]["schemas"]["DeviceBootstrapChallengeRequest"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "continuation_token",
        "public_key_spki_b64url",
        "canonical_device_name",
        "platform",
        "roles",
    ]
    properties = set(schema["properties"])
    assert properties == set(schema["required"])
    assert properties.isdisjoint({
        "human_identity_id",
        "tenant_id",
        "device_id",
        "membership_id",
        "role",
        "status",
        "email",
        "subject",
        "provider",
        "access_token",
        "refresh_token",
    })
    assert schema["properties"]["platform"] == {"const": "ANDROID"}
    assert set(schema["properties"]["roles"]["items"]["enum"]) == {
        "CLIENT", "CAPABILITY_NODE",
    }


def test_continuation_credential_is_explicitly_sensitive_and_single_purpose_shape():
    token = load_contract()["components"]["schemas"]["DeviceBootstrapChallengeRequest"][
        "properties"
    ]["continuation_token"]
    assert token["pattern"] == "^hcg_[A-Za-z0-9_-]{43}$"
    assert "never persist or log" in token["description"]


def test_device_key_contract_sends_spki_not_fingerprint_authority():
    schema = load_contract()["components"]["schemas"]["DeviceBootstrapChallengeRequest"]
    assert "public_key_spki_b64url" in schema["properties"]
    assert "public_key_fingerprint" not in schema["properties"]
    assert "Server validates P-256 and derives the fingerprint" in (
        schema["properties"]["public_key_spki_b64url"]["description"]
    )


def test_complete_request_contains_only_signature():
    schema = load_contract()["components"]["schemas"]["DeviceBootstrapCompleteRequest"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["device_signature_b64url"]
    assert set(schema["properties"]) == {"device_signature_b64url"}


def test_established_response_has_authority_snapshot_but_no_session():
    schema = load_contract()["components"]["schemas"]["DeviceBootstrapEstablishedResponse"]
    assert schema["required"] == [
        "status",
        "human_identity_id",
        "memberships",
        "initial_tenant_id",
        "device",
    ]
    assert set(schema["properties"]) == set(schema["required"])
    assert schema["properties"]["status"]["const"] == "DEVICE_BOOTSTRAP_ESTABLISHED"
    raw = json.dumps(schema)
    for forbidden in (
        "access_token", "refresh_token", "session_id",
        "email", "subject", "google_sub",
    ):
        assert f'"{forbidden}"' not in raw


def test_initial_tenant_can_be_null_for_multiple_memberships():
    initial = load_contract()["components"]["schemas"]["DeviceBootstrapEstablishedResponse"][
        "properties"
    ]["initial_tenant_id"]
    assert {item["type"] for item in initial["anyOf"]} == {"string", "null"}


def test_device_view_exposes_server_derived_fingerprint_and_no_public_key():
    schema = load_contract()["components"]["schemas"]["ClientDeviceView"]
    assert schema["properties"]["public_key_fingerprint"]["pattern"] == (
        "^sha256:[0-9a-f]{64}$"
    )
    assert "public_key_spki_b64url" not in schema["properties"]
    assert "private_key" not in json.dumps(schema)


def test_v03b_wire_examples_validate():
    start = {
        "continuation_token": "hcg_" + "a" * 43,
        "public_key_spki_b64url": "A" * 120,
        "canonical_device_name": "Synthetic Android",
        "platform": "ANDROID",
        "roles": ["CLIENT", "CAPABILITY_NODE"],
    }
    challenge = {
        "bootstrap_challenge_id": "dbc_" + "a" * 24,
        "challenge_b64url": "b" * 43,
        "expires_at": "2026-09-17T23:00:00Z",
    }
    complete = {"device_signature_b64url": "c" * 96}
    established = {
        "status": "DEVICE_BOOTSTRAP_ESTABLISHED",
        "human_identity_id": "hid_" + "d" * 24,
        "memberships": [{
            "membership_id": "ctm_synthetic",
            "tenant_id": "tnt_synthetic",
            "role": "OWNER",
            "status": "ACTIVE",
        }],
        "initial_tenant_id": "tnt_synthetic",
        "device": {
            "device_id": "cdev_" + "e" * 24,
            "public_key_fingerprint": "sha256:" + "f" * 64,
            "canonical_name": "Synthetic Android",
            "platform": "ANDROID",
            "roles": ["CLIENT", "CAPABILITY_NODE"],
            "status": "ACTIVE",
        },
    }
    for schema_name, payload in (
        ("DeviceBootstrapChallengeRequest", start),
        ("DeviceBootstrapChallengeResponse", challenge),
        ("DeviceBootstrapCompleteRequest", complete),
        ("DeviceBootstrapEstablishedResponse", established),
    ):
        assert validator_for(schema_name).is_valid(payload)


def test_v03b_wire_objects_reject_extra_authority_fields():
    start = {
        "continuation_token": "hcg_" + "a" * 43,
        "public_key_spki_b64url": "A" * 120,
        "canonical_device_name": "Synthetic Android",
        "platform": "ANDROID",
        "roles": ["CLIENT"],
        "tenant_id": "attacker-selected",
    }
    assert not validator_for("DeviceBootstrapChallengeRequest").is_valid(start)


def test_bootstrap_error_vocabulary_is_bounded():
    error = load_contract()["components"]["schemas"]["DeviceBootstrapErrorResponse"]
    assert set(error["properties"]["code"]["enum"]) == {
        "DEVICE_BOOTSTRAP_GRANT_REJECTED",
        "DEVICE_BOOTSTRAP_CHALLENGE_NOT_FOUND",
        "DEVICE_BOOTSTRAP_CHALLENGE_EXPIRED",
        "DEVICE_BOOTSTRAP_CHALLENGE_CONSUMED",
        "DEVICE_BOOTSTRAP_DEVICE_KEY_INVALID",
        "DEVICE_BOOTSTRAP_SIGNATURE_INVALID",
        "DEVICE_BOOTSTRAP_MEMBERSHIP_CONFLICT",
        "DEVICE_BOOTSTRAP_DEVICE_CONFLICT",
        "DEVICE_BOOTSTRAP_UNAVAILABLE",
    }


def test_operations_reference_frozen_v03b_schemas():
    document = load_contract()
    start = document["paths"][START]["post"]
    complete = document["paths"][COMPLETE]["post"]

    assert start["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/DeviceBootstrapChallengeRequest"
    }
    assert start["responses"]["201"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/DeviceBootstrapChallengeResponse"
    }
    assert complete["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/DeviceBootstrapCompleteRequest"
    }
    assert complete["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/DeviceBootstrapEstablishedResponse"
    }
