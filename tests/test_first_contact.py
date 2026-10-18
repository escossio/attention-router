"""Operational presentation state with synthetic receipts and signed fake proofs."""

from dataclasses import replace
from datetime import timedelta
import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from attention_router.application import first_contact as fc
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure import human_identity_models  # noqa: F401
from attention_router.infrastructure.first_contact_models import (
    FirstContactAttemptRow, FirstContactPresentationRow,
)
from attention_router.infrastructure.models import (
    ActorBindingRow, AgentDecisionRow, AgentExecutionIntentRow, AgentResponseReviewRow, AutonomyEvaluationRow,
    InboundEventRow, InteractionRow, OutboxMessageRow, TenantRow,
)
from attention_router.infrastructure.repository import upsert_actor_binding


PEER = "5500000000211@lid"
INTRO = "Sou a Andy, uma assistente virtual. Posso ajudar com perguntas gerais."
SECRET = "synthetic-first-contact-proof-secret-32-bytes"


def event(session, *, tenant=DEFAULT_TENANT_ID, account="synthetic-account", peer=PEER,
          offset=0, origin="EXTERNAL_INBOUND", group=False, canonical_peer=None, aliases=None):
    canonical_peer = canonical_peer or peer
    aliases = aliases or [peer]
    stamp = now_utc() + timedelta(seconds=offset)
    interaction = InteractionRow(
        id=new_id(), tenant_id=tenant, event_type="message", contact_id=peer,
        contact_name="Synthetic visitor", relationship_category="unknown", active_context=None,
        inbound_text="Synthetic question", state="RECEIVED", created_at=stamp, updated_at=stamp,
        correlation_id=new_id(),
    )
    session.add(interaction)
    session.flush()
    payload = {
        "event_type": "message", "channel": "whatsapp", "actor_id": peer,
        "external_actor_id": peer, "event_origin": origin, "owner_authenticated": False,
        "metadata": {"source_account": account, "from_me": False, "is_group": group,
                     "conversation_state": "READY", "conversation_key": "wwebjs:" + canonical_peer,
                     "peer_identifiers": aliases,
                     "first_contact_identity": {"contract_version": "provider-lid-v1",
                                                "state": "VERIFIED", "canonical_peer_id": canonical_peer,
                                                "provenance": "DIRECTIONAL_LID" if peer == canonical_peer
                                                else "PROVIDER_LID_PN_MAPPING"}},
    }
    receipt = InboundEventRow(
        id=new_id(), tenant_id=tenant, source="wwebjs", external_event_id=new_id(), event_type="message",
        payload=payload, payload_hash=fc.text_hash(json.dumps(payload, sort_keys=True)), received_at=stamp,
        processed_at=stamp, interaction_id=interaction.id, status="PROCESSED",
        correlation_id=interaction.correlation_id, lineage_classification="ORGANIC",
    )
    session.add(receipt)
    session.flush()
    return receipt


def scope(receipt):
    return fc.FirstContactScope(
        receipt.tenant_id, "wwebjs", "whatsapp", receipt.payload["metadata"]["source_account"],
        fc.text_hash(PEER),
    )


def decision(session, receipt, text=INTRO):
    row = AgentDecisionRow(
        id=new_id(), event_id=receipt.id, interaction_id=receipt.interaction_id,
        actor_id=receipt.payload["actor_id"], actor_binding_id=None, audience="unknown",
        decision_pipeline_version="synthetic-first-contact", decision_type="RESPOND",
        recommended_action="respond", proposed_response=text, escalation_required=False,
        missing_information=[], requested_capabilities=[], execution_allowed=False,
        external_delivery_allowed=False, reasoning_summary="synthetic", status="DRY_RUN", created_at=now_utc(),
    )
    session.add(row)
    session.flush()
    return row


