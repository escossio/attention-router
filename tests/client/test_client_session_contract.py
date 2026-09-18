import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts/client/v1/client-api.openapi.json"
START = "/api/v1/session/device/challenges"
COMPLETE = START + "/{session_challenge_id}/complete"
SNAPSHOT = "/api/v1/client/bootstrap"


def load_contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def validator_for(schema_name: str) -> Draft202012Validator:
    document = load_contract()
    return Draft202012Validator(
        document["components"]["schemas"][schema_name],
        format_checker=FormatChecker(),
        resolver=Draft202012Validator(document).resolver,
    )


def test_v03c_adds_exact_session_and_authenticated_bootstrap_paths():
    document = load_contract()
    assert START in document["paths"]
    assert COMPLETE in document["paths"]
    assert SNAPSHOT in document["paths"]
    assert document["paths"][START]["post"]["security"] == []
    assert document["paths"][COMPLETE]["post"]["security"] == []
    assert document["paths"][SNAPSHOT]["get"]["security"] == [{"ClientSession": []}]


def test_client_session_security_scheme_is_separate_opaque_bearer():
    scheme = load_contract()["components"]["securitySchemes"]["ClientSession"]
    assert scheme["type"] == "http"
    assert scheme["scheme"] == "bearer"
    assert scheme["bearerFormat"] == "opaque-client-session-v03c"


def test_session_start_contains_assertions_but_no_identity_or_device_authority_ids():
    schema = load_contract()["components"]["schemas"]["ClientSessionChallengeRequest"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["public_key_spki_b64url"]
    assert set(schema["properties"]) == {
        "public_key_spki_b64url",
        "requested_tenant_id",
    }
    raw = json.dumps(schema)
    for forbidden in (
        '"human_identity_id"',
        '"device_id"',
        '"membership_id"',
        '"role"',
        '"status"',
        '"email"',
        '"google_sub"',
        '"session_id"',
        '"session_token"',
    ):
        assert forbidden not in raw
    assert "not authority by itself" in schema["properties"]["public_key_spki_b64url"]["description"]
    assert "grants no authority" in schema["properties"]["requested_tenant_id"]["description"]


def test_session_token_shape_is_frozen_and_sensitive():
    token = load_contract()["components"]["schemas"]["ClientSessionView"]["properties"][
        "session_token"
    ]
    assert token["pattern"] == "^cst_[A-Za-z0-9_-]{43}$"
    assert token["minLength"] == token["maxLength"] == 47
    assert "Never persist raw server-side or log" in token["description"]


def test_session_complete_contains_only_device_signature():
    schema = load_contract()["components"]["schemas"]["ClientSessionCompleteRequest"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["device_signature_b64url"]
    assert set(schema["properties"]) == {"device_signature_b64url"}


def test_session_response_has_no_refresh_or_provider_identity_material():
    schema = load_contract()["components"]["schemas"]["ClientSessionEstablishedResponse"]
    raw = json.dumps(schema)
    assert schema["properties"]["status"]["const"] == "CLIENT_SESSION_ESTABLISHED"
    for forbidden in ("refresh_token", "google_sub", "email", "id_token", "continuation_token"):
        assert forbidden not in raw


def test_authenticated_bootstrap_is_deliberately_bounded():
    schema = load_contract()["components"]["schemas"]["AuthenticatedClientBootstrapSnapshot"]
    assert schema["required"] == [
        "contract_version",
        "human_identity_id",
        "active_tenant_id",
        "memberships",
        "device",
        "session_expires_at",
        "server_time",
    ]
    assert set(schema["properties"]) == set(schema["required"])
    raw = json.dumps(schema)
    for forbidden in (
        "capabilities",
        "channel_installations",
        "integration_installations",
        "refresh_token",
        "session_token",
        "google_sub",
        "email",
    ):
        assert forbidden not in raw


def test_v03c_wire_examples_validate():
    start = {
        "public_key_spki_b64url": "A" * 120,
        "requested_tenant_id": "tnt_synthetic",
    }
    challenge = {
        "session_challenge_id": "csc_" + "a" * 24,
        "challenge_b64url": "b" * 43,
        "expires_at": "2026-09-18T16:15:00Z",
    }
    complete = {"device_signature_b64url": "c" * 96}
    established = {
        "status": "CLIENT_SESSION_ESTABLISHED",
        "session": {
            "session_id": "csn_" + "d" * 24,
            "session_token": "cst_" + "e" * 43,
            "token_type": "Bearer",
            "expires_at": "2026-09-18T16:30:00Z",
            "human_identity_id": "hid_" + "f" * 24,
            "device_id": "cdev_" + "g" * 24,
            "tenant_id": "tnt_synthetic",
        },
    }
    snapshot = {
        "contract_version": "1",
        "human_identity_id": "hid_" + "f" * 24,
        "active_tenant_id": "tnt_synthetic",
        "memberships": [{
            "membership_id": "ctm_synthetic",
            "tenant_id": "tnt_synthetic",
            "role": "OWNER",
            "status": "ACTIVE",
        }],
        "device": {
            "device_id": "cdev_" + "g" * 24,
            "public_key_fingerprint": "sha256:" + "a" * 64,
            "canonical_name": "Synthetic Android",
            "platform": "ANDROID",
            "roles": ["CLIENT", "CAPABILITY_NODE"],
            "status": "ACTIVE",
        },
        "session_expires_at": "2026-09-18T16:30:00Z",
        "server_time": "2026-09-18T16:16:00Z",
    }
    for schema_name, payload in (
        ("ClientSessionChallengeRequest", start),
        ("ClientSessionChallengeResponse", challenge),
        ("ClientSessionCompleteRequest", complete),
        ("ClientSessionEstablishedResponse", established),
        ("AuthenticatedClientBootstrapSnapshot", snapshot),
    ):
        assert validator_for(schema_name).is_valid(payload)


def test_v03c_start_rejects_client_selected_identity_authority():
    payload = {
        "public_key_spki_b64url": "A" * 120,
        "human_identity_id": "hid_" + "x" * 24,
    }
    assert not validator_for("ClientSessionChallengeRequest").is_valid(payload)


def test_session_error_vocabulary_is_bounded():
    error = load_contract()["components"]["schemas"]["ClientSessionErrorResponse"]
    assert set(error["properties"]["code"]["enum"]) == {
        "CLIENT_SESSION_DEVICE_REJECTED",
        "CLIENT_SESSION_CHALLENGE_NOT_FOUND",
        "CLIENT_SESSION_CHALLENGE_EXPIRED",
        "CLIENT_SESSION_CHALLENGE_CONSUMED",
        "CLIENT_SESSION_SIGNATURE_INVALID",
        "CLIENT_SESSION_ACTIVE_TENANT_REQUIRED",
        "CLIENT_SESSION_TENANT_FORBIDDEN",
        "CLIENT_SESSION_UNAUTHENTICATED",
        "CLIENT_SESSION_AUTHORITY_REJECTED",
        "CLIENT_SESSION_UNAVAILABLE",
    }
