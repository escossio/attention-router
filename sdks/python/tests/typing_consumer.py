"""Checked by mypy against the installed public API, not executed as a unit test."""

from typing import assert_type

from andy_integration_sdk import (
    InboundIntegrationEvent,
    IntegrationContractMessage,
    validate_contract,
)

# The ignore must become unused (and fail --strict) if required fields disappear.
missing: InboundIntegrationEvent = {"contract_type": "inbound_event"}  # type: ignore[typeddict-item]


def narrow(message: IntegrationContractMessage) -> str:
    if message["contract_type"] == "artifact_receipt":
        return message["content_sha256"]
    return message["correlation_id"]


def validate_unknown(value: object) -> None:
    assert_type(validate_contract(value), IntegrationContractMessage)
