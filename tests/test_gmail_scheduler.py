from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import sessionmaker

from attention_router.application.gmail_history import (
    GmailHistoryResult,
    GmailProductHistoryBusy,
    GmailProductHistoryStale,
)
from attention_router.application.gmail_product_runner import (
    GmailProductAuthorizationUnavailable,
    GmailProductProviderUnavailable,
)
from attention_router.config import Settings
from attention_router.infrastructure.gmail_scheduler import (
    discover_active_installation_ids,
    run_scheduler_cycle,
)
from attention_router.infrastructure.provider_authorization_models import (
    ProviderAuthorizationRow,
)

NOW = datetime(2026, 9, 21, 18, 0, tzinfo=UTC)

def _authorization(
    installation_id: str,
    *,
    status: str = "ACTIVE",
) -> ProviderAuthorizationRow:
    revoked_at = NOW if status == "REVOKED" else None
    return ProviderAuthorizationRow(
        id=installation_id,
        slot_key="slot-" + installation_id,
        tenant_id="tenant-" + installation_id,
        human_identity_id="human-" + installation_id,
        provider="GOOGLE",
        product="GMAIL",
        provider_account_hash="original-" + installation_id,
        gmail_history_id="10",
        granted_scopes=["https://www.googleapis.com/auth/gmail.metadata"],
        secret_nonce_b64url="nonce-" + installation_id,
        secret_ciphertext_b64url="cipher-" + installation_id,
        secret_key_version="v1",
        integration_binding_id="binding-" + installation_id,
        integration_credential_id="credential-" + installation_id,
        status=status,
        created_at=NOW,
        updated_at=NOW,
        revoked_at=revoked_at,
    )


def _session_factory(session):
    return sessionmaker(
        bind=session.get_bind(),
        expire_on_commit=False,
        future=True,
    )

def test_discovery_rotates_and_wraps_without_starvation(session):
    session.add_all(_authorization(value) for value in ("a", "b", "c", "d"))
    session.add(_authorization("revoked", status="REVOKED"))
    session.commit()

    assert discover_active_installation_ids(session, limit=2) == ("a", "b")
    assert discover_active_installation_ids(
        session, limit=2, after_id="b"
    ) == ("c", "d")
    assert discover_active_installation_ids(
        session, limit=2, after_id="d"
    ) == ("a", "b")


@pytest.mark.parametrize("limit", [0, 501, True, 1.5])
def test_discovery_rejects_unbounded_or_invalid_batch_limit(session, limit):
    with pytest.raises(
        ValueError,
        match="GMAIL_SCHEDULER_BATCH_LIMIT_OUT_OF_RANGE",
    ):
        discover_active_installation_ids(session, limit=limit)


class _Runner:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    def run_incremental(
        self,
        session,
        *,
        installation_id,
        max_results,
        max_pages,
        now,
    ):
        self.calls.append(
            (installation_id, max_results, max_pages, now)
        )
        row = session.get(ProviderAuthorizationRow, installation_id)
        row.provider_account_hash = "attempt-" + installation_id
        outcome = self.outcomes[installation_id]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_cycle_isolates_transactions_and_classifies_failures(
    session,
    caplog,
):
    ids = ("a", "b", "c", "d", "e", "f")
    session.add_all(_authorization(value) for value in ids)
    session.commit()
    factory = _session_factory(session)
    runner = _Runner(
        {
            "a": GmailHistoryResult(
                "a",
                initialized=True,
                accepted=2,
                duplicates=1,
                cursor_advanced=True,
            ),

            "b": GmailProductHistoryBusy(),
            "c": GmailProductHistoryStale(),
            "d": GmailProductAuthorizationUnavailable(),
            "e": GmailProductProviderUnavailable(),
            "f": RuntimeError("private-provider-error-must-not-be-logged"),
        }
    )
    stale_installations = set()

    result = run_scheduler_cycle(
        factory,
        runner,
        limit=6,
        max_results=7,
        max_pages=4,
        stale_installations=stale_installations,
        now=NOW,
    )

    assert result.selected == 6
    assert result.processed == 1
    assert result.initialized == 1
    assert result.busy == 1
    assert result.stale == 1
    assert result.unavailable == 1
    assert result.failed == 2
    assert result.accepted == 2
    assert result.duplicates == 1
    assert result.cursor_advanced == 1

    assert result.last_installation_id == "f"
    assert stale_installations == {"c"}
    assert all(call[1:] == (7, 4, NOW) for call in runner.calls)
    assert "private-provider-error-must-not-be-logged" not in caplog.text

    session.expire_all()
    assert session.get(
        ProviderAuthorizationRow, "a"
    ).provider_account_hash == "attempt-a"
    for installation_id in ("b", "c", "d", "e", "f"):
        assert session.get(
            ProviderAuthorizationRow,
            installation_id,
        ).provider_account_hash == "original-" + installation_id


