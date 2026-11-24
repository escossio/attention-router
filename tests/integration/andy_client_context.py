"""Mount the contextual route in a test-only app and reuse the travel reader."""

from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
import json
import re

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from starlette.routing import Route

from andy_lab import recover, source_text
from attention_router.api.v1.client_context import build_client_context_router
from attention_router.api.v1.client_context_staged import StagedContextEndpoint
from attention_router.application.client_context import ClientContextReader, ClientContextScope
from attention_router.application.client_context_staged import StagedClientContextReader
from attention_router.application.cognitive_observation import (
    lock_conversation_dependencies, observation_event_is_current,
)
from attention_router.application.context_validity import ContextValidity
from attention_router.application.staged_read import DetachedEvidence, SourceVersion, StagedReadRejected
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.artifact_models import ArtifactReceiptRow
from attention_router.infrastructure.hashing import stable_json
from attention_router.infrastructure.models import ConversationMessageRow, TimelineEventRow
from test_postgres_client_session import service


QUERY_PATH = "/api/v1/client/context/query"


def enroll_fixture_fence(sessions, request):
    """Only test cases that exercise a fenced reader enroll a synthetic tenant."""
    with sessions.begin() as db:
        added = db.execute(text(
            "INSERT INTO public.andy_context_fenced_tenants (tenant_id, enabled) "
            "VALUES (:tenant, true) ON CONFLICT DO NOTHING"
        ), {"tenant": DEFAULT_TENANT_ID}).rowcount
    if added:
        def release():
            with sessions.begin() as db:
                db.execute(text("DELETE FROM public.andy_context_fenced_tenants WHERE tenant_id=:tenant"),
                           {"tenant": DEFAULT_TENANT_ID})
        request.addfinalizer(release)


def read(context, epoch, question, resource_id):
    answer = recover(context, epoch, question, resource_id, batch=True)["answer"]
    kind, conclusion, uncertainty = {
        "INSUFFICIENT_EVIDENCE": (
            "INSUFFICIENT_EVIDENCE", None, "Falta evidência vigente para responder à pergunta."
        ),
        "PROPOSAL_ONLY": (
            "HYPOTHESIS", "Há uma proposta de data para a viagem.", "A data ainda precisa de confirmação."
        ),
        "CONFIRMATION_SUPPORTED": (
            "OBSERVATION", "A fonte registra uma confirmação da data da viagem.",
            "A confirmação registrada não comprova que a viagem aconteceu."
        ),
        "CONFLICTING_EVIDENCE": (
            "CONFLICT", None, "As fontes registram confirmações com datas divergentes."
        ),
    }.get(answer["state"], ("INSUFFICIENT_EVIDENCE", None, "Pergunta fora do piloto."))
    evidence = {}
    if kind != "INSUFFICIENT_EVIDENCE":
        for record in answer["evidence"]:
            for source in record["sources"]:
                if not source["current"]:
                    raise ValueError("CLIENT_CONTEXT_SUPPORT_NOT_CURRENT")
                event, message = context.session.execute(
                    select(TimelineEventRow, ConversationMessageRow)
                    .join(ConversationMessageRow, TimelineEventRow.event_ref["conversation_message_id"].as_string()
                          == ConversationMessageRow.id)
                    .where(TimelineEventRow.id == source["timeline_event_id"],
                           ConversationMessageRow.id == source["message_id"],
                           TimelineEventRow.tenant_id == context.arguments["tenant_id"],
                           ConversationMessageRow.tenant_id == context.arguments["tenant_id"],
                           ConversationMessageRow.source_account == context.arguments["source_account"])
                ).one()
                excerpt = source_text(context.session, event, message)
                if not excerpt:
                    raise ValueError("CLIENT_CONTEXT_SUPPORT_MISSING")
                evidence[message.id] = {
                    "source_ref": message.id, "observed_at": source["occurred_at"], "excerpt": excerpt,
                }
    return {
        "kind": kind, "conclusion": conclusion, "date": answer.get("date"),
        "conditions": (), "uncertainty": uncertainty, "evidence": tuple(evidence.values()),
    }


def app(sessions, role, consent, epoch, scope, scope_ref, resource_id, clock, auth_clock, read_fn=read):
    scope = ClientContextScope(
        scope["tenant_id"], scope["owner_human_identity_id"], scope["binding_id"],
        scope["source_account"], scope["start"], resource_id,
    )
    reader = ClientContextReader(
        sessions, role=role, client_sessions=service(),
        scope_registry={scope_ref: scope}, consent=consent, epoch=epoch, read=read_fn,
        clock=clock, auth_clock=auth_clock,
    )
    application = FastAPI()

    @application.exception_handler(RequestValidationError)
    async def invalid_request(_request, _error):
        return JSONResponse({"code": "CLIENT_CONTEXT_QUERY_INVALID"}, status_code=422)

    application.include_router(build_client_context_router(reader))
    return application


