from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from attention_router.application.personal_context_bootstrap import (
    PersonalContextBootstrapError,
)
from attention_router.application.personal_context_bootstrap_product import (
    PersonalContextBootstrapProductService,
)
from attention_router.config import Settings
from attention_router.domain.models import new_id
from attention_router.infrastructure.client_bootstrap_models import (
    ClientTenantMembershipRow,
)
from attention_router.infrastructure.models import ActorBindingRow, TenantRow
from attention_router.infrastructure.personal_context_bootstrap_models import (
    PersonalContextBootstrapRunRow,
)


TENANT = "00000000-0000-4000-8000-000000000255"
HUMAN = "human-bootstrap-255"
OWNER = "owner-bootstrap-255"


class FakeClientSessions:
    def authenticated_bootstrap(self, session, *, session_token, now=None):
        assert session_token == "session-token"
        return SimpleNamespace(
            human_identity_id=HUMAN,
            active_tenant_id=TENANT,
            device=SimpleNamespace(device_id="device-bootstrap-255"),
        )


def _scope(session):
    stamp = datetime(2026, 10, 3, tzinfo=UTC)
    session.add(
        TenantRow(
            id=TENANT,
            slug="bootstrap-255",
            name="Bootstrap 255",
            status="ACTIVE",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    session.add(
        ClientTenantMembershipRow(
            id=new_id(),
            human_identity_id=HUMAN,
            tenant_id=TENANT,
            role="OWNER",
            status="ACTIVE",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    session.add(
        ActorBindingRow(
            id=new_id(),
            tenant_id=TENANT,
            source="wwebjs",
            external_actor_id="owner@c.us",
            actor_key=OWNER,
            display_name="Owner",
            actor_category="owner",
            active_context=None,
            is_active=True,
            binding_metadata={"owner": True},
            created_at=stamp,
            updated_at=stamp,
        )
    )
    session.flush()


def _service():
    return PersonalContextBootstrapProductService(
        settings=Settings(
            _env_file=None,
            whatsapp_history_source_account="owner-whatsapp",
        ),
        client_sessions=FakeClientSessions(),
    )


def test_start_derives_authority_and_is_idempotent(session):
    _scope(session)
    service = _service()

    first = service.start(
        session,
        session_token="session-token",
        client_request_id="request-1",
        chat_keys=("chat-a", "chat-b"),
    )
    second = service.start(
        session,
        session_token="session-token",
        client_request_id="request-1",
        chat_keys=("chat-a", "chat-b"),
    )

    assert first.run_id == second.run_id
    row = session.get(PersonalContextBootstrapRunRow, first.run_id)
    assert row.tenant_id == TENANT
    assert row.owner_human_identity_id == HUMAN
    assert row.represented_owner_actor_key == OWNER
    assert row.source_kind == "WHATSAPP_TEXT"
    assert row.source_account == "owner-whatsapp"
    assert row.source_selection == {"chat_keys": ["chat-a", "chat-b"]}
    assert row.state == "QUEUED"
    assert "session-token" not in row.consent_ref
    assert "request-1" not in row.consent_ref


def test_status_control_and_list_are_owner_scoped(session):
    _scope(session)
    service = _service()
    run = service.start(
        session,
        session_token="session-token",
        client_request_id="request-control",
    )

    paused = service.control(
        session,
        session_token="session-token",
        run_id=run.run_id,
        action="PAUSE",
    )
    assert paused.state == "PAUSED"
    resumed = service.control(
        session,
        session_token="session-token",
        run_id=run.run_id,
        action="RESUME",
    )
    assert resumed.state == "QUEUED"
    listed = service.list_recent(
        session,
        session_token="session-token",
    )
    assert [item.run_id for item in listed] == [run.run_id]


def test_product_service_rejects_ambiguous_or_invalid_selection(session):
    _scope(session)
    stamp = datetime(2026, 10, 3, tzinfo=UTC)
    session.add(
        ActorBindingRow(
            id=new_id(),
            tenant_id=TENANT,
            source="email",
            external_actor_id="other@example.invalid",
            actor_key="different-owner",
            display_name="Other",
            actor_category="owner",
            active_context=None,
            is_active=True,
            binding_metadata={"owner": True},
            created_at=stamp,
            updated_at=stamp,
        )
    )
    session.flush()

    with pytest.raises(
        PersonalContextBootstrapError,
        match="BOOTSTRAP_REPRESENTED_OWNER_AMBIGUOUS",
    ):
        _service().start(
            session,
            session_token="session-token",
            client_request_id="request-ambiguous",
        )
