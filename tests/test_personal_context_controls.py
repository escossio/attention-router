from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from attention_router.application import services
from attention_router.application.personal_context import build_personal_context
from attention_router.application.personal_context_anomaly_suggestions import (
    build_anomaly_suggestions,
)
from attention_router.application.personal_context_controls import (
    CONTEXT_CONTROL_PREDICATE,
    active_context_control_policy,
    claim_is_owner_private,
    parse_context_control,
)
from attention_router.application.personal_context_sequence_hypotheses import (
    persist_context_event_sequence_hypothesis,
)
from attention_router.application.personal_context_sequences import (
    detect_event_sequence_hypotheses,
)
from attention_router.infrastructure.models import (
    AgentExecutionIntentRow,
    ExecutionIntentRow,
    FactRow,
    MemoryClaimRow,
    ReminderRow,
)
from tests.test_personal_context_bounded_review import _make_interested
from tests.test_personal_context_sequences import (
    ACTOR,
    _sequence_occurrence,
)
from tests.test_personal_context_suggestion_delivery import _reply


def _effect_counts(session) -> tuple[int, int, int, int]:
    return (
        session.scalar(select(func.count()).select_from(ExecutionIntentRow)),
        session.scalar(select(func.count()).select_from(AgentExecutionIntentRow)),
        session.scalar(select(func.count()).select_from(ReminderRow)),
        session.scalar(select(func.count()).select_from(FactRow)),
    )


def _make_reviewed(session, monkeypatch, stamp: datetime):
    interested, interest_at = _make_interested(session, monkeypatch, stamp)
    result = services.receive_normalized_inbound_event(
        session,
        _reply(
            "v1r-review",
            "mostrar revisão",
            interest_at + timedelta(minutes=1),
        ),
    )
    reviewed = session.scalar(
        select(MemoryClaimRow).where(
            MemoryClaimRow.predicate == "context.suggestion.proactive",
            MemoryClaimRow.status == "ACTIVE",
        )
    )
    session.refresh(interested)
    assert interested.status == "SUPERSEDED"
    assert reviewed is not None
    assert reviewed.object_json["lifecycle_state"] == "REVIEWED"
    return reviewed, interest_at + timedelta(minutes=1), result


def _source_sequence(session, reviewed: MemoryClaimRow) -> MemoryClaimRow:
    row = session.get(
        MemoryClaimRow,
        reviewed.context["source_sequence_claim_id"],
    )
    assert row is not None
    assert row.predicate == "context.pattern.event_sequence"
    return row


def _active_control(session) -> MemoryClaimRow:
    row = session.scalar(
        select(MemoryClaimRow).where(
            MemoryClaimRow.predicate == CONTEXT_CONTROL_PREDICATE,
            MemoryClaimRow.status == "ACTIVE",
        )
    )
    assert row is not None
    return row


def test_context_control_parser_is_closed_and_non_authoritative():
    assert parse_context_control(
        "marcar este contexto como privado"
    ).kind.value == "SET_PRIVATE"
    assert parse_context_control(
        "não usar este contexto para ações"
    ).kind.value == "SET_NON_ACTIONABLE"
    assert parse_context_control(
        "remover marcação privada deste contexto"
    ).kind.value == "CLEAR_PRIVATE"
    assert parse_context_control(
        "permitir sugestões com este contexto"
    ).kind.value == "CLEAR_NON_ACTIONABLE"
    assert parse_context_control("sim") is None
    assert parse_context_control("mostrar revisão") is None


def test_owner_private_control_hides_context_without_deleting_history(
    session,
    monkeypatch,
):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    reviewed, reviewed_at, _ = _make_reviewed(session, monkeypatch, stamp)
    sequence = _source_sequence(session, reviewed)
    sequence_id = sequence.id
    hypothesis_id = sequence.context["hypothesis_id"]
    before_effects = _effect_counts(session)

    before_snapshot = build_personal_context(
        session,
        reviewed.subject_actor_id and reviewed.context.get("tenant_id", None)
        or "default",
    ) if False else None

    services.receive_normalized_inbound_event(
        session,
        _reply(
            "v1r-private",
            "marcar este contexto como privado",
            reviewed_at + timedelta(minutes=1),
        ),
    )

    control = _active_control(session)
    assert control.source_quality == "USER_DECLARED"
    assert control.object_json["privacy"] == "PRIVATE"
    assert control.object_json["actionability"] == "DEFAULT"
    assert control.object_json["grants_authority"] is False
    assert control.object_json["grants_disclosure_authority"] is False
    assert control.context["target_hypothesis_id"] == hypothesis_id

    policy = active_context_control_policy(
        session,
        actor_id=sequence.subject_actor_id,
        hypothesis_id=hypothesis_id,
    )
    assert policy.private is True
    assert policy.non_actionable is False
    assert claim_is_owner_private(session, claim=sequence) is True

    snapshot = build_personal_context(session, sequence.tenant_id if hasattr(sequence, "tenant_id") else "default") if False else None
    # Build through the tenant owner boundary using the known default tenant.
    from attention_router.core.tenancy import DEFAULT_TENANT_ID

    snapshot = build_personal_context(session, DEFAULT_TENANT_ID)
    predicates = {item.predicate for item in snapshot.claims}
    assert "context.pattern.event_sequence" not in predicates
    assert "context.pattern.sequence_anomaly" not in predicates
    assert "context.suggestion.proactive" not in predicates
    assert CONTEXT_CONTROL_PREDICATE not in predicates

    session.refresh(sequence)
    assert sequence.id == sequence_id
    assert sequence.status == "ACTIVE"
    assert _effect_counts(session) == before_effects

    assert build_anomaly_suggestions(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=reviewed_at + timedelta(minutes=1),
    ) == ()


