import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts/client/v1/client-api.openapi.json"
PENDING = "/api/v1/client/approvals/pending"
DETAIL = "/api/v1/client/approvals/{approval_id}"
DECISION = "/api/v1/client/approvals/{approval_id}/decision"


def load_contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def validator_for(schema_name: str) -> Draft202012Validator:
    document = load_contract()
    return Draft202012Validator(
        document["components"]["schemas"][schema_name],
        format_checker=FormatChecker(),
        resolver=Draft202012Validator(document).resolver,
    )


def test_native_approval_surface_is_client_session_authenticated():
    document = load_contract()
    assert document["paths"][PENDING]["get"]["security"] == [
        {"ClientSession": []}
    ]
    assert document["paths"][DETAIL]["get"]["security"] == [
        {"ClientSession": []}
    ]
    assert document["paths"][DECISION]["post"]["security"] == [
        {"ClientSession": []}
    ]
    assert (
        document["paths"][PENDING]["get"]["operationId"]
        == "listPendingClientApprovals"
    )
    assert (
        document["paths"][DECISION]["post"]["operationId"]
        == "decideClientApproval"
    )


def test_decision_request_contains_decision_only_not_authority():
    schema = load_contract()["components"]["schemas"][
        "ClientApprovalDecisionRequest"
    ]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["decision"]
    assert set(schema["properties"]) == {"decision"}
    assert set(schema["properties"]["decision"]["enum"]) == {
        "APPROVE",
        "DENY",
    }
    raw = json.dumps(schema)
    for forbidden in (
        "tenant_id",
        "human_identity_id",
        "device_id",
        "session_id",
        "session_token",
        "execution_intent_id",
    ):
        assert forbidden not in raw


def test_approval_view_is_sanitized_and_bounded():
    schema = load_contract()["components"]["schemas"]["ClientApprovalView"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "approval_id",
        "state",
        "capability",
        "operation",
        "target",
        "preview",
        "issued_at",
        "expires_at",
    }
    assert set(schema["properties"]) == set(schema["required"])
    assert set(schema["properties"]["state"]["enum"]) == {
        "PENDING_HUMAN_APPROVAL",
        "APPROVED",
        "DENIED",
        "EXPIRED",
        "CONSUMED",
        "REVOKED",
    }
    raw = json.dumps(schema)
    for forbidden in (
        "scope_fingerprint",
        "request_wamid",
        "button_id",
        "provider_event",
        "session_token",
        "token_digest",
    ):
        assert forbidden not in raw


def test_wire_examples_validate():
    request = {"decision": "APPROVE"}
    view = {
        "approval_id": "apr_test",
        "state": "PENDING_HUMAN_APPROVAL",
        "capability": "conversation.reply",
        "operation": "conversation.reply",
        "target": "synthetic-target",
        "preview": "Mensagem proposta",
        "issued_at": "2026-09-22T06:45:00Z",
        "expires_at": "2026-09-22T06:50:00Z",
    }
    assert validator_for("ClientApprovalDecisionRequest").is_valid(request)
    assert validator_for("ClientApprovalView").is_valid(view)


def test_client_approval_contract_does_not_expose_execute_surface():
    document = load_contract()
    approval_paths = {
        path: value
        for path, value in document["paths"].items()
        if "/client/approvals" in path
    }
    raw = json.dumps(approval_paths).lower()
    for forbidden in (
        "/execute",
        "/run",
        "execution_allowed",
        "external_delivery_allowed",
        "meta_whatsapp",
        "wamid",
    ):
        assert forbidden not in raw
