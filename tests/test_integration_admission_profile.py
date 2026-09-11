"""Bounded parsing checks; these do not claim PostgreSQL concurrency coverage."""
import json
import secrets

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from attention_router.integrations.admission import _parse, admit_inbound


@pytest.mark.parametrize("body", [b'{"a":1,"a":2}', b'{"a":{"x":1,"x":2}}',
                                  b'NaN', b'Infinity', b'1e999', b'\xff', b'{',
                                  b'[' * 65 + b'0' + b']' * 65])
def test_invalid_json_profile(body):
    assert _parse(body) == (None, "INVALID_REQUEST")


def test_size_and_contract_validation():
    assert _parse(b' ' * 65537) == (None, "BODY_TOO_LARGE")
    assert _parse(json.dumps({"schema_version": "1"}).encode()) == (None, "INVALID_CONTRACT")


def test_sqlite_cannot_masquerade_as_transactional_proof():
    engine = create_engine("sqlite://")
    try:
        result = admit_inbound(sessionmaker(bind=engine), secrets.token_urlsafe(32), b'{}',
                                audience="test-ingress")
        assert result.code == "INGRESS_UNAVAILABLE" and result.receipt_id is None
    finally:
        engine.dispose()
