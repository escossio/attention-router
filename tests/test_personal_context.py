from datetime import timedelta

import pytest

from attention_router.application.personal_context import (
    PersonalContextUnavailable,
    build_personal_context,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import (
    FactRow,
    MemoryActorRow,
    MemoryClaimRow,
    TenantRow,
)
from attention_router.infrastructure.repository import upsert_actor_binding


def _memory_actor(session, tenant_id: str, actor_key: str) -> MemoryActorRow:
    stamp = now_utc()
    row = MemoryActorRow(
        id=new_id(),
        tenant_id=tenant_id,
        actor_key=actor_key,
        metadata_json={},
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(row)
    session.flush()
    return row


def _claim(
    session,
    actor: MemoryActorRow,
    predicate: str,
    value: str,
    *,
    sensitivity: str = "NORMAL",
    status: str = "ACTIVE",
    valid_until=None,
) -> MemoryClaimRow:
    stamp = now_utc()
    row = MemoryClaimRow(
        id=new_id(),
        subject_actor_id=actor.id,
        subject_entity_id=None,
        predicate=predicate,
        object_type="TEXT",
        object_text=value,
        object_actor_id=None,
        object_entity_id=None,
        object_json=None,
        context={},
        confidence=0.97,
        sensitivity_class=sensitivity,
        source_quality="SELF_REPORTED",
        valid_from=stamp - timedelta(minutes=1),
        valid_until=valid_until,
        status=status,
        staleness_class="STABLE",
        supersedes_claim_id=None,
        conflict_group_id=None,
        first_observed_at=stamp,
        last_observed_at=stamp,
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(row)
    session.flush()
    return row


def _tenant(session, tenant_id: str) -> None:
    stamp = now_utc()
    session.add(TenantRow(
        id=tenant_id,
        slug=tenant_id,
        name=f"Synthetic {tenant_id}",
        status="ACTIVE",
        created_at=stamp,
        updated_at=stamp,
    ))
    session.flush()


def test_personal_context_unifies_multiple_owner_source_aliases(session):
    upsert_actor_binding(
        session, "whatsapp", "owner-wa", "owner-a", "owner",
        metadata={"owner": True},
    )
    upsert_actor_binding(
        session, "email", "owner@example.invalid", "owner-a", "owner",
        metadata={"owner": True},
    )
    actor = _memory_actor(session, DEFAULT_TENANT_ID, "owner@example.invalid")
    _claim(session, actor, "professional.role", "network engineer")

    snapshot = build_personal_context(session, DEFAULT_TENANT_ID)

    assert snapshot.represented_actor_id == "owner-a"
    assert snapshot.source_alias_count == 3
    assert [claim.predicate for claim in snapshot.claims] == ["professional.role"]
    assert snapshot.claims[0].value_text == "network engineer"


def test_personal_context_excludes_secret_inactive_and_expired_claims(session):
    upsert_actor_binding(
        session, "test", "owner-external", "owner-a", "owner",
        metadata={"owner": True},
    )
    actor = _memory_actor(session, DEFAULT_TENANT_ID, "owner-external")
    _claim(session, actor, "identity.preferred_name", "Leo")
    _claim(session, actor, "security.password", "must-not-leak", sensitivity="SECRET")
    _claim(session, actor, "professional.old_role", "old", status="SUPERSEDED")
    _claim(
        session,
        actor,
        "context.expired",
        "expired",
        valid_until=now_utc() - timedelta(seconds=1),
    )

    snapshot = build_personal_context(session, DEFAULT_TENANT_ID)

    assert [claim.predicate for claim in snapshot.claims] == ["identity.preferred_name"]


def test_personal_context_is_tenant_scoped_even_when_aliases_match(session):
    tenant_b = "00000000-0000-4000-8000-000000000099"
    _tenant(session, tenant_b)
    upsert_actor_binding(
        session, "test", "shared-external", "owner-a", "owner",
        metadata={"owner": True},
    )
    upsert_actor_binding(
        session, "test", "shared-external", "owner-b", "owner",
        metadata={"owner": True}, tenant_id=tenant_b,
    )
    actor_a = _memory_actor(session, DEFAULT_TENANT_ID, "shared-external")
    actor_b = _memory_actor(session, tenant_b, "shared-external")
    _claim(session, actor_a, "identity.preferred_name", "Alpha")
    _claim(session, actor_b, "identity.preferred_name", "Beta")

    snapshot_a = build_personal_context(session, DEFAULT_TENANT_ID)
    snapshot_b = build_personal_context(session, tenant_b)

    assert [claim.value_text for claim in snapshot_a.claims] == ["Alpha"]
    assert [claim.value_text for claim in snapshot_b.claims] == ["Beta"]


def test_personal_context_includes_canonical_owner_facts_with_provenance(session):
    upsert_actor_binding(
        session, "test", "owner-external", "owner-a", "owner",
        metadata={"owner": True},
    )
    stamp = now_utc()
    session.add(FactRow(
        id=new_id(),
        tenant_id=DEFAULT_TENANT_ID,
        subject_type="ACTOR",
        subject_id="owner-a",
        predicate="work.primary_site",
        value_json={"site": "branch-235"},
        value_ref=None,
        fact_class="ASSERTED",
        source_type="USER_INPUT",
        source_ref="synthetic-fixture",
        confidence=1.0,
        observed_at=stamp,
        valid_from=stamp - timedelta(minutes=1),
        valid_until=None,
        supersedes_fact_id=None,
        metadata_json={},
        created_at=stamp,
    ))
    session.flush()

    snapshot = build_personal_context(session, DEFAULT_TENANT_ID)

    assert snapshot.facts[0].predicate == "work.primary_site"
    assert snapshot.facts[0].value_json == {"site": "branch-235"}
    assert snapshot.facts[0].source_type == "USER_INPUT"
    assert snapshot.facts[0].source_ref == "synthetic-fixture"


def test_personal_context_fails_closed_for_missing_or_ambiguous_owner(session):
    with pytest.raises(PersonalContextUnavailable, match="REPRESENTED_OWNER_NOT_UNIQUE"):
        build_personal_context(session, DEFAULT_TENANT_ID)

    upsert_actor_binding(
        session, "test-a", "owner-a-ext", "owner-a", "owner",
        metadata={"owner": True},
    )
    upsert_actor_binding(
        session, "test-b", "owner-b-ext", "owner-b", "owner",
        metadata={"owner": True},
    )
    with pytest.raises(PersonalContextUnavailable, match="REPRESENTED_OWNER_NOT_UNIQUE"):
        build_personal_context(session, DEFAULT_TENANT_ID)


def test_personal_context_limit_is_bounded(session):
    with pytest.raises(ValueError, match="PERSONAL_CONTEXT_LIMIT_OUT_OF_RANGE"):
        build_personal_context(session, DEFAULT_TENANT_ID, limit=0)