def effect(session, row, *, authorization_source="POLICY_AUTONOMY"):
    evaluation = AutonomyEvaluationRow(
        id=new_id(), agent_decision_id=row.id, event_id=row.event_id, interaction_id=row.interaction_id,
        blueprint_mode="AUTO_ALLOWED", policy_mode="AUTO_ALLOWED", effective_mode="AUTO_ALLOWED",
        decision_type="RESPOND", recommended_action="respond", action_allowed=True,
        actor_scope_valid=True, audience_scope_valid=True, freshness_valid=True, from_me=False,
        automatic_execution_allowed=True, reason_code="AUTO_ALLOWED", created_at=now_utc(),
    )
    session.add(evaluation)
    session.flush()
    review = None
    if authorization_source == "HUMAN_REVIEW":
        review = AgentResponseReviewRow(
            id=new_id(), agent_decision_id=row.id, status="APPROVED",
            proposed_response_snapshot=row.proposed_response, effective_response=row.proposed_response,
            reviewer_type="operator", version=1, created_at=now_utc(), updated_at=now_utc(),
        )
        session.add(review)
        session.flush()
    intent = AgentExecutionIntentRow(
        id=new_id(), agent_decision_id=row.id, autonomy_evaluation_id=evaluation.id if review is None else None,
        response_review_id=review.id if review else None,
        authorization_source=authorization_source, intent_type="WHATSAPP_RESPONSE",
        effective_response_snapshot=row.proposed_response, status="READY", execution_allowed=True,
        external_delivery_allowed=True, release_status="RELEASED", idempotency_key="fc-test:" + new_id(),
        created_at=now_utc(),
    )
    session.add(intent)
    session.flush()
    outbox = OutboxMessageRow(
        id=new_id(), interaction_id=row.interaction_id, execution_intent_id=intent.id,
        action_type="agent_execution_text", destination="local_transport",
        payload={"message_type": "text", "text": row.proposed_response,
                 "external_actor_id": row.actor_id, "execution_intent_id": intent.id},
        status="PENDING", created_at=now_utc(), available_at=now_utc(), attempt_count=0,
        idempotency_key="execution:" + intent.id,
    )
    session.add(outbox)
    session.flush()
    return intent, outbox


def queued(session, *, authorization_source="POLICY_AUTONOMY"):
    receipt = event(session)
    result = fc.reserve(session, scope(receipt), receipt.id)
    session.commit()
    row = decision(session, receipt)
    assert fc.record_generated(session, result.token, row.id, fc.text_hash(INTRO))
    session.commit()
    intent, outbox = effect(session, row, authorization_source=authorization_source)
    assert fc.bind_outbox(session, row.id, intent.id, outbox.id)
    session.commit()
    return SimpleNamespace(event=receipt, scope=scope(receipt), token=result.token,
                           decision=row, intent=intent, outbox=outbox)


def signed_proof(session, case, *, ack=2, reference="synthetic-provider-message-211", **changes):
    from attention_router.adapters.first_contact_delivery import verify_delivery_response

    expected = fc.delivery_scope(session, case.outbox.id)
    changes = dict(changes)
    if "execution_intent_id" in changes:
        changes.setdefault("idempotency_key", "execution:" + changes["execution_intent_id"])
    elif "idempotency_key" in changes:
        changes.setdefault("execution_intent_id", changes["idempotency_key"].removeprefix("execution:"))
    expected.update({key: value for key, value in changes.items() if key in expected})
    issued = int(now_utc().timestamp())
    nonce = "a" * 64
    payload = {
        **expected, "status": "SENT", "message_reference": reference,
        "ack": ack, "ack_at": now_utc().isoformat() if ack is not None else None,
        "nonce": nonce, "issued_at": issued,
    }
    payload.update(changes)
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(
        SECRET.encode(), f"first-contact-delivery:v1\nresponse\n{issued}\n{nonce}\n".encode() + raw,
        hashlib.sha256,
    ).hexdigest()
    return verify_delivery_response(
        raw, {"X-Attention-Proof-Timestamp": str(issued), "X-Attention-Proof-Signature": signature},
        secret=SECRET, expected_nonce=nonce, expected_scope=expected, now=float(issued),
    )


def test_reserve_requires_new_event_and_never_releases_by_age(session):
    receipt = event(session)
    first = fc.reserve(session, scope(receipt), receipt.id)
    session.commit()
    assert first.status == "RESERVED" and first.token is not None
    assert fc.reserve(session, scope(receipt), receipt.id) == fc.ReservationResult("REPLAY")
    newer = event(session, offset=10)
    assert fc.reserve(session, scope(newer), newer.id) == fc.ReservationResult("BUSY")
    row = session.get(FirstContactPresentationRow, first.token.scope_id)
    row.updated_at -= timedelta(days=90)
    session.commit()
    assert fc.reserve(session, scope(newer), newer.id).status == "BUSY"
    assert session.scalar(select(func.count()).select_from(FirstContactAttemptRow)) == 1


