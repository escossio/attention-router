"""Run the integrated travel scenario with synthetic ingress and real persistence."""

from dataclasses import asdict
from datetime import timedelta
import json
import os
from pathlib import Path
from secrets import token_bytes
import subprocess
import sys

import pytest
from sqlalchemy import func, select

from andy_lab import AUTHORITY, ENVIRONMENT, PREDICATE, QUERY, interpret, recover
from attention_router.application.candidate_insights import (
    CandidateEvidenceInput,
    propose_candidate_insight,
    reject_candidate_insight,
)
from attention_router.application.context_consent import LabConsent, RETENTION_LAYERS, SourceConsent
from attention_router.application.context_epoch import ContextEpoch
from attention_router.application.context_preactivation import ContextPreactivation
from attention_router.application.semantic_episodes import assign_timeline_event_to_episode
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.candidate_insight_models import CandidateInsightEvidenceRow, CandidateInsightRow
from attention_router.infrastructure.context_epoch_lab import LabEpochAuthority
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.obligation_models import ObligationInstanceRow
from attention_router.infrastructure.models import (
    ActorBindingRow,
    AgentExecutionIntentRow,
    CapabilityGrantRow,
    ConversationMessageRow,
    ExecutionIntentRow,
    FactRow,
    InboundEventRow,
    MemoryClaimRow,
    OutboxMessageRow,
    QueueRow,
    RelationshipRow,
    TimelineEventRow,
)
from test_cognitive_observation import ACCOUNT, START, _attachment, _seed_observation_owner
from test_postgres_context_preactivation import enroll_role  # noqa: F401


QUESTION = "Para que dia ficou a viagem?"
DRAFT = "Planejamos a viagem para sábado."
PROPOSAL = "Proponho mudar a viagem para domingo."
CONFIRM = "Confirmo a viagem para domingo."
CONFLICT = "Confirmo a viagem para sábado, 21/11/2026."
RENEWED = "Confirmo a viagem para domingo, 22/11/2026."
UNKNOWN = "Talvez Jo do outro grupo tenha confirmado a viagem."


