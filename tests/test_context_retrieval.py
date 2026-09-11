from datetime import timedelta

import pytest

from attention_router.application.context_retrieval import retrieve_personal_context
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import FactRow, MemoryActorRow, MemoryClaimRow, TenantRow
from attention_router.infrastructure.repository import upsert_actor_binding


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


def _owner(session, tenant_id: str, *, actor_key: str, external_id: str) -> MemoryActorRow:
    upsert_actor_binding(
        session,
        "test",
        external_id,
        actor_key,
        "owner",
        metadata={"owner": True},
        tenant_id=tenant_id,
    )
    stamp = now_utc()
    actor = MemoryActorRow(
        id=new_id(),
        tenant_id=tenant_id,
        actor_key=external_id,
        metadata_json={},
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(actor)
    session.flush()
    return actor


def _claim(
    session,
    actor: MemoryActorRow,
    predicate: str,
    value: str,
    *,
    sensitivity: str = "NORMAL",
    confidence: float = 0.95,
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
        confidence=confidence,
        sensitivity_class=sensitivity,
        source_quality="SELF_REPORTED",
        valid_from=stamp - timedelta(minutes=1),
        valid_until=None,
        status="ACTIVE",
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


def _fact(session, tenant_id: str, actor_key: str, predicate: str, value: dict) -> FactRow:
    stamp = now_utc()
    row = FactRow(
        id=new_id(),
        tenant_id=tenant_id,
        subject_type="ACTOR",
        subject_id=actor_key,
        predicate=predicate,
        value_json=value,
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
    )
    session.add(row)
    session.flush()
    return row


def test_retrieval_selects_pix_and_ignores_unrelated_context(session):
    actor = _owner(
        session,
        DEFAULT_TENANT_ID,
        actor_key="owner-a",
        external_id="owner-ext",
    )
    _claim(session, actor, "payment.pix.primary", "leo@example.invalid")
    _claim(session, actor, "professional.role", "network engineer")
    _claim(session, actor, "communication.answer_length", "short")

    result = retrieve_personal_context(
        session,
        DEFAULT_TENANT_ID,
        "Qual é o meu Pix principal?",
    )

    assert [item.predicate for item in result.items] == ["payment.pix.primary"]
    assert result.items[0].value_text == "leo@example.invalid"
    assert result.items[0].matched_terms == ("pix", "principal")
    assert result.retrieval_method == "LEXICAL_V0"


def test_retrieval_matches_structured_fact_values(session):
    _owner(
        session,
        DEFAULT_TENANT_ID,
        actor_key="owner-a",
        external_id="owner-ext",
    )
    _fact(
        session,
        DEFAULT_TENANT_ID,
        "owner-a",
        "work.primary_site",
        {"site": "filial 235"},
    )

    result = retrieve_personal_context(
        session,
        DEFAULT_TENANT_ID,
        "Qual é a filial 235?",
    )

    assert len(result.items) == 1
    assert result.items[0].kind == "FACT"
    assert result.items[0].predicate == "work.primary_site"
    assert result.items[0].value_json == {"site": "filial 235"}
    assert "235" in result.items[0].matched_terms


def test_retrieval_is_tenant_scoped(session):
    tenant_b = "00000000-0000-4000-8000-000000000088"
    _tenant(session, tenant_b)
    actor_a = _owner(
        session,
        DEFAULT_TENANT_ID,
        actor_key="owner-a",
        external_id="owner-ext-a",
    )
    actor_b = _owner(
        session,
        tenant_b,
        actor_key="owner-b",
        external_id="owner-ext-b",
    )
    _claim(session, actor_a, "payment.pix.primary", "alpha@example.invalid")
    _claim(session, actor_b, "payment.pix.primary", "beta@example.invalid")

    result_a = retrieve_personal_context(session, DEFAULT_TENANT_ID, "meu pix")
    result_b = retrieve_personal_context(session, tenant_b, "meu pix")

    assert [item.value_text for item in result_a.items] == ["alpha@example.invalid"]
    assert [item.value_text for item in result_b.items] == ["beta@example.invalid"]


def test_retrieval_never_returns_secret_claims(session):
    actor = _owner(
        session,
        DEFAULT_TENANT_ID,
        actor_key="owner-a",
        external_id="owner-ext",
    )
    _claim(
        session,
        actor,
        "security.password",
        "must-not-leak",
        sensitivity="SECRET",
    )

    result = retrieve_personal_context(session, DEFAULT_TENANT_ID, "qual minha senha password")

    assert result.items == ()


def test_retrieval_returns_empty_for_unrelated_or_stopword_only_query(session):
    actor = _owner(
        session,
        DEFAULT_TENANT_ID,
        actor_key="owner-a",
        external_id="owner-ext",
    )
    _claim(session, actor, "professional.role", "network engineer")

    unrelated = retrieve_personal_context(session, DEFAULT_TENANT_ID, "clima amanhã")
    empty = retrieve_personal_context(session, DEFAULT_TENANT_ID, "qual é o meu")

    assert unrelated.items == ()
    assert empty.items == ()


def test_retrieval_is_bounded_and_deterministic(session):
    actor = _owner(
        session,
        DEFAULT_TENANT_ID,
        actor_key="owner-a",
        external_id="owner-ext",
    )
    _claim(session, actor, "work.branch.primary", "filial 235", confidence=1.0)
    _claim(session, actor, "work.branch.secondary", "filial 236", confidence=0.8)

    result = retrieve_personal_context(
        session,
        DEFAULT_TENANT_ID,
        "filial trabalho",
        limit=1,
    )

    assert len(result.items) == 1
    assert result.items[0].predicate == "work.branch.primary"
    assert result.candidate_count == 2

    with pytest.raises(ValueError, match="CONTEXT_RETRIEVAL_LIMIT_OUT_OF_RANGE"):
        retrieve_personal_context(session, DEFAULT_TENANT_ID, "filial", limit=0)

    with pytest.raises(ValueError, match="CONTEXT_RETRIEVAL_CANDIDATE_LIMIT_OUT_OF_RANGE"):
        retrieve_personal_context(
            session,
            DEFAULT_TENANT_ID,
            "filial",
            limit=8,
            candidate_limit=4,
        )