def test_generation_failure_releases_only_new_entry_and_preserves_attempt(session):
    first_event = event(session)
    token = fc.reserve(session, scope(first_event), first_event.id).token
    session.commit()
    assert fc.fail_before_effect(session, token, "AGENT_FAILURE")
    session.commit()
    assert fc.reserve(session, scope(first_event), first_event.id).status == "REPLAY"
    newer = event(session, offset=10)
    second = fc.reserve(session, scope(newer), newer.id)
    assert second.status == "RESERVED" and second.token.generation == 2
    session.commit()
    assert session.get(FirstContactAttemptRow, token.attempt_id).state == "FAILED"
    assert session.scalar(select(func.count()).select_from(FirstContactPresentationRow)) == 1
    assert session.scalar(select(func.count()).select_from(FirstContactAttemptRow)) == 2
    with pytest.raises(fc.FirstContactConflict, match="STALE_ATTEMPT"):
        fc.fail_before_effect(session, token, "AGENT_FAILURE")


@pytest.mark.parametrize("kind", ["group", "owner", "binding", "account", "tenant", "identity", "no_proof"])
def test_ineligible_or_cross_scope_event_cannot_reserve(session, kind):
    receipt = event(session, group=kind == "group", origin="OWNER_COMMAND" if kind == "owner" else "EXTERNAL_INBOUND")
    target = scope(receipt)
    if kind == "binding":
        upsert_actor_binding(session, "wwebjs", PEER, "synthetic-owner", "owner", metadata={"owner": True})
    elif kind == "account":
        target = replace(target, source_account="wrong-account")
    elif kind == "tenant":
        target = replace(target, tenant_id="synthetic-other-tenant")
    elif kind == "identity":
        target = replace(target, canonical_peer_hash="a" * 64)
    elif kind == "no_proof":
        receipt.payload = {**receipt.payload, "metadata": {**receipt.payload["metadata"],
                                                         "first_contact_identity": None}}
    with pytest.raises(fc.FirstContactConflict):
        fc.reserve(session, target, receipt.id)
    assert session.scalar(select(func.count()).select_from(FirstContactPresentationRow)) == 0


def test_scope_uniqueness_does_not_include_presentation_version(session):
    unique = next(c for c in FirstContactPresentationRow.__table__.constraints
                  if c.name == "uq_first_contact_scope")
    assert list(unique.columns.keys()) == [
        "tenant_id", "source", "channel", "source_account", "canonical_peer_hash",
    ]
    receipt = event(session)
    with pytest.raises(fc.FirstContactConflict, match="VERSION_UNSUPPORTED"):
        fc.reserve(session, scope(receipt), receipt.id, "first-contact-v2")


def test_verified_phone_and_lid_aliases_share_state_without_inheriting_binding(session):
    phone = "5500000000211@c.us"
    receipt = event(session, peer=phone, canonical_peer=PEER, aliases=[phone, PEER])
    first = fc.reserve(session, scope(receipt), receipt.id)
    session.commit()
    second = event(session, offset=10)
    assert fc.reserve(session, scope(second), second.id).status == "BUSY"
    assert session.scalar(select(func.count()).select_from(FirstContactPresentationRow)) == 1
    assert fc.fail_before_effect(session, first.token, "GENERATION_FAILED")
    session.commit()
    upsert_actor_binding(session, "wwebjs", phone, "synthetic-owner", "owner")
    third = event(session, offset=20, aliases=[PEER, phone])
    with pytest.raises(fc.FirstContactConflict, match="BINDING_DENIED"):
        fc.reserve(session, scope(third), third.id)


def test_accounts_and_tenants_have_separate_presentation_state(session):
    receipt = event(session)
    assert fc.reserve(session, scope(receipt), receipt.id).status == "RESERVED"
    session.commit()
    second = event(session, account="other-account")
    assert fc.reserve(session, scope(second), second.id).status == "RESERVED"
    session.commit()
    tenant = TenantRow(id=new_id(), slug="fc-test-" + new_id(), name="Synthetic tenant",
                       status="ACTIVE", created_at=now_utc(), updated_at=now_utc())
    session.add(tenant)
    session.flush()
    third = event(session, tenant=tenant.id)
    assert fc.reserve(session, scope(third), third.id).status == "RESERVED"
    assert session.scalar(select(func.count()).select_from(FirstContactPresentationRow)) == 3