@pytest.fixture
def lab(Session, monkeypatch, enroll_role, tmp_path, request):  # noqa: F811
    ids = {}
    grants = []
    deadline = START + timedelta(days=30)
    client_query = getattr(request, "param", None) == "client-query"
    captions = (
        ("Planejamos a viagem para sábado, 28/11/2026.",
         "Proponho mudar a viagem para domingo, 29/11/2026.",
         "Confirmo a viagem para domingo, 29/11/2026.",
         "Confirmo a viagem para sábado, 28/11/2026.") if client_query
        else (DRAFT, PROPOSAL, CONFIRM, CONFLICT)
    )
    with Session.begin() as session:
        _seed_observation_owner(session, monkeypatch)
        session.get(ActorBindingRow, "fixture-peer-binding").display_name = "Jo Silva"
        session.add(ActorBindingRow(
            id="fixture-peer-neighbor-binding", tenant_id=DEFAULT_TENANT_ID, source="wwebjs",
            external_actor_id="fixture-peer-neighbor", actor_key="fixture-peer-neighbor-binding",
            actor_category="known_contact", display_name="Jo Silveira", binding_metadata={},
            is_active=True, created_at=START, updated_at=START,
        ))
        for name, day, caption, conversation, sender in (
            ("plan", 1, captions[0], "trip-first", "fixture-peer"),
            ("proposal", 2, captions[1], "trip-second", "fixture-peer"),
            ("confirm", 3, captions[2], "trip-second", "fixture-peer"),
            ("conflict", 3, captions[3], "trip-first", "fixture-peer"),
            ("new", 4, RENEWED, "trip-third", "fixture-peer"),
            ("ambiguous", 4, UNKNOWN, "trip-third", "fixture-person-unresolved"),
        ):
            message_id, _, registered = _attachment(
                session, name, day, content=caption, conversation=f"wwebjs:{conversation}", sender=sender,
            )
            ids[name] = message_id
            message = session.get(ConversationMessageRow, message_id)
            grants.append(SourceConsent(
                decision_ref=f"fixture:consent:{name}", tenant_id=DEFAULT_TENANT_ID,
                owner_id="fixture-human-owner", source_account=ACCOUNT,
                conversation_id=message.conversation_id, message_id=message_id,
                receipt_id=message.metadata_json["artifact_receipt_id"], purpose="prospective_context",
                starts_at=message.sent_at, expires_at=deadline, sensitivity="NORMAL",
                retention=tuple((layer, deadline) for layer in sorted(RETENTION_LAYERS)),
            ))
        resource_id = registered.artifact.resource_id
        ids["other_account"], _, _ = _attachment(session, "same-other-account", 2, content=CONFIRM, account="other")
        ids["other_tenant"], _, _ = _attachment(session, "same-other-tenant", 2, content=CONFIRM, tenant_id="other")

    role, Reader = enroll_role(current=True, source_withdrawal=True)
    key = token_bytes(32)
    authority = LabEpochAuthority(tmp_path / "epoch.sqlite", authority_id=AUTHORITY, key=key)
    epoch = ContextEpoch(authority, authority_id=AUTHORITY, environment=ENVIRONMENT, key=key)
    consent = LabConsent(tmp_path / "consent.sqlite", tuple(grants))
    consent.initialize()
    now = START + timedelta(days=1, hours=1)
    scope = dict(
        tenant_id=DEFAULT_TENANT_ID, binding_id="fixture-owner",
        owner_human_identity_id="fixture-human-owner", source_account=ACCOUNT, start=START,
    )
    pilot = ContextPreactivation(Reader, role=role, epoch=epoch, consent=consent, clock=lambda: now)
    with consent.current() as (_, permits), Reader.begin() as session:
        context = pilot._context(session, permits, scope)
        authority.initialize(epoch.domain(context), epoch.checkpoint(context))

    effect_models = (
        OutboxMessageRow, ExecutionIntentRow, AgentExecutionIntentRow, CapabilityGrantRow,
        FactRow, MemoryClaimRow, RelationshipRow, ObligationInstanceRow,
    )

    def negative_effects():
        with Session() as session:
            counters = {model.__tablename__: session.scalar(select(func.count()).select_from(model))
                        for model in effect_models}
            counters["queue_non_index"] = session.scalar(
                select(func.count()).select_from(QueueRow).where(QueueRow.kind != "conversation_vector_v1")
            )
            return counters

    initial_effects = negative_effects()

    def verify_no_effects():
        assert negative_effects() == initial_effects

    def advance_clock(*, day, hour=1):
        nonlocal now
        now = START + timedelta(days=day, hours=hour)

    def change(operation):
        with consent.current() as (_, permits), Reader.begin() as session:
            context = pilot._context(session, permits, scope)
            return epoch.advance(context, operation, context)

    def observe(name, *, abort=False):
        def ingest(context):
            session = context.session
            result = context.observe((ids[name],))
            if not result.linked:
                return None, result

            _, graph = context.graph()
            graph_ids = {node.source_id for node in graph.nodes if node.source_type == "TIMELINE_EVENT"}
            event = session.scalar(select(TimelineEventRow).where(
                TimelineEventRow.tenant_id == DEFAULT_TENANT_ID,
                TimelineEventRow.id.in_(graph_ids),
                TimelineEventRow.event_ref["conversation_message_id"].as_string() == ids[name],
            ))
            assert event is not None
            message = session.get(ConversationMessageRow, ids[name])
            inbound = session.get(InboundEventRow, event.event_ref["inbound_event_id"])
            assert message.text == inbound.payload["content"]
            parsed = interpret(message.text, event.occurred_at) if event.actor_id else None
            if parsed is None or event.resource_id != resource_id:
                return None, result

            scope_key = stable_hash({"binding_id": context.binding.id, "source_account": ACCOUNT})
            assigned = assign_timeline_event_to_episode(
                session, tenant_id=DEFAULT_TENANT_ID, event_id=event.id,
                episode_type=f"ATTACHMENT_OBSERVED:{scope_key[:32]}", scope_type="RESOURCE",
                max_inactivity=timedelta(days=6), confidence=parsed.confidence, now=now,
            )
            evidence = [CandidateEvidenceInput(
                "TIMELINE_EVENT", event.id, event.canonical_event_id, parsed.confidence,
                provenance={
                    "archive_message_id": message.id, "span": [0, len(message.text)],
                    "caption_fingerprint": stable_hash(message.text),
                },
            )]
            if parsed.action == "CONFIRMS":
                prior = session.execute(
                    select(CandidateInsightEvidenceRow, TimelineEventRow)
                    .join(CandidateInsightRow, CandidateInsightRow.id == CandidateInsightEvidenceRow.candidate_id)
                    .join(TimelineEventRow, TimelineEventRow.id == CandidateInsightEvidenceRow.source_ref)
                    .where(
                        CandidateInsightRow.tenant_id == context.arguments["tenant_id"],
                        CandidateInsightRow.subject_id == event.resource_id,
                        CandidateInsightRow.predicate == PREDICATE,
                        CandidateInsightRow.state == "PROPOSED",
                        CandidateInsightRow.evidence_status == "CURRENT",
                        CandidateInsightRow.provenance["context_scope"].as_string() == context.scope,
                        CandidateInsightRow.proposed_value["date"].as_string() == parsed.date,
                        CandidateInsightRow.proposed_value["action"].as_string().in_(("PROPOSES", "COUNTERPROPOSES")),
                        CandidateInsightEvidenceRow.evidence_type == "TIMELINE_EVENT",
                        CandidateInsightEvidenceRow.evidence_role == "SUPPORT",
                        TimelineEventRow.tenant_id == context.arguments["tenant_id"],
                        TimelineEventRow.id.in_(graph_ids),
                        TimelineEventRow.occurred_at < event.occurred_at,
                    ).order_by(TimelineEventRow.occurred_at.desc(), TimelineEventRow.id).limit(1)
                ).one_or_none()
                if prior is not None:
                    source, proposal = prior
                    evidence.append(CandidateEvidenceInput(
                        "TIMELINE_EVENT", proposal.id, source.independence_key, source.confidence,
                        provenance=source.provenance,
                    ))
            row, created = propose_candidate_insight(
                session, tenant_id=DEFAULT_TENANT_ID, insight_type="STATE_PROPOSAL",
                subject_type="RESOURCE", subject_id=event.resource_id, predicate=PREDICATE,
                proposed_value={
                    "action": parsed.action, "date": parsed.date, "episode_id": assigned.episode.id,
                },
                source_engine="RULE", confidence=parsed.confidence, now=now,
                evidence=evidence,
                provenance={"context_scope": context.scope, "parser": parsed.parser},
            )
            if assigned.created_membership or created:
                context._advance("LAB_SEMANTIC_INTERPRETATION", message.id)
            if abort:
                raise RuntimeError("COGNITIVE_LAB_INTERRUPTED_BEFORE_COMMIT")
            return row.id, result

        outcome = change(ingest)
        verify_no_effects()
        return outcome

    def query_configuration():
        return {
            "scope": {**scope, "start": scope["start"].isoformat(), "now": now.isoformat()},
            "consent": str(consent.path), "authority": str(authority.path), "secret": key.hex(),
            "grants": [asdict(grant) for grant in grants], "resource_id": resource_id,
        }

    def query(question=QUESTION, fresh_process=False):
        if not fresh_process:
            with consent.current() as (_, permits), Reader.begin() as session:
                context = pilot._context(session, permits, scope)
                return recover(context, epoch, question, resource_id)
        args = {
            "url": Reader.kw["bind"].url.render_as_string(hide_password=False),
            **query_configuration(), "question": question,
        }
        child = subprocess.run(
            [sys.executable, "-m", "andy_lab"], input=json.dumps(args, default=lambda value: value.isoformat()),
            env={**os.environ, "PYTHONPATH": os.pathsep.join((str(Path(__file__).parent), str(Path.cwd())))},
            text=True, stdout=subprocess.PIPE, check=True, timeout=25,
        )
        return json.loads(child.stdout)

    return ids, advance_clock, observe, query, change, pilot, scope, consent, verify_no_effects, query_configuration


