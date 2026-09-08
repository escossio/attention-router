from datetime import datetime, timedelta, timezone

from attention_router.application.platform.context import DatabaseStateRetriever, resolve_represented_subject
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import now_utc
from attention_router.infrastructure.models import EntityStateRow
from attention_router.infrastructure.repository import upsert_actor_binding


def _state(session, *, tenant_id, subject_id, status, expires_at=None):
    stamp = now_utc()
    session.add(
        EntityStateRow(
            id=f"state-{tenant_id}-{subject_id}-{status}",
            tenant_id=tenant_id,
            subject_type="ACTOR",
            subject_id=subject_id,
            state_namespace="presence",
            state_key="effective",
            state_value={"status": status},
            source="test",
            effective_at=stamp,
            expires_at=expires_at,
            version=1,
            updated_at=stamp,
        )
    )
    session.flush()


def test_represented_owner_resolution_is_tenant_scoped_and_presence_is_effective(session):
    tenant_b = "00000000-0000-4000-8000-000000000002"
    upsert_actor_binding(session, "test", "owner-a-external", "owner-a", "owner", metadata={"owner": True})
    upsert_actor_binding(session, "test", "owner-b-external", "owner-b", "owner", metadata={"owner": True}, tenant_id=tenant_b)
    _state(session, tenant_id=DEFAULT_TENANT_ID, subject_id="owner-a", status="sleeping")
    _state(session, tenant_id=tenant_b, subject_id="owner-b", status="available")

    assert resolve_represented_subject(session, DEFAULT_TENANT_ID).entity_id == "owner-a"
    assert resolve_represented_subject(session, tenant_b).entity_id == "owner-b"
    state = DatabaseStateRetriever(session).retrieve(DEFAULT_TENANT_ID, "owner-a", 20)
    assert state[0]["value"]["status"] == "sleeping"
    assert DatabaseStateRetriever(session).retrieve(tenant_b, "owner-a", 20) == []


def test_expired_presence_is_not_effective_and_missing_owner_fails_closed(session):
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)
    _state(session, tenant_id=DEFAULT_TENANT_ID, subject_id="unresolved-owner", status="sleeping", expires_at=expired)

    assert resolve_represented_subject(session, DEFAULT_TENANT_ID) is None
    assert DatabaseStateRetriever(session).retrieve(DEFAULT_TENANT_ID, "unresolved-owner", 20) == []