def ask_child(sessions, role, consent, epoch, scope, fixture):
    now = scope.pop("now")
    application = app(
        sessions, role, consent, epoch, scope, fixture["scope_ref"], fixture["resource_id"],
        lambda: now, lambda: datetime.fromisoformat(fixture["auth_now"]),
    )
    with TestClient(application) as client:
        response = client.post(
            QUERY_PATH, json={"question": fixture["question"], "scope_ref": fixture["scope_ref"]},
            headers={"Authorization": "Bearer " + fixture["bearer"]},
        )
        return {"status": response.status_code, "body": response.json()}


STAGED_PATH = "/api/v1/client/context/staged-evidence"


@dataclass(frozen=True, slots=True)
class EvidenceExcerpt:
    source_ref: str
    timeline_ref: str
    canonical_ref: str
    inbound_ref: str
    artifact_revision: int
    sender_ref: str | None
    sender_revision: int | None
    observed_at: str
    excerpt: str


def _resource_evidence(context: ContextValidity, resource_id: str) -> DetachedEvidence:
    """Bounded slice of text attachments, with the existing current-source checks."""
    session = context.session
    args = context.arguments
    message_ref = TimelineEventRow.event_ref["conversation_message_id"].as_string()
    events = session.scalars(
        select(TimelineEventRow)
        .join(ConversationMessageRow, ConversationMessageRow.id == message_ref)
        .where(
            TimelineEventRow.tenant_id == args["tenant_id"],
            TimelineEventRow.event_type == "ATTACHMENT_OBSERVED",
            TimelineEventRow.resource_id == resource_id,
            TimelineEventRow.event_ref["owner_binding_id"].as_string() == context.binding.id,
            ConversationMessageRow.tenant_id == args["tenant_id"],
            ConversationMessageRow.source_account == args["source_account"],
            ConversationMessageRow.message_type != "VOICE",
        )
        .order_by(TimelineEventRow.id).limit(33)
        .with_for_update(read=True, of=TimelineEventRow)
        .execution_options(populate_existing=True)
    ).all()
    if len(events) > 32:
        raise StagedReadRejected("STAGED_RESOURCE_SLICE_TOO_LARGE")
    messages = context.lock_permitted_source_rows(
        event.event_ref["conversation_message_id"] for event in events
    )
    by_id = {message.id: message for message in messages}
    locked = lock_conversation_dependencies(session, args["tenant_id"], messages)
    rows, versions = [], []
    for event in events:
        refs = event.event_ref
        message = by_id.get(refs["conversation_message_id"])
        if message is None or message.message_type == "VOICE" or not observation_event_is_current(
            session, event, binding_id=context.binding.id, source_account=args["source_account"],
            start=args["start"], now=args["now"], lock_sources=True, _source_rows=locked,
        ):
            continue
        excerpt = source_text(session, event, message)
        if not excerpt or excerpt != message.text:
            continue
        if len(excerpt) > 4096:
            raise StagedReadRejected("STAGED_EXCERPT_TOO_LARGE")
        receipt = locked[ArtifactReceiptRow][refs["artifact_receipt_id"]]
        rows.append(EvidenceExcerpt(
            message.id, event.id, event.canonical_event_id, refs["inbound_event_id"],
            refs["artifact_context_revision"], event.actor_id, refs["sender_binding_revision"],
            event.occurred_at.isoformat(), excerpt,
        ))
        versions.append(SourceVersion(
            message.id, str(message.context_revision), receipt.id, str(receipt.context_revision),
            sha256(excerpt.encode()).hexdigest(),
        ))
    return DetachedEvidence(stable_json([asdict(row) for row in rows]).encode(), tuple(versions))


def capture_evidence(context: ContextValidity, _epoch, _question: str, resource_id: str) -> DetachedEvidence:
    return _resource_evidence(context, resource_id)


def verify_evidence(
    context: ContextValidity, _epoch, evidence: DetachedEvidence, resource_id: str,
) -> None:
    # Compare the whole bounded candidate set, including losing candidates and
    # additions. The observation validator checks the referenced lineage rows.
    if _resource_evidence(context, resource_id) != evidence:
        raise StagedReadRejected("STAGED_SUPPORT_CHANGED")


def rank_evidence(evidence: DetachedEvidence, question: str) -> bytes:
    terms = set(re.findall(r"\w{3,}", question.casefold()))
    ranked = []
    for item in json.loads(evidence.content):
        row = EvidenceExcerpt(**item)
        score = len(terms.intersection(re.findall(r"\w{3,}", row.excerpt.casefold())))
        if score:
            ranked.append((score, row.observed_at, row.source_ref, row))
    ranked.sort(reverse=True)
    return stable_json({
        "evidence": [
            {"source_ref": row.source_ref, "observed_at": row.observed_at, "excerpt": row.excerpt[:512]}
            for _, _, _, row in ranked[:5]
        ],
    }).encode()


def staged_app(original, compute=rank_evidence):
    reader = StagedClientContextReader(
        original, capture_fn=capture_evidence, verify_fn=verify_evidence, compute_fn=compute,
    )
    return FastAPI(routes=[Route(STAGED_PATH, StagedContextEndpoint(reader), methods=["POST"])])