@pytest.mark.postgres
def test_integrated_cognitive_laboratory(Session, lab):
    ids, clock, observe, query, change, pilot, scope, consent, no_effects, _ = lab

    def stage(name, state, date=None, *, restart=False):
        result = query(fresh_process=restart)
        if restart:
            assert result == query()
        assert result["answer"]["state"] == state
        assert result["answer"].get("date") == date
        no_effects()
        print("andy_cognitive_lab " + json.dumps({"stage": name, **result}, ensure_ascii=False))
        return result

    stage("empty", "INSUFFICIENT_EVIDENCE")
    assert observe("plan")[1].new_links == 1
    first = stage("initial_plan", "PROPOSAL_ONLY", "2026-11-21")
    assert first["answer"]["evidence"][0]["sources"][0]["original_id"] == "plan"
    clock(day=2)
    observe("proposal")
    proposed = stage("change_proposed", "PROPOSAL_ONLY", "2026-11-22")
    assert len(proposed["episodes"]) == 1 and len(proposed["episodes"][0]["memberships"]) == 2
    assert len({record["sources"][0]["conversation_id"] for record in proposed["history"]}) == 2
    for intruder in ("other_account", "other_tenant"):
        with pytest.raises(ValueError, match="CONTEXT_CONSENT_SOURCE_DENIED"):
            observe(intruder)

    clock(day=3)
    accepted, _ = observe("confirm")
    old, old_packet = pilot.compile(QUERY, **scope)
    reopened = stage("confirmation_after_restart", "CONFIRMATION_SUPPORTED", "2026-11-22", restart=True)
    assert reopened["answer"]["evidence"][0]["candidate_id"] == accepted
    assert {item["original_id"] for item in reopened["answer"]["evidence"][0]["sources"]} == {"proposal", "confirm"}
    corrected, _ = observe("conflict")
    stage("direct_conflict", "CONFLICTING_EVIDENCE")
    clock(day=3, hour=2)

    def correct(context):
        reject_candidate_insight(
            context.session, candidate_id=corrected, decision_actor_key=context.binding.actor_key,
            decision_ref="fixture:owner-correction", now=context.arguments["now"],
        )
        context._advance("LAB_OWNER_CORRECTION")

    change(correct)
    corrected_view = stage("owner_corrected", "CONFIRMATION_SUPPORTED", "2026-11-22")
    assert next(row for row in corrected_view["history"] if row["candidate_id"] == corrected)["state"] == "REJECTED"
    revision = corrected_view["token"]
    assert observe("conflict")[0] == corrected
    assert query()["token"] == revision

    change(lambda context: context.revoke_message(ids["confirm"]))
    revoked = stage("confirmation_withdrawn", "PROPOSAL_ONLY", "2026-11-22")
    historical = next(row for row in revoked["history"] if row["candidate_id"] == accepted)
    assert historical["evidence_status"] == "EVIDENCE_REVOKED"
    original_support = {item["original_id"]: item for item in historical["sources"]}
    assert original_support["confirm"]["timeline_event_id"] not in revoked["visible_events"]
    assert original_support["proposal"]["timeline_event_id"] in revoked["visible_events"]
    assert revoked["episodes"][0]["revision"]["invalid_member_refs"]
    assert observe("confirm")[1].rejected == {"ARCHIVE_INELIGIBLE": 1}
    with pytest.raises(ValueError, match="CONTEXT_EPOCH_TOKEN_STALE"):
        pilot.consume(old, old_packet, **scope)

    clock(day=4)
    assert observe("ambiguous")[0] is None
    with Session() as session:
        ambiguous_event = session.scalar(select(TimelineEventRow).where(
            TimelineEventRow.event_ref["conversation_message_id"].as_string() == ids["ambiguous"],
        ))
        assert ambiguous_event is not None and ambiguous_event.actor_id is None
    assert query("Quem é Jo?")["answer"]["state"] == "INSUFFICIENT_EVIDENCE"
    before_interruption = query()
    with pytest.raises(RuntimeError, match="COGNITIVE_LAB_INTERRUPTED_BEFORE_COMMIT"):
        observe("new", abort=True)
    assert query() == before_interruption
    renewed, _ = observe("new")
    assert renewed not in {accepted, corrected}
    final = stage("new_confirmation_after_restart", "CONFIRMATION_SUPPORTED", "2026-11-22", restart=True)
    assert final["answer"]["evidence"][0]["candidate_id"] == renewed
    assert final["answer"]["evidence"][0]["decision_ref"] is None
    assert len(final["history"]) == 5
    assert len({member["timeline_event_id"] for member in final["episodes"][0]["memberships"]}) == 5

    if question := os.environ.get("ANDY_COGNITIVE_LAB_QUESTION"):
        output = query(question, fresh_process=True)
        print("andy_cognitive_answer " + json.dumps({"question": question, **output["answer"]}, ensure_ascii=False))

    token, snapshot = pilot.compile(QUERY, **scope)
    assert consent.revoke(ids["new"])
    with pytest.raises(ValueError, match="CONTEXT_CONSENT_REVISION_STALE"):
        pilot.consume(token, snapshot, **scope)
    redacted = stage("consent_withdrawn", "PROPOSAL_ONLY", "2026-11-22", restart=True)
    assert ids["new"] not in json.dumps(redacted)
    historical = query("histórico", fresh_process=True)
    assert ids["new"] not in json.dumps(historical)
    with pytest.raises(ValueError, match="CONTEXT_CONSENT_SOURCE_DENIED"):
        observe("new")
    with Session() as session:
        assert session.scalar(select(func.count()).select_from(CandidateInsightRow)) == 5
    no_effects()
