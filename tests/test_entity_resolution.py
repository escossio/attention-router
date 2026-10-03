from __future__ import annotations

from sqlalchemy import func, select

import pytest

from attention_router.application.cognitive_graph import build_cognitive_graph_slice
from attention_router.application.entity_resolution import (
    EntityResolutionError,
    IdentityEvidenceInput,
    confirm_entity_resolution,
    mark_entity_resolution_ambiguous,
    propose_entity_resolution,
    reject_entity_resolution,
    revoke_entity_alias,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.cognitive_graph import CognitiveRelationKind
from attention_router.domain.models import now_utc
from attention_router.infrastructure.entity_resolution_models import (
    EntityAliasResolutionRow,
    EntityResolutionCandidateRow,
    EntityResolutionEvidenceRow,
)
from attention_router.infrastructure.models import (
    ExecutionIntentRow,
    OutboxMessageRow,
    TenantRow,
)
from attention_router.infrastructure.repository import upsert_actor_binding


OWNER = "actor-owner-v2c"
ANGELO_WHATSAPP = "actor-angelo-whatsapp"
ANGELO_CONTRACT = "actor-angelo-contract"


def _seed_actor(
    session,
    *,
    actor_key: str,
    source: str,
    external_id: str,
    display_name: str,
    tenant_id: str = DEFAULT_TENANT_ID,
    category: str = "contact",
    metadata: dict | None = None,
):
    return upsert_actor_binding(
        session,
        source=source,
        external_actor_id=external_id,
        actor_key=actor_key,
        actor_category=category,
        display_name=display_name,
        metadata=metadata or {},
        tenant_id=tenant_id,
    )


def _seed_default_world(session):
    _seed_actor(
        session,
        actor_key=OWNER,
        source="whatsapp",
        external_id="owner-ext",
        display_name="Owner",
        category="owner",
        metadata={"owner": True},
    )
    left = _seed_actor(
        session,
        actor_key=ANGELO_WHATSAPP,
        source="whatsapp",
        external_id="wa-angelo",
        display_name="Ângelo",
    )
    right = _seed_actor(
        session,
        actor_key=ANGELO_CONTRACT,
        source="contract",
        external_id="contract-angelo",
        display_name="Angelo Silva",
    )
    session.flush()
    return left, right


def _evidence():
    return [
        IdentityEvidenceInput(
            evidence_type="NORMALIZED_PHONE",
            source_ref="contact:+5585999990000",
            confidence=0.96,
            independence_key="phone:+5585999990000",
            metadata={"normalization": "E164"},
        ),
        IdentityEvidenceInput(
            evidence_type="SOURCE_ALIAS",
            source_ref="contract:tenant-42",
            confidence=0.82,
            independence_key="contract:tenant-42",
            metadata={"role": "tenant"},
        ),
    ]


def test_v2c_candidate_is_deterministic_and_evidence_backed(session):
    _seed_default_world(session)

    first, created = propose_entity_resolution(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key_a=ANGELO_WHATSAPP,
        actor_key_b=ANGELO_CONTRACT,
        evidence=_evidence(),
    )
    second, second_created = propose_entity_resolution(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key_a=ANGELO_CONTRACT,
        actor_key_b=ANGELO_WHATSAPP,
        evidence=list(reversed(_evidence())),
    )

    assert created is True
    assert second_created is False
    assert first.id == second.id
    assert first.left_actor_key < first.right_actor_key
    assert first.state == "PROPOSED"
    assert first.evidence_count == 2
    assert first.confidence == pytest.approx((0.96 + 0.82) / 2)

    evidence_rows = session.scalars(
        select(EntityResolutionEvidenceRow).where(
            EntityResolutionEvidenceRow.candidate_id == first.id
        )
    ).all()
    assert len(evidence_rows) == 2
    assert {row.evidence_type for row in evidence_rows} == {
        "NORMALIZED_PHONE",
        "SOURCE_ALIAS",
    }


def test_v2c_duplicate_evidence_family_does_not_double_count_confidence(session):
    _seed_default_world(session)

    candidate, _ = propose_entity_resolution(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key_a=ANGELO_WHATSAPP,
        actor_key_b=ANGELO_CONTRACT,
        evidence=[
            IdentityEvidenceInput(
                evidence_type="NORMALIZED_PHONE",
                source_ref="whatsapp:+5585999990000",
                confidence=0.90,
                independence_key="phone:+5585999990000",
            ),
            IdentityEvidenceInput(
                evidence_type="SOURCE_ALIAS",
                source_ref="contact:+5585999990000",
                confidence=0.95,
                independence_key="phone:+5585999990000",
            ),
        ],
    )

    assert candidate.confidence == pytest.approx(0.95)
    assert candidate.evidence_count == 2


def test_v2c_name_only_evidence_is_not_admissible(session):
    _seed_default_world(session)

    with pytest.raises(
        EntityResolutionError,
        match="ENTITY_RESOLUTION_EVIDENCE_TYPE_UNSUPPORTED",
    ):
        propose_entity_resolution(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            actor_key_a=ANGELO_WHATSAPP,
            actor_key_b=ANGELO_CONTRACT,
            evidence=[
                IdentityEvidenceInput(
                    evidence_type="DISPLAY_NAME_ONLY",
                    source_ref="display:angelo",
                    confidence=0.99,
                    independence_key="display:angelo",
                )
            ],
        )


def test_v2c_rejected_candidate_is_durable_against_same_evidence(session):
    _seed_default_world(session)
    candidate, _ = propose_entity_resolution(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key_a=ANGELO_WHATSAPP,
        actor_key_b=ANGELO_CONTRACT,
        evidence=_evidence(),
    )

    rejected = reject_entity_resolution(
        session,
        candidate_id=candidate.id,
        decision_actor_key=OWNER,
        decision_ref="owner-command:reject-001",
    )
    assert rejected.state == "REJECTED"

    replay, created = propose_entity_resolution(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key_a=ANGELO_WHATSAPP,
        actor_key_b=ANGELO_CONTRACT,
        evidence=_evidence(),
    )
    assert created is False
    assert replay.id == candidate.id
    assert replay.state == "REJECTED"


def test_v2c_ambiguous_candidate_fails_closed(session):
    _seed_default_world(session)
    candidate, _ = propose_entity_resolution(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key_a=ANGELO_WHATSAPP,
        actor_key_b=ANGELO_CONTRACT,
        evidence=_evidence(),
    )

    ambiguous = mark_entity_resolution_ambiguous(
        session,
        candidate_id=candidate.id,
    )
    assert ambiguous.state == "AMBIGUOUS"
    assert session.scalar(
        select(func.count()).select_from(EntityAliasResolutionRow)
    ) == 0


def test_v2c_confirmation_creates_reversible_alias_and_graph_edge(session):
    left, right = _seed_default_world(session)
    left_actor_before = left.actor_key
    right_actor_before = right.actor_key

    candidate, _ = propose_entity_resolution(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key_a=ANGELO_WHATSAPP,
        actor_key_b=ANGELO_CONTRACT,
        evidence=_evidence(),
    )
    confirmed, alias = confirm_entity_resolution(
        session,
        candidate_id=candidate.id,
        canonical_actor_key=ANGELO_WHATSAPP,
        decision_actor_key=OWNER,
        decision_ref="owner-command:confirm-001",
    )

    assert confirmed.state == "CONFIRMED"
    assert confirmed.canonical_actor_key == ANGELO_WHATSAPP
    assert alias.alias_actor_key == ANGELO_CONTRACT
    assert alias.canonical_actor_key == ANGELO_WHATSAPP
    assert alias.state == "ACTIVE"

    # Source observations remain untouched.
    session.refresh(left)
    session.refresh(right)
    assert left.actor_key == left_actor_before
    assert right.actor_key == right_actor_before

    graph = build_cognitive_graph_slice(session, DEFAULT_TENANT_ID)
    aliases = [
        edge
        for edge in graph.edges
        if edge.relation_kind is CognitiveRelationKind.IDENTITY_ALIAS
    ]
    assert len(aliases) == 1
    assert aliases[0].source_node_id == f"person:{ANGELO_CONTRACT}"
    assert aliases[0].target_node_id == f"person:{ANGELO_WHATSAPP}"
    assert aliases[0].semantic_relation == "ALIAS_OF"

    revoked = revoke_entity_alias(
        session,
        alias_id=alias.id,
        decision_actor_key=OWNER,
        decision_ref="owner-command:revoke-001",
    )
    assert revoked.state == "REVOKED"
    assert revoked.decision_ref == "owner-command:confirm-001"
    assert revoked.revoked_by_actor_key == OWNER
    assert revoked.revocation_ref == "owner-command:revoke-001"

    graph_after = build_cognitive_graph_slice(session, DEFAULT_TENANT_ID)
    assert not any(
        edge.relation_kind is CognitiveRelationKind.IDENTITY_ALIAS
        for edge in graph_after.edges
    )

    with pytest.raises(
        EntityResolutionError,
        match="ENTITY_RESOLUTION_CONFIRMED_ALIAS_INACTIVE",
    ):
        confirm_entity_resolution(
            session,
            candidate_id=candidate.id,
            canonical_actor_key=ANGELO_WHATSAPP,
            decision_actor_key=OWNER,
            decision_ref="owner-command:confirm-replay",
        )


def test_v2c_owner_decision_fails_closed_when_owner_is_ambiguous(session):
    _seed_default_world(session)
    _seed_actor(
        session,
        actor_key="owner-other",
        source="other",
        external_id="owner-other-ext",
        display_name="Other Owner",
        category="owner",
        metadata={"owner": True},
    )
    candidate, _ = propose_entity_resolution(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key_a=ANGELO_WHATSAPP,
        actor_key_b=ANGELO_CONTRACT,
        evidence=_evidence(),
    )

    with pytest.raises(
        EntityResolutionError,
        match="ENTITY_RESOLUTION_OWNER_AMBIGUOUS",
    ):
        confirm_entity_resolution(
            session,
            candidate_id=candidate.id,
            canonical_actor_key=ANGELO_WHATSAPP,
            decision_actor_key=OWNER,
            decision_ref="owner-command:confirm-ambiguous",
        )


def test_v2c_cross_tenant_resolution_is_impossible(session):
    tenant_b = "00000000-0000-4000-8000-00000000c222"
    stamp = now_utc()
    session.add(
        TenantRow(
            id=tenant_b,
            slug="v2c-tenant-b",
            name="V2C Tenant B",
            status="ACTIVE",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    _seed_actor(
        session,
        actor_key=ANGELO_WHATSAPP,
        source="whatsapp",
        external_id="tenant-b-angelo",
        display_name="Ângelo",
        tenant_id=tenant_b,
    )
    _seed_actor(
        session,
        actor_key=ANGELO_CONTRACT,
        source="contract",
        external_id="tenant-b-contract",
        display_name="Angelo Silva",
        tenant_id=tenant_b,
    )
    _seed_actor(
        session,
        actor_key=OWNER,
        source="whatsapp",
        external_id="tenant-b-owner",
        display_name="Owner B",
        tenant_id=tenant_b,
        category="owner",
        metadata={"owner": True},
    )

    # Only one actor exists in the default tenant: cross-tenant lookup must not
    # borrow the matching actor key from tenant B.
    _seed_actor(
        session,
        actor_key=ANGELO_WHATSAPP,
        source="whatsapp",
        external_id="default-angelo",
        display_name="Ângelo",
    )

    with pytest.raises(
        EntityResolutionError,
        match="ENTITY_RESOLUTION_ACTOR_NOT_FOUND",
    ):
        propose_entity_resolution(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            actor_key_a=ANGELO_WHATSAPP,
            actor_key_b=ANGELO_CONTRACT,
            evidence=_evidence(),
        )


def test_v2c_resolution_creates_zero_execution_or_outbound_authority(session):
    _seed_default_world(session)
    candidate, _ = propose_entity_resolution(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key_a=ANGELO_WHATSAPP,
        actor_key_b=ANGELO_CONTRACT,
        evidence=_evidence(),
    )
    confirm_entity_resolution(
        session,
        candidate_id=candidate.id,
        canonical_actor_key=ANGELO_WHATSAPP,
        decision_actor_key=OWNER,
        decision_ref="owner-command:confirm-no-authority",
    )

    assert session.scalar(
        select(func.count()).select_from(ExecutionIntentRow)
    ) == 0
    assert session.scalar(
        select(func.count()).select_from(OutboxMessageRow)
    ) == 0