def test_private_control_survives_new_snapshot_of_same_hypothesis(
    session,
    monkeypatch,
):
    from attention_router.core.tenancy import DEFAULT_TENANT_ID

    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    reviewed, reviewed_at, _ = _make_reviewed(session, monkeypatch, stamp)
    original = _source_sequence(session, reviewed)
    hypothesis_id = original.context["hypothesis_id"]

    services.receive_normalized_inbound_event(
        session,
        _reply(
            "v1r-private-reinfer",
            "marcar este contexto como privado",
            reviewed_at + timedelta(minutes=1),
        ),
    )

    _sequence_occurrence(
        session,
        start=stamp + timedelta(days=2),
        gap_minutes=31,
        first_provenance="device-location",
        second_provenance="calendar",
    )
    evaluation_time = stamp + timedelta(days=2, hours=1)
    detected = detect_event_sequence_hypotheses(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=ACTOR,
        now=evaluation_time,
    )
    matching = [
        item for item in detected
        if item.hypothesis_id == hypothesis_id
    ]
    assert len(matching) == 1
    new_claim, changed = persist_context_event_sequence_hypothesis(
        session,
        hypothesis=matching[0],
        now=evaluation_time,
    )

    assert changed is True
    assert new_claim.id != original.id
    assert new_claim.context["hypothesis_id"] == hypothesis_id
    assert claim_is_owner_private(session, claim=new_claim) is True

    snapshot = build_personal_context(
        session,
        DEFAULT_TENANT_ID,
        now=evaluation_time,
    )
    assert all(
        item.claim_id != new_claim.id
        for item in snapshot.claims
    )


def test_clear_private_restores_read_eligibility_without_disclosure_authority(
    session,
    monkeypatch,
):
    from attention_router.core.tenancy import DEFAULT_TENANT_ID

    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    reviewed, reviewed_at, _ = _make_reviewed(session, monkeypatch, stamp)
    sequence = _source_sequence(session, reviewed)

    services.receive_normalized_inbound_event(
        session,
        _reply(
            "v1r-private-set",
            "marcar este contexto como privado",
            reviewed_at + timedelta(minutes=1),
        ),
    )
    first_control = _active_control(session)

    services.receive_normalized_inbound_event(
        session,
        _reply(
            "v1r-private-clear",
            "remover marcação privada deste contexto",
            reviewed_at + timedelta(minutes=2),
        ),
    )

    session.refresh(first_control)
    current = _active_control(session)
    assert first_control.status == "SUPERSEDED"
    assert current.supersedes_claim_id == first_control.id
    assert current.object_json["privacy"] == "DEFAULT"
    assert current.object_json["grants_disclosure_authority"] is False
    assert claim_is_owner_private(session, claim=sequence) is False

    snapshot = build_personal_context(session, DEFAULT_TENANT_ID)
    assert any(
        item.claim_id == sequence.id
        for item in snapshot.claims
    )


def test_non_actionable_control_blocks_new_suggestions_but_keeps_knowledge_visible(
    session,
    monkeypatch,
):
    from attention_router.core.tenancy import DEFAULT_TENANT_ID

    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    reviewed, reviewed_at, _ = _make_reviewed(session, monkeypatch, stamp)
    sequence = _source_sequence(session, reviewed)
    before_effects = _effect_counts(session)

    services.receive_normalized_inbound_event(
        session,
        _reply(
            "v1r-non-actionable",
            "não usar este contexto para ações",
            reviewed_at + timedelta(minutes=1),
        ),
    )

    policy = active_context_control_policy(
        session,
        actor_id=sequence.subject_actor_id,
        hypothesis_id=sequence.context["hypothesis_id"],
    )
    assert policy.private is False
    assert policy.non_actionable is True

    snapshot = build_personal_context(session, DEFAULT_TENANT_ID)
    assert any(
        item.claim_id == sequence.id
        for item in snapshot.claims
    )
    assert build_anomaly_suggestions(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=reviewed_at + timedelta(minutes=1),
    ) == ()
    assert _effect_counts(session) == before_effects

    services.receive_normalized_inbound_event(
        session,
        _reply(
            "v1r-actionable-clear",
            "permitir sugestões com este contexto",
            reviewed_at + timedelta(minutes=2),
        ),
    )
    restored = active_context_control_policy(
        session,
        actor_id=sequence.subject_actor_id,
        hypothesis_id=sequence.context["hypothesis_id"],
    )
    assert restored.non_actionable is False
    assert build_anomaly_suggestions(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=reviewed_at + timedelta(minutes=2),
    ) != ()