def test_generated_and_queued_are_not_confirmed_and_bind_is_idempotent(session):
    case = queued(session)
    assert not fc.bind_outbox(session, case.decision.id, case.intent.id, case.outbox.id)
    row = session.get(FirstContactPresentationRow, case.token.scope_id)
    assert row.state == "QUEUED" and row.confirmed_at is None
    attempt = session.get(FirstContactAttemptRow, case.token.attempt_id)
    assert attempt.generated_at is not None and attempt.text_hash == fc.text_hash(INTRO)
    assert fc.delivery_scope(session, case.outbox.id)["attempt_id"] == case.token.attempt_id
    with pytest.raises(fc.FirstContactConflict, match="EFFECT_ALREADY_BOUND"):
        fc.fail_before_effect(session, case.token, "GENERATION_FAILURE")


def test_review_authorized_original_snapshot_accepts_late_ack_after_revocation(session):
    case = queued(session, authorization_source="HUMAN_REVIEW")
    proof = signed_proof(session, case)
    review = session.get(AgentResponseReviewRow, case.intent.response_review_id)
    review.status = "REJECTED"
    session.commit()
    assert fc.confirm_delivery(session, case.outbox.id, proof)
    session.commit()
    assert session.get(FirstContactPresentationRow, case.token.scope_id).state == "CONFIRMED"


@pytest.mark.parametrize("forgery", ["source", "missing_review", "different_decision"])
def test_forged_review_authorization_cannot_bind_first_contact(session, forgery):
    receipt = event(session)
    token = fc.reserve(session, scope(receipt), receipt.id).token
    row = decision(session, receipt)
    fc.record_generated(session, token, row.id, fc.text_hash(INTRO))
    intent, outbox = effect(session, row, authorization_source="HUMAN_REVIEW")
    if forgery == "source":
        intent.authorization_source = "FORGED_SOURCE"
    elif forgery == "missing_review":
        intent.response_review_id = None
    else:
        different = decision(session, event(session, offset=10))
        session.get(AgentResponseReviewRow, intent.response_review_id).agent_decision_id = different.id
    session.flush()
    with pytest.raises(fc.FirstContactConflict, match="AUTHORIZATION_MISMATCH"):
        fc.bind_outbox(session, row.id, intent.id, outbox.id)
    assert session.get(FirstContactPresentationRow, token.scope_id).state == "RESERVED"


def test_review_cannot_bind_edited_response_as_original_first_contact(session):
    receipt = event(session)
    token = fc.reserve(session, scope(receipt), receipt.id).token
    row = decision(session, receipt)
    fc.record_generated(session, token, row.id, fc.text_hash(INTRO))
    intent, outbox = effect(session, row, authorization_source="HUMAN_REVIEW")
    intent.effective_response_snapshot = "Synthetic operator edit"
    outbox.payload = {**outbox.payload, "text": intent.effective_response_snapshot}
    session.flush()
    with pytest.raises(fc.FirstContactConflict, match="TEXT_MISMATCH"):
        fc.bind_outbox(session, row.id, intent.id, outbox.id)
    assert session.get(FirstContactPresentationRow, token.scope_id).state == "RESERVED"


@pytest.mark.parametrize("mutation", ["text", "intent", "recipient", "destination", "key"])
def test_bound_effect_rejects_changed_snapshot_or_correlation(session, mutation):
    case = queued(session)
    if mutation == "text":
        case.outbox.payload = {**case.outbox.payload, "text": "different synthetic text"}
    elif mutation == "intent":
        case.intent.effective_response_snapshot = "different synthetic text"
    elif mutation == "recipient":
        case.outbox.payload = {**case.outbox.payload, "external_actor_id": "5500000000222@lid"}
    elif mutation == "destination":
        case.outbox.destination = "other-transport"
    else:
        case.outbox.idempotency_key = "synthetic-changed-key"
    session.flush()
    with pytest.raises(fc.FirstContactConflict):
        fc.delivery_scope(session, case.outbox.id)


def test_generated_text_and_decision_must_match_reservation(session):
    receipt = event(session)
    token = fc.reserve(session, scope(receipt), receipt.id).token
    row = decision(session, receipt)
    with pytest.raises(fc.FirstContactConflict, match="TEXT_MISMATCH"):
        fc.record_generated(session, token, row.id, fc.text_hash("wrong"))
    other = decision(session, event(session, offset=5))
    with pytest.raises(fc.FirstContactConflict, match="DECISION_MISMATCH"):
        fc.record_generated(session, token, other.id, fc.text_hash(INTRO))


