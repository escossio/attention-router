import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts/client/v1/client-api.openapi.json"
CURRENT = "/api/v1/client/location/current"


def load_contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def validator_for(schema_name: str) -> Draft202012Validator:
    document = load_contract()
    return Draft202012Validator(
        document["components"]["schemas"][schema_name],
        format_checker=FormatChecker(),
        resolver=Draft202012Validator(document).resolver,
    )


def test_v04a_adds_exact_authenticated_current_location_surface():
    operation = load_contract()["paths"][CURRENT]
    assert operation["put"]["security"] == [{"ClientSession": []}]
    assert operation["get"]["security"] == [{"ClientSession": []}]
    assert operation["put"]["operationId"] == "putCurrentClientLocation"
    assert operation["get"]["operationId"] == "getCurrentClientLocation"


def test_write_request_contains_observation_only_not_authority():
    schema = load_contract()["components"]["schemas"]["ClientLocationWriteRequest"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "latitude",
        "longitude",
        "accuracy_m",
        "captured_at",
    ]
    assert set(schema["properties"]) == {
        "latitude",
        "longitude",
        "accuracy_m",
        "captured_at",
        "precision",
    }
    raw = json.dumps(schema)
    for forbidden in (
        "human_identity_id",
        "device_id",
        "tenant_id",
        "membership_id",
        "session_id",
        "session_token",
    ):
        assert forbidden not in raw


def test_location_contract_is_foreground_snapshot_not_tracking_surface():
    raw = json.dumps(load_contract()["paths"][CURRENT]) + json.dumps(
        load_contract()["components"]["schemas"]["ClientLocationWriteRequest"]
    )
    for forbidden in (
        "background",
        "geofence",
        "history",
        "track",
        "speed",
        "bearing",
        "altitude",
        "address",
        "google",
    ):
        assert forbidden not in raw.lower()


def test_precision_vocabulary_is_bounded():
    precision = load_contract()["components"]["schemas"]["ClientLocationPrecision"]
    assert set(precision["enum"]) == {"PRECISE", "APPROXIMATE"}


def test_snapshot_contains_server_derived_authority_and_observation():
    schema = load_contract()["components"]["schemas"]["ClientLocationSnapshot"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "contract_version",
        "location_snapshot_id",
        "human_identity_id",
        "device_id",
        "tenant_id",
        "latitude",
        "longitude",
        "accuracy_m",
        "captured_at",
        "received_at",
    }
    assert set(schema["properties"]) == set(schema["required"]) | {"precision"}


def test_v04a_wire_examples_validate():
    write = {
        "latitude": -3.73,
        "longitude": -38.54,
        "accuracy_m": 12.5,
        "captured_at": "2026-09-18T23:30:00Z",
        "precision": "PRECISE",
    }
    snapshot = {
        "contract_version": "1",
        "location_snapshot_id": "cloc_" + "a" * 24,
        "human_identity_id": "hid_" + "b" * 24,
        "device_id": "cdev_" + "c" * 24,
        "tenant_id": "tnt_synthetic",
        "latitude": -3.73,
        "longitude": -38.54,
        "accuracy_m": 12.5,
        "precision": "PRECISE",
        "captured_at": "2026-09-18T23:30:00Z",
        "received_at": "2026-09-18T23:30:02Z",
    }
    assert validator_for("ClientLocationWriteRequest").is_valid(write)
    assert validator_for("ClientLocationSnapshot").is_valid(snapshot)


def test_error_vocabulary_is_bounded():
    error = load_contract()["components"]["schemas"]["ClientLocationErrorResponse"]
    assert set(error["properties"]["code"]["enum"]) == {
        "CLIENT_LOCATION_DISABLED",
        "CLIENT_LOCATION_INVALID",
        "CLIENT_LOCATION_STALE",
        "CLIENT_LOCATION_FUTURE",
        "CLIENT_LOCATION_UNAUTHENTICATED",
        "CLIENT_LOCATION_AUTHORITY_REJECTED",
        "CLIENT_LOCATION_NOT_FOUND",
        "CLIENT_LOCATION_UNAVAILABLE",
    }
