from datetime import UTC, datetime, timedelta

import pytest

from attention_router.platform.transport_canary_authorization import (
    CANARY_TEXT,
    OPERATION,
    TRANSPORT,
    consume_transport_canary_authorization,
    prepare_transport_canary_authorization,
)


def test_transport_canary_is_prepared_with_immutable_scope_and_ttl(session):
    now = datetime.now(UTC)
    row = prepare_transport_canary_authorization(session, phone_number_id="phone-id", correlation_id="corr", now=now)
    assert (row.transport, row.operation, row.phone_number_id, row.text, row.recipient) == (TRANSPORT, OPERATION, "phone-id", CANARY_TEXT, "PENDING_OPERATOR_CONFIRMATION")
    assert row.expires_at == now + timedelta(seconds=300)
    assert row.scope_fingerprint


def test_transport_canary_consume_is_single_use_and_scope_bound(session):
    row = prepare_transport_canary_authorization(session, phone_number_id="phone-id", correlation_id="corr")
    row.status = "AUTHORIZED"
    row.recipient = "+15550001111"
    from attention_router.platform.transport_canary_authorization import scope_fingerprint
    row.scope_fingerprint = scope_fingerprint(transport=TRANSPORT, operation=OPERATION, phone_number_id="phone-id", text=CANARY_TEXT, recipient=row.recipient)
    session.commit()
    consume_transport_canary_authorization(session, row.id, transport=TRANSPORT, operation=OPERATION, phone_number_id="phone-id", text=CANARY_TEXT, recipient="+15550001111")
    session.commit()
    with pytest.raises(PermissionError, match="INVALID_OR_ALREADY_CONSUMED"):
        consume_transport_canary_authorization(session, row.id, transport=TRANSPORT, operation=OPERATION, phone_number_id="phone-id", text="alterado", recipient="+15550001111")