def test_claim_is_explicit_and_done_does_not_confirm_delivery(session):
    case = queued(session)
    with pytest.raises(fc.FirstContactConflict, match="NOT_CLAIMED"):
        fc.mark_sending(session, case.outbox.id)
    case.outbox.status, case.outbox.attempt_count = "PROCESSING", 1
    session.flush()
    assert fc.mark_sending(session, case.outbox.id)
    session.commit()
    case.outbox.status = "DONE"
    session.flush()
    assert fc.observe_outbox(session, case.outbox.id)
    session.commit()
    assert session.get(FirstContactPresentationRow, case.token.scope_id).state == "SENT"
    assert fc.reserve(session, case.scope, event(session, offset=20).id).status == "BUSY"


@pytest.mark.parametrize("status", ["FAILED", "BLOCKED", "CANCELED", "CANCELLED"])
def test_proven_failure_before_effect_allows_only_next_new_entry(session, status):
    case = queued(session)
    case.outbox.status = status
    session.flush()
    assert fc.observe_outbox(session, case.outbox.id, known_before_effect=True)
    session.commit()
    assert session.get(FirstContactPresentationRow, case.token.scope_id).state == "FAILED"
    assert fc.reserve(session, case.scope, case.event.id).status == "REPLAY"
    assert fc.reserve(session, case.scope, event(session, offset=20).id).status == "RESERVED"


@pytest.mark.parametrize("status", ["AMBIGUOUS", "RECONCILIATION_REQUIRED", "FAILED"])
def test_uncertain_effect_never_releases_reservation(session, status):
    case = queued(session)
    case.outbox.status, case.outbox.attempt_count = "PROCESSING", 1
    session.flush()
    fc.mark_sending(session, case.outbox.id)
    session.commit()
    case.outbox.status = status
    session.flush()
    assert fc.observe_outbox(session, case.outbox.id)
    session.commit()
    assert session.get(FirstContactPresentationRow, case.token.scope_id).state == "AMBIGUOUS"
    assert fc.reserve(session, case.scope, event(session, offset=20).id).status == "BUSY"


@pytest.mark.parametrize("ack", [None, 0, 1])
def test_authenticated_transport_acceptance_is_not_device_confirmation(session, ack):
    case = queued(session)
    proof = signed_proof(session, case, ack=ack)
    assert not fc.confirm_delivery(session, case.outbox.id, proof)
    session.commit()
    row = session.get(FirstContactPresentationRow, case.token.scope_id)
    assert row.state == "SENT" and row.confirmed_at is None


def test_authenticated_ack_confirms_once_and_survives_new_input(session):
    case = queued(session)
    proof = signed_proof(session, case)
    assert fc.confirm_delivery(session, case.outbox.id, proof)
    session.commit()
    confirmed_at = session.get(FirstContactPresentationRow, case.token.scope_id).confirmed_at
    assert not fc.confirm_delivery(session, case.outbox.id, proof)
    session.commit()
    row = session.get(FirstContactPresentationRow, case.token.scope_id)
    assert row.state == "CONFIRMED" and row.confirmed_at == confirmed_at
    assert fc.reserve(session, case.scope, event(session, offset=20).id).status == "CONFIRMED"


def test_ack_monotonicity_preserves_confirming_evidence(session):
    case = queued(session)
    first = signed_proof(session, case, ack=3)
    fc.confirm_delivery(session, case.outbox.id, first)
    session.commit()
    older = signed_proof(session, case, ack=1)
    assert not fc.confirm_delivery(session, case.outbox.id, older)
    session.commit()
    attempt = session.get(FirstContactAttemptRow, case.token.attempt_id)
    assert attempt.state == "CONFIRMED" and attempt.ack == 3
    assert attempt.proof_sha256 == first.body_sha256


def test_unknown_signed_snapshot_never_confirms_or_records_transport_acceptance(session):
    case = queued(session)
    proof = signed_proof(session, case, status="UNKNOWN")
    assert not fc.confirm_delivery(session, case.outbox.id, proof)
    session.commit()
    attempt = session.get(FirstContactAttemptRow, case.token.attempt_id)
    assert attempt.state == "QUEUED" and attempt.ack is None and attempt.message_reference is None


def test_late_confirmation_keeps_original_identity_after_new_binding(session):
    case = queued(session)
    proof = signed_proof(session, case)
    upsert_actor_binding(session, "wwebjs", PEER, "synthetic-now-bound", "owner")
    session.commit()
    assert fc.confirm_delivery(session, case.outbox.id, proof)
    session.commit()
    assert session.get(FirstContactPresentationRow, case.token.scope_id).state == "CONFIRMED"


