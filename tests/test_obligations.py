from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from attention_router.application.cognitive_graph import build_cognitive_graph_slice
from attention_router.application.context_compiler import compile_graph_context
from attention_router.application.obligations import (
    ObligationError,
    advance_due_obligation_states,
    create_recurring_obligation_definition,
    extend_obligation_instance,
    generate_monthly_obligation_instance,
    reconcile_obligation_event,
    waive_obligation_instance,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import now_utc
from attention_router.infrastructure.candidate_insight_models import (
    CandidateInsightRow,
)
from attention_router.infrastructure.models import (
    ExecutionIntentRow,
    OutboxMessageRow,
    ResourceRow,
    TenantRow,
    TimelineEventRow,
)
from attention_router.infrastructure.obligation_models import (
    ObligationFulfillmentRow,
    ObligationTransitionRow,
    RecurringObligationDefinitionRow,
)
from attention_router.infrastructure.repository import upsert_actor_binding


OWNER = "actor-owner-v2g"
ANGELO = "actor-angelo-v2g"


def _tenant(session, tenant_id: str) -> None:
    if session.get(TenantRow, tenant_id) is not None:
        return
    stamp = now_utc()
    session.add(
        TenantRow(
            id=tenant_id,
            slug=f"v2g-{tenant_id[-8:]}",
            name=f"V2G {tenant_id[-8:]}",
            status="ACTIVE",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    session.flush()


def _world(session, *, tenant_id: str = DEFAULT_TENANT_ID, suffix: str = "a"):
    _tenant(session, tenant_id)
    owner_key = OWNER if tenant_id == DEFAULT_TENANT_ID else f"{OWNER}-{tenant_id[-6:]}"
    angelo_key = ANGELO if tenant_id == DEFAULT_TENANT_ID else f"{ANGELO}-{tenant_id[-6:]}"

    upsert_actor_binding(
        session,
        source="test",
        external_actor_id=f"{tenant_id}:owner",
        actor_key=owner_key,
        actor_category="owner",
        display_name="Leonardo",
        metadata={"owner": True},
        tenant_id=tenant_id,
    )
    upsert_actor_binding(
        session,
        source="test",
        external_actor_id=f"{tenant_id}:angelo",
        actor_key=angelo_key,
        actor_category="contact",
        display_name="Ângelo",
        metadata={},
        tenant_id=tenant_id,
    )
    stamp = now_utc()
    resource = ResourceRow(
        id=f"{tenant_id}:casa-07-{suffix}",
        tenant_id=tenant_id,
        resource_type="PROPERTY",
        canonical_name=f"Casa 07 {suffix}",
        status="ACTIVE",
        metadata_json={},
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(resource)
    session.flush()
    return owner_key, angelo_key, resource


def _definition(
    session,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
    suffix: str = "a",
    obligation_kind: str = "RENT",
    due_day: int = 10,
    grace_seconds: int = 0,
):
    owner_key, angelo_key, resource = _world(
        session,
        tenant_id=tenant_id,
        suffix=suffix,
    )
    definition, created = create_recurring_obligation_definition(
        session,
        tenant_id=tenant_id,
        subject_type="RESOURCE",
        subject_id=resource.id,
        obligation_kind=obligation_kind,
        expected_actor_key=angelo_key,
        expected_event_type="RENT_PAYMENT",
        due_day=due_day,
        due_timezone="America/Fortaleza",
        grace_seconds=grace_seconds,
        value_constraints={"amount": 1350, "currency": "BRL"},
        confidence=0.95,
        sensitivity_class="PRIVATE",
        source_kind="OWNER_DECLARED",
        decision_actor_key=owner_key,
        decision_ref=f"owner-command:{suffix}",
        provenance={"fixture": suffix},
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        now=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
    )
    assert created is True
    return owner_key, angelo_key, resource, definition


def _instance(
    session,
    *,
    suffix: str = "a",
    obligation_kind: str = "RENT",
    due_day: int = 10,
    grace_seconds: int = 0,
):
    owner_key, angelo_key, resource, definition = _definition(
        session,
        suffix=suffix,
        obligation_kind=obligation_kind,
        due_day=due_day,
        grace_seconds=grace_seconds,
    )
    instance, created = generate_monthly_obligation_instance(
        session,
        definition_id=definition.id,
        year=2026,
        month=10,
        now=datetime(2026, 10, 1, 12, 1, tzinfo=UTC),
    )
    assert created is True
    return owner_key, angelo_key, resource, definition, instance


def _payment(
    session,
    *,
    actor_key: str,
    resource_id: str,
    suffix: str,
    occurred_at: datetime,
    tenant_id: str = DEFAULT_TENANT_ID,
):
    row = TimelineEventRow(
        id=f"{tenant_id}:rent-payment-{suffix}",
        tenant_id=tenant_id,
        canonical_event_id=None,
        actor_id=actor_key,
        relationship_id=None,
        resource_id=resource_id,
        event_type="RENT_PAYMENT",
        event_ref={"source": "test", "suffix": suffix},
        occurred_at=occurred_at,
        visibility="PRIVATE",
        provenance="TEST",
        metadata_json={},
    )
    session.add(row)
    session.flush()
    return row


def test_v2g_definition_and_monthly_instance_are_idempotent(session):
    owner, angelo, resource, definition = _definition(session, suffix="idempotent")

    replay, created = create_recurring_obligation_definition(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        subject_type="RESOURCE",
        subject_id=resource.id,
        obligation_kind="RENT",
        expected_actor_key=angelo,
        expected_event_type="RENT_PAYMENT",
        due_day=10,
        due_timezone="America/Fortaleza",
        value_constraints={"amount": 1350, "currency": "BRL"},
        confidence=0.95,
        sensitivity_class="PRIVATE",
        source_kind="OWNER_DECLARED",
        decision_actor_key=owner,
        decision_ref="owner-command:idempotent",
        provenance={"fixture": "idempotent"},
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        now=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
    )
    assert created is False
    assert replay.id == definition.id

    first, first_created = generate_monthly_obligation_instance(
        session,
        definition_id=definition.id,
        year=2026,
        month=10,
        now=datetime(2026, 10, 1, 12, 1, tzinfo=UTC),
    )
    second, second_created = generate_monthly_obligation_instance(
        session,
        definition_id=definition.id,
        year=2026,
        month=10,
        now=datetime(2026, 10, 1, 12, 2, tzinfo=UTC),
    )

    assert first_created is True
    assert second_created is False
    assert second.id == first.id
    assert first.period_key == "2026-10"
    assert first.state == "EXPECTED"
    assert first.reconciliation_status == "PENDING"
    assert first.uncertainty_code == "AWAITING_CONFIRMING_EVIDENCE"
    assert first.expected_by == datetime(
        2026, 10, 11, 2, 59, 59, 999999, tzinfo=UTC
    )
    assert first.provenance["absence_is_fact"] is False


def test_v2g_full_fulfillment_satisfies_without_side_effects_and_projects_graph(session):
    _owner, angelo, resource, definition, instance = _instance(
        session,
        suffix="full",
    )
    event = _payment(
        session,
        actor_key=angelo,
        resource_id=resource.id,
        suffix="full",
        occurred_at=datetime(2026, 10, 8, 15, 0, tzinfo=UTC),
    )
    before = {
        "execution": session.scalar(
            select(func.count()).select_from(ExecutionIntentRow)
        ),
        "outbox": session.scalar(
            select(func.count()).select_from(OutboxMessageRow)
        ),
    }

    fulfillment, changed = reconcile_obligation_event(
        session,
        instance_id=instance.id,
        timeline_event_id=event.id,
        fulfillment_fraction=1.0,
        observed_value={"amount": 1350, "currency": "BRL"},
        now=datetime(2026, 10, 8, 15, 1, tzinfo=UTC),
    )
    replay, replay_changed = reconcile_obligation_event(
        session,
        instance_id=instance.id,
        timeline_event_id=event.id,
        fulfillment_fraction=1.0,
        observed_value={"amount": 1350, "currency": "BRL"},
        now=datetime(2026, 10, 8, 15, 2, tzinfo=UTC),
    )
    session.refresh(instance)

    assert changed is True
    assert replay_changed is False
    assert replay.id == fulfillment.id
    assert instance.state == "SATISFIED"
    assert instance.reconciliation_status == "CONFIRMED"
    assert instance.satisfaction_ratio == 1.0
    assert instance.uncertainty_code is None
    assert before == {
        "execution": session.scalar(
            select(func.count()).select_from(ExecutionIntentRow)
        ),
        "outbox": session.scalar(
            select(func.count()).select_from(OutboxMessageRow)
        ),
    }

    graph = build_cognitive_graph_slice(session, DEFAULT_TENANT_ID)
    nodes = graph.node_index()
    assert f"obligation:{definition.id}" in nodes
    assert f"expectation:{instance.id}" in nodes
    expectation = nodes[f"expectation:{instance.id}"]
    assert expectation.kind.value == "EXPECTATION"
    assert expectation.attributes["state"] == "SATISFIED"
    assert expectation.provenance["absence_is_fact"] is False

    packet = compile_graph_context(
        session,
        DEFAULT_TENANT_ID,
        "Ângelo",
        max_hops=2,
        item_budget=30,
        token_budget=7000,
    )
    assert any(item.source_id == definition.id for item in packet.items)
    assert any(item.source_id == instance.id for item in packet.items)


def test_v2g_partial_then_complete_fulfillment(session):
    _owner, angelo, resource, _definition_row, instance = _instance(
        session,
        suffix="partial",
    )
    first = _payment(
        session,
        actor_key=angelo,
        resource_id=resource.id,
        suffix="partial-1",
        occurred_at=datetime(2026, 10, 5, 15, 0, tzinfo=UTC),
    )
    second = _payment(
        session,
        actor_key=angelo,
        resource_id=resource.id,
        suffix="partial-2",
        occurred_at=datetime(2026, 10, 8, 15, 0, tzinfo=UTC),
    )

    reconcile_obligation_event(
        session,
        instance_id=instance.id,
        timeline_event_id=first.id,
        fulfillment_fraction=0.4,
        observed_value={"amount": 540},
    )
    session.refresh(instance)
    assert instance.state == "PARTIALLY_SATISFIED"
    assert instance.reconciliation_status == "PARTIAL"
    assert instance.satisfaction_ratio == pytest.approx(0.4)
    assert instance.uncertainty_code == "PARTIAL_CONFIRMING_EVIDENCE"

    reconcile_obligation_event(
        session,
        instance_id=instance.id,
        timeline_event_id=second.id,
        fulfillment_fraction=0.6,
        observed_value={"amount": 810},
    )
    session.refresh(instance)
    assert instance.state == "SATISFIED"
    assert instance.satisfaction_ratio == pytest.approx(1.0)
    assert session.scalar(
        select(func.count())
        .select_from(ObligationFulfillmentRow)
        .where(ObligationFulfillmentRow.instance_id == instance.id)
    ) == 2


def test_v2g_unconfirmed_after_due_is_not_nonpayment_and_late_evidence_revises_it(session):
    _owner, angelo, resource, _definition_row, instance = _instance(
        session,
        suffix="late",
    )

    changed = advance_due_obligation_states(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        now=datetime(2026, 10, 12, 12, 0, tzinfo=UTC),
    )
    session.refresh(instance)

    assert changed == 1
    assert instance.state == "UNCONFIRMED_AFTER_DUE"
    assert instance.reconciliation_status == "UNCONFIRMED"
    assert instance.uncertainty_code == "NO_CONFIRMING_EVIDENCE_AFTER_DUE"

    overdue_transition = session.scalar(
        select(ObligationTransitionRow)
        .where(
            ObligationTransitionRow.instance_id == instance.id,
            ObligationTransitionRow.reason_code
            == "DUE_WINDOW_CLOSED_WITHOUT_CONFIRMING_EVIDENCE",
        )
    )
    assert overdue_transition is not None
    assert overdue_transition.metadata_json["absence_is_fact"] is False
    assert overdue_transition.metadata_json["asserts_non_payment"] is False

    late_ingested = _payment(
        session,
        actor_key=angelo,
        resource_id=resource.id,
        suffix="late-evidence",
        occurred_at=datetime(2026, 10, 10, 15, 0, tzinfo=UTC),
    )
    fulfillment, created = reconcile_obligation_event(
        session,
        instance_id=instance.id,
        timeline_event_id=late_ingested.id,
        now=datetime(2026, 10, 12, 12, 5, tzinfo=UTC),
    )
    session.refresh(instance)

    assert created is True
    assert fulfillment.reconciliation_kind == "LATE_INGESTED"
    assert instance.state == "SATISFIED"
    assert instance.reconciliation_status == "LATE_CONFIRMED"
    assert instance.uncertainty_code is None
    reasons = set(
        session.scalars(
            select(ObligationTransitionRow.reason_code).where(
                ObligationTransitionRow.instance_id == instance.id
            )
        ).all()
    )
    assert "DUE_WINDOW_CLOSED_WITHOUT_CONFIRMING_EVIDENCE" in reasons
    assert "LATE_EVIDENCE_CONFIRMED" in reasons


def test_v2g_partial_after_due_remains_partial_not_nonpayment(session):
    _owner, angelo, resource, _definition_row, instance = _instance(
        session,
        suffix="partial-overdue",
    )
    event = _payment(
        session,
        actor_key=angelo,
        resource_id=resource.id,
        suffix="partial-overdue",
        occurred_at=datetime(2026, 10, 8, 15, 0, tzinfo=UTC),
    )
    reconcile_obligation_event(
        session,
        instance_id=instance.id,
        timeline_event_id=event.id,
        fulfillment_fraction=0.4,
        observed_value={"amount": 540},
    )

    changed = advance_due_obligation_states(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        now=datetime(2026, 10, 12, 12, 0, tzinfo=UTC),
    )
    session.refresh(instance)

    assert changed == 1
    assert instance.state == "PARTIALLY_SATISFIED"
    assert instance.reconciliation_status == "PARTIAL"
    assert instance.uncertainty_code == "PARTIAL_EVIDENCE_AFTER_DUE"
    transition = session.scalar(
        select(ObligationTransitionRow).where(
            ObligationTransitionRow.instance_id == instance.id,
            ObligationTransitionRow.reason_code
            == "PARTIAL_EVIDENCE_REMAINS_AFTER_DUE",
        )
    )
    assert transition is not None
    assert transition.metadata_json["absence_is_fact"] is False


def test_v2g_admitted_candidate_can_source_definition_without_becoming_authority(session):
    _owner, angelo, resource = _world(session, suffix="candidate")
    stamp = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    candidate = CandidateInsightRow(
        id="candidate-v2g-admitted",
        tenant_id=DEFAULT_TENANT_ID,
        semantic_key="candidate-obligation-v2g",
        idempotency_key="candidate-obligation-v2g-idempotency",
        insight_type="CLAIM_PROPOSAL",
        subject_type="RESOURCE",
        subject_id=resource.id,
        predicate="context.recurring_obligation",
        proposed_value={"kind": "RENT"},
        source_engine="RULE",
        confidence=0.82,
        sensitivity_class="PRIVATE",
        valid_from=None,
        valid_until=None,
        contradiction_refs=[],
        state="ADMITTED",
        supersedes_insight_id=None,
        decision_kind="OWNER_ADMITTED",
        decision_actor_key=OWNER,
        decision_ref="owner-command:admit-v2g",
        provenance={
            "grants_authority": False,
            "materializes_canonical_truth": False,
        },
        created_at=stamp,
        updated_at=stamp,
        decided_at=stamp,
    )
    session.add(candidate)
    session.flush()

    definition, created = create_recurring_obligation_definition(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        subject_type="RESOURCE",
        subject_id=resource.id,
        obligation_kind="RENT",
        expected_actor_key=angelo,
        expected_event_type="RENT_PAYMENT",
        due_day=10,
        due_timezone="America/Fortaleza",
        confidence=0.95,
        sensitivity_class="NORMAL",
        source_kind="ADMITTED_CANDIDATE",
        source_ref=candidate.id,
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        now=stamp,
    )

    assert created is True
    assert definition.source_kind == "ADMITTED_CANDIDATE"
    assert definition.source_ref == candidate.id
    assert definition.confidence == pytest.approx(0.82)
    assert definition.sensitivity_class == "PRIVATE"
    assert definition.provenance["candidate_insight_id"] == candidate.id
    assert definition.provenance["grants_authority"] is False


def test_v2g_owner_extension_delays_unconfirmed_transition(session):
    owner, _angelo, _resource, _definition_row, instance = _instance(
        session,
        suffix="extension",
    )
    extension = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)

    extended = extend_obligation_instance(
        session,
        instance_id=instance.id,
        extension_until=extension,
        decision_actor_key=owner,
        decision_ref="owner-command:extend-v2g",
        now=datetime(2026, 10, 11, 12, 0, tzinfo=UTC),
    )
    assert extended.state == "EXTENDED"
    assert extended.reconciliation_status == "EXTENDED"

    assert advance_due_obligation_states(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        now=datetime(2026, 10, 14, 12, 0, tzinfo=UTC),
    ) == 0
    session.refresh(instance)
    assert instance.state == "EXTENDED"

    assert advance_due_obligation_states(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        now=datetime(2026, 10, 16, 12, 0, tzinfo=UTC),
    ) == 1
    session.refresh(instance)
    assert instance.state == "UNCONFIRMED_AFTER_DUE"


def test_v2g_owner_waiver_is_terminal_and_audited(session):
    owner, _angelo, _resource, _definition_row, instance = _instance(
        session,
        suffix="waive",
    )

    waived = waive_obligation_instance(
        session,
        instance_id=instance.id,
        decision_actor_key=owner,
        decision_ref="owner-command:waive-v2g",
        now=datetime(2026, 10, 7, 12, 0, tzinfo=UTC),
    )

    assert waived.state == "WAIVED"
    assert waived.reconciliation_status == "WAIVED"
    assert waived.waived_at is not None
    transition = session.scalar(
        select(ObligationTransitionRow).where(
            ObligationTransitionRow.instance_id == instance.id,
            ObligationTransitionRow.reason_code == "OWNER_WAIVED_OBLIGATION",
        )
    )
    assert transition is not None
    assert transition.decision_actor_key == owner

    assert advance_due_obligation_states(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        now=datetime(2026, 10, 20, 12, 0, tzinfo=UTC),
    ) == 0


def test_v2g_one_event_requires_explicit_governance_to_satisfy_multiple_obligations(session):
    owner, angelo, resource, first_definition, first_instance = _instance(
        session,
        suffix="shared",
        obligation_kind="RENT",
    )
    second_definition, created = create_recurring_obligation_definition(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        subject_type="RESOURCE",
        subject_id=resource.id,
        obligation_kind="RENT_FEE",
        expected_actor_key=angelo,
        expected_event_type="RENT_PAYMENT",
        due_day=10,
        due_timezone="America/Fortaleza",
        confidence=0.9,
        sensitivity_class="PRIVATE",
        source_kind="OWNER_DECLARED",
        decision_actor_key=owner,
        decision_ref="owner-command:shared-fee",
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        now=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
    )
    assert created is True
    second_instance, _ = generate_monthly_obligation_instance(
        session,
        definition_id=second_definition.id,
        year=2026,
        month=10,
    )
    event = _payment(
        session,
        actor_key=angelo,
        resource_id=resource.id,
        suffix="shared",
        occurred_at=datetime(2026, 10, 8, 15, 0, tzinfo=UTC),
    )

    reconcile_obligation_event(
        session,
        instance_id=first_instance.id,
        timeline_event_id=event.id,
    )
    with pytest.raises(
        ObligationError,
        match="OBLIGATION_EVENT_ALREADY_RECONCILED",
    ):
        reconcile_obligation_event(
            session,
            instance_id=second_instance.id,
            timeline_event_id=event.id,
        )

    second_link, created = reconcile_obligation_event(
        session,
        instance_id=second_instance.id,
        timeline_event_id=event.id,
        allow_shared_event=True,
    )
    assert created is True
    assert second_link.explicitly_shared is True
    assert second_link.reconciliation_kind == "EXPLICIT_SHARED"


def test_v2g_definition_correction_supersedes_without_deleting_history(session):
    owner, angelo, resource, original = _definition(
        session,
        suffix="correction",
        due_day=10,
    )

    replacement, created = create_recurring_obligation_definition(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        subject_type="RESOURCE",
        subject_id=resource.id,
        obligation_kind="RENT",
        expected_actor_key=angelo,
        expected_event_type="RENT_PAYMENT",
        due_day=12,
        due_timezone="America/Fortaleza",
        value_constraints={"amount": 1350, "currency": "BRL"},
        confidence=0.95,
        sensitivity_class="PRIVATE",
        source_kind="OWNER_DECLARED",
        decision_actor_key=owner,
        decision_ref="owner-command:correct-due-day",
        supersedes_definition_id=original.id,
        valid_from=datetime(2026, 10, 3, tzinfo=UTC),
        now=datetime(2026, 10, 3, 12, 0, tzinfo=UTC),
    )
    session.refresh(original)

    assert created is True
    assert original.state == "SUPERSEDED"
    assert replacement.state == "ACTIVE"
    assert replacement.supersedes_definition_id == original.id
    assert replacement.due_day == 12
    assert session.scalar(
        select(func.count()).select_from(RecurringObligationDefinitionRow)
    ) == 2


def test_v2g_cross_tenant_and_secret_events_fail_closed(session):
    _owner, angelo, resource, _definition_row, instance = _instance(
        session,
        suffix="isolation",
    )
    tenant_b = "00000000-0000-4000-8000-00000000a226"
    _tenant(session, tenant_b)
    foreign = TimelineEventRow(
        id=f"{tenant_b}:foreign-payment",
        tenant_id=tenant_b,
        canonical_event_id=None,
        actor_id=angelo,
        relationship_id=None,
        resource_id=resource.id,
        event_type="RENT_PAYMENT",
        event_ref={"source": "foreign"},
        occurred_at=datetime(2026, 10, 8, 15, 0, tzinfo=UTC),
        visibility="PRIVATE",
        provenance="TEST",
        metadata_json={},
    )
    secret = TimelineEventRow(
        id=f"{DEFAULT_TENANT_ID}:secret-payment",
        tenant_id=DEFAULT_TENANT_ID,
        canonical_event_id=None,
        actor_id=angelo,
        relationship_id=None,
        resource_id=resource.id,
        event_type="RENT_PAYMENT",
        event_ref={"source": "secret"},
        occurred_at=datetime(2026, 10, 8, 15, 0, tzinfo=UTC),
        visibility="SECRET",
        provenance="TEST",
        metadata_json={},
    )
    session.add_all([foreign, secret])
    session.flush()

    with pytest.raises(ObligationError, match="OBLIGATION_TENANT_MISMATCH"):
        reconcile_obligation_event(
            session,
            instance_id=instance.id,
            timeline_event_id=foreign.id,
        )
    with pytest.raises(
        ObligationError,
        match="OBLIGATION_SECRET_EVENT_NOT_RECONCILABLE",
    ):
        reconcile_obligation_event(
            session,
            instance_id=instance.id,
            timeline_event_id=secret.id,
        )