def test_quarantined_stale_installation_is_not_called_again(session):
    session.add_all(_authorization(value) for value in ("a", "b"))
    session.commit()
    runner = _Runner(
        {
            "a": AssertionError("quarantined installation was called"),
            "b": GmailHistoryResult("b"),
        }
    )

    result = run_scheduler_cycle(
        _session_factory(session),
        runner,
        limit=2,

        max_results=5,
        max_pages=10,
        stale_installations={"a"},
        now=NOW,
    )

    assert result.selected == 2
    assert result.quarantined == 1
    assert result.processed == 1
    assert [call[0] for call in runner.calls] == ["b"]


def _settings(**overrides):
    return Settings(
        _env_file=None,
        admin_auth_enabled=False,
        internal_ingress_hmac_secret=(
            "synthetic-test-secret-that-is-long-enough"
        ),
        **overrides,
    )


def test_scheduler_configuration_defaults_fail_closed():
    configured = _settings()
    assert configured.gmail_product_scheduler_enabled is False
    assert configured.gmail_product_scheduler_poll_interval_seconds == 30
    assert configured.gmail_product_scheduler_batch_size == 20
    assert configured.gmail_product_scheduler_max_pages == 10


@pytest.mark.parametrize(

    ("field", "value", "message"),
    [
        (
            "gmail_product_scheduler_poll_interval_seconds",
            0,
            "GMAIL_PRODUCT_SCHEDULER_POLL_INTERVAL_SECONDS",
        ),
        (
            "gmail_product_scheduler_poll_interval_seconds",
            3601,
            "GMAIL_PRODUCT_SCHEDULER_POLL_INTERVAL_SECONDS",
        ),
        (
            "gmail_product_scheduler_batch_size",
            0,
            "GMAIL_PRODUCT_SCHEDULER_BATCH_SIZE",
        ),
        (
            "gmail_product_scheduler_batch_size",
            501,
            "GMAIL_PRODUCT_SCHEDULER_BATCH_SIZE",
        ),
        (
            "gmail_product_scheduler_max_pages",
            0,
            "GMAIL_PRODUCT_SCHEDULER_MAX_PAGES",
        ),
        (
            "gmail_product_scheduler_max_pages",
            11,
            "GMAIL_PRODUCT_SCHEDULER_MAX_PAGES",
        ),
    ],
)
def test_scheduler_configuration_bounds(field, value, message):

    with pytest.raises(ValidationError, match=message):
        _settings(**{field: value})


def test_scheduler_cannot_enable_without_governed_runner():
    with pytest.raises(
        ValidationError,
        match=(
            "GMAIL_PRODUCT_SCHEDULER_ENABLED requires "
            "GMAIL_PRODUCT_RUNNER_ENABLED=true"
        ),
    ):
        _settings(gmail_product_scheduler_enabled=True)


def test_scheduler_can_enable_only_on_full_gmail_product_boundary():
    configured = _settings(
        client_session_enabled=True,
        gmail_connect_enabled=True,
        gmail_product_runner_enabled=True,
        gmail_product_scheduler_enabled=True,
        google_workspace_oauth_client_id="synthetic-client",
        google_workspace_oauth_client_secret="synthetic-secret",
        provider_authorization_key_b64url="A" * 43,
    )
    assert configured.gmail_product_scheduler_enabled is True