@pytest.mark.parametrize("field,value", [
    ("tenant_id", "synthetic-other"), ("source_account", "wrong-account"),
    ("canonical_peer_hash", "b" * 64), ("attempt_id", "wrong-attempt"),
    ("execution_intent_id", "wrong-intent"), ("idempotency_key", "execution:wrong-key"),
    ("outbox_id", "wrong-outbox"),
    ("text_hash", "c" * 64),
])
def test_authenticated_but_wrong_effect_proof_is_rejected(session, field, value):
    case = queued(session)
    proof = signed_proof(session, case, **{field: value})
    with pytest.raises(fc.FirstContactConflict, match="PROOF_SCOPE_MISMATCH"):
        fc.confirm_delivery(session, case.outbox.id, proof)
    assert session.get(FirstContactPresentationRow, case.token.scope_id).state == "QUEUED"


def test_reference_cannot_change_after_authenticated_sent_receipt(session):
    case = queued(session)
    fc.confirm_delivery(session, case.outbox.id, signed_proof(session, case, ack=None))
    session.commit()
    proof = signed_proof(session, case, reference="synthetic-other-reference")
    with pytest.raises(fc.FirstContactConflict, match="REFERENCE_MISMATCH"):
        fc.confirm_delivery(session, case.outbox.id, proof)


def test_unverified_object_cannot_confirm_receipt(session):
    case = queued(session)
    with pytest.raises(fc.FirstContactConflict, match="AUTHENTICATED_PROOF_REQUIRED"):
        fc.confirm_delivery(session, case.outbox.id, SimpleNamespace(authenticated=True, ack=4))


def test_database_rejects_confirmation_without_ack_or_effect(session):
    receipt = event(session)
    token = fc.reserve(session, scope(receipt), receipt.id).token
    session.commit()
    with pytest.raises(IntegrityError), session.begin_nested():
        attempt = session.get(FirstContactAttemptRow, token.attempt_id)
        attempt.state, attempt.confirmed_at = "CONFIRMED", now_utc()
        session.flush()
    assert session.get(FirstContactAttemptRow, token.attempt_id).state == "RESERVED"


@pytest.mark.parametrize("missing", ["ack", "ack_at", "execution_intent_id", "proof_sha256"])
def test_confirmation_constraint_rejects_null_required_evidence(session, missing):
    case = queued(session)
    with pytest.raises(IntegrityError), session.begin_nested():
        attempt = session.get(FirstContactAttemptRow, case.token.attempt_id)
        attempt.state, attempt.confirmed_at = "CONFIRMED", now_utc()
        attempt.ack, attempt.ack_at = 2, now_utc()
        attempt.proof_sha256, attempt.message_reference = "a" * 64, "synthetic-reference"
        setattr(attempt, missing, None)
        session.flush()
    assert session.get(FirstContactAttemptRow, case.token.attempt_id).state == "QUEUED"


@pytest.mark.parametrize("mutation", ["tenant", "source_account", "audience", "binding"])
def test_linked_effect_revalidates_original_scope_and_decision_authority(session, mutation):
    case = queued(session)
    if mutation == "tenant":
        tenant = TenantRow(id=new_id(), slug="fc-" + new_id(), name="Synthetic tenant",
                           status="ACTIVE", created_at=now_utc(), updated_at=now_utc())
        session.add(tenant)
        session.flush()
        case.event.tenant_id = tenant.id
    elif mutation == "source_account":
        case.event.payload = {**case.event.payload,
                              "metadata": {**case.event.payload["metadata"], "source_account": "other"}}
    elif mutation == "audience":
        case.decision.audience = "owner"
    else:
        binding = upsert_actor_binding(session, source="wwebjs", external_actor_id=PEER,
                                       actor_key="synthetic-bound", actor_category="owner")
        case.decision.actor_binding_id = binding.id
    session.flush()
    with pytest.raises(fc.FirstContactConflict):
        fc.delivery_scope(session, case.outbox.id)


def test_service_mutations_remain_in_callers_transaction(session):
    receipt = event(session)
    session.commit()
    fc.reserve(session, scope(receipt), receipt.id)
    session.rollback()
    assert session.scalar(select(func.count()).select_from(FirstContactPresentationRow)) == 0
    assert session.scalar(select(func.count()).select_from(FirstContactAttemptRow)) == 0
    assert session.scalar(select(func.count()).select_from(ActorBindingRow)) == 0
