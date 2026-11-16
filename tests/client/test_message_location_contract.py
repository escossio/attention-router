"""Public API schema is owner-scoped, evidence-first and cannot claim a person."""
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

DOC=json.loads(
    (Path(__file__).resolve().parents[2]/"contracts/client/v1/client-api.openapi.json").read_text()
)


def test_message_location_contract_owner_bearer_and_statuses():
    op=DOC["paths"]["/api/v1/client/location/message/{message_id}"]["get"]
    assert op["security"]==[{"ClientSession":[]}]
    assert op["operationId"]=="getLocationContextForMessage"
    assert "post" not in DOC["paths"]["/api/v1/client/location/message/{message_id}"]
    schema=DOC["components"]["schemas"]["ClientMessageLocationContext"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"])=={
        "contract_version","message_id","status","evidence_basis","message_at",
    }
    assert schema["properties"]["evidence_basis"]["const"]=="ANDROID_DEVICE_POSITION_NEAR_WHATSAPP_MESSAGE_TIME"
    assert "OWNER_PRESENT_AT_MESSAGE" not in schema["properties"]["status"]["enum"]
    assert {"NO_TIME_MATCHED_LOCATION","AMBIGUOUS_DEVICES","AUTHORSHIP_NOT_PROVEN"}<=set(
        schema["properties"]["status"]["enum"]
    )


def test_location_result_requires_temporal_provenance_not_fabricated_address():
    schema=DOC["components"]["schemas"]["ClientMessageLocationContext"]
    validator=Draft202012Validator(schema,format_checker=FormatChecker())
    example={
        "contract_version":"1","message_id":"cmsg_synthetic",
        "status":"NO_TIME_MATCHED_LOCATION",
        "evidence_basis":"ANDROID_DEVICE_POSITION_NEAR_WHATSAPP_MESSAGE_TIME",
        "message_at":"2026-11-16T15:30:00Z",
    }
    assert validator.is_valid(example)
    assert not validator.is_valid({**example,"address":"invented address"})
    assert validator.is_valid({
        **example,
        "status":"DEVICE_POSITION_NEAR_MESSAGE",
        "location_observation_id":"clh_synthetic",
        "captured_at":"2026-11-16T15:30:07Z",
        "delta_seconds":7,
        "latitude":-3.71,"longitude":-38.54,
        "accuracy_m":12.0,"precision":"PRECISE",
    })

def test_context_history_location_endpoint_requires_true_consent_and_owner_session():
    path="/api/v1/client/location/context/observations"
    operation=DOC["paths"][path]["put"]
    assert operation["security"]==[{"ClientSession":[]}]
    assert operation["operationId"]=="putClientLocationContextObservation"
    assert "get" not in DOC["paths"][path]
    schema=DOC["components"]["schemas"]["ClientLocationContextWriteRequest"]
    assert schema["additionalProperties"] is False
    assert "context_history_opt_in" in schema["required"]
    assert schema["properties"]["context_history_opt_in"]["const"] is True
    validator=Draft202012Validator(
        schema,
        resolver=Draft202012Validator(DOC).resolver,
    )
    basic={
      "latitude":-3.71,"longitude":-38.54,"accuracy_m":15,
      "captured_at":"2026-11-16T15:30:00Z",
      "precision":"PRECISE",
    }
    assert not validator.is_valid(basic)
    assert not validator.is_valid({**basic,"context_history_opt_in":False})
    assert validator.is_valid({**basic,"context_history_opt_in":True})
    assert not validator.is_valid({**basic,"context_history_opt_in":True,"person_at_location":True})
