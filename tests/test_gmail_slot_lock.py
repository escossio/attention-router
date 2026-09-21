"""Sanitized failures at the new connection gate boundary."""
import traceback

import pytest

from attention_router.application import gmail_connection


def test_connection_gate_discards_database_diagnostics(monkeypatch):
    def fail(*args):
        raise RuntimeError('synthetic-secret-database-diagnostic')
    monkeypatch.setattr(gmail_connection, 'acquire_gmail_slot', fail)
    with pytest.raises(gmail_connection.GmailConnectionConflict) as caught:
        gmail_connection.GmailConnectionService._lock_slot(None, 'synthetic-slot')
    assert caught.value.__cause__ is caught.value.__context__ is None
    assert 'synthetic-secret' not in ''.join(traceback.format_exception(caught.value))
