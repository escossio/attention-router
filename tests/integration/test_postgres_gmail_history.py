"""Real row locking and neutral-ingress replay on disposable PostgreSQL."""
from datetime import UTC, datetime
import json

import pytest
from sqlalchemy import select

from attention_router.application.gmail_history import GmailProductHistoryBusy
from attention_router.application.gmail_product_runner import GmailProductRunner, GmailProductIngressFailed
from attention_router.config import settings
from attention_router.infrastructure.provider_authorization_models import ProviderAuthorizationRow
from attention_router.integrations.admission import admit_inbound
from attention_router.integrations.gmail_connector import IntegrationIngressResponse
from test_gmail_history import Reader, record
from test_gmail_product_runner import _seed_connected, _enabled, FakeRefresher

pytestmark = pytest.mark.postgres


def test_cursor_lock_persistence_and_real_admission_replay(Session, monkeypatch):
    stamp = datetime.now(UTC)
    with Session() as seed:
        installation = _seed_connected(seed, monkeypatch, now=stamp)
    _enabled(monkeypatch)
    reader = Reader()
    attempts = []
    class Ingress:
        def __init__(self, bearer):
            self.bearer = bearer
        def send(self, payload):
            raw = json.dumps(payload, ensure_ascii=True, allow_nan=False,
                             separators=(",", ":")).encode()
            receipt = admit_inbound(Session, self.bearer, raw,
                                    audience=settings.integration_ingress_audience)
            attempts.append(receipt.code)
            # Simulate an ambiguous transport failure AFTER durable acceptance.
            if len(attempts) == 1:
                raise RuntimeError("synthetic-transport-failure")
            status = {"ACCEPTED": "accepted", "DUPLICATE": "duplicate"}[receipt.code]
            return IntegrationIngressResponse(202, {"status": status})
    runner = GmailProductRunner(settings=settings, token_refresher=FakeRefresher(),
                                reader_factory=lambda token: reader, ingress_factory=Ingress)
    with Session() as first, Session() as second:
        assert runner.run_incremental(first, installation_id=installation, now=stamp).initialized
        with pytest.raises(GmailProductHistoryBusy):
            runner.run_incremental(second, installation_id=installation, now=stamp)
        second.rollback()
        first.commit()
        assert second.get(ProviderAuthorizationRow, installation).gmail_history_id == "10"
        second.rollback()
        reader.pages = [{"historyId": "20", "history": [record(12, "one")]}]
        with pytest.raises(GmailProductIngressFailed):
            runner.run_incremental(first, installation_id=installation, now=stamp)
        first.rollback()
        reader.calls.clear()
        assert runner.run_incremental(first, installation_id=installation, now=stamp).duplicates == 1
        first.commit()
        assert attempts == ["ACCEPTED", "DUPLICATE"]
        assert second.scalar(select(ProviderAuthorizationRow.gmail_history_id)) == "20"


def test_migration_roundtrip_and_populated_downgrade_guard(Session, monkeypatch, pg_url):
    import os
    import subprocess
    import sys
    from sqlalchemy import inspect, text
    def migrate(direction, revision):
        env = dict(os.environ, DATABASE_URL=pg_url)
        return subprocess.run([sys.executable, "-m", "alembic", direction, revision],
                              env=env, capture_output=True, text=True)
    assert migrate("downgrade", "0045_provider_authorization_v1").returncode == 0
    with Session() as session:
        assert "gmail_history_id" not in {c["name"] for c in inspect(session.bind).get_columns(
            "provider_authorizations"
        )}
    assert migrate("upgrade", "head").returncode == 0
    with Session() as session:
        installation = _seed_connected(session, monkeypatch)
        row = session.get(ProviderAuthorizationRow, installation)
        assert row.gmail_history_id is None
        row.gmail_history_id = "10"
        session.commit()
    result = migrate("downgrade", "0045_provider_authorization_v1")
    assert result.returncode != 0
    assert "GMAIL_HISTORY_DOWNGRADE_REQUIRES_DATA_EXPORT" in result.stderr
    with Session() as session:
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0049_client_command_channel_v1"
        )
        assert session.get(ProviderAuthorizationRow, installation).gmail_history_id == "10"
