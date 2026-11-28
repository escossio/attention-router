"""Offline parity against the governed reader with a synthetic index boundary."""

from collections import defaultdict
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from attention_router.application import conversation_evidence as control
from attention_router.application import conversation_evidence_staged as staged
from attention_router.application.context_compiler import compile_evidence_candidates
from attention_router.application.conversation_segments import segment_messages
from attention_router.application.staged_read import StagedReadRejected
from attention_router.infrastructure.artifact_models import ArtifactReceiptRow
from attention_router.infrastructure.models import ConversationMessageRow
from attention_router.observability.private_context import query_trace
from attention_router.web.private_context import owner_answer


NOW = datetime(2026, 11, 23, tzinfo=UTC)


class Provider:
    model = "synthetic"
    revision = "fixed"
    dimensions = 384

    def __init__(self, state):
        self.state = state

    def encode(self, texts, *, query=False):
        assert query and len(texts) == 1
        self.state["encodes"] += 1
        return [[1.0] + [0.0] * 383]

    def count(self, text):
        return len(text.split())

    def fragments(self, text):
        return [(0, len(text))]


@pytest.fixture
def world(monkeypatch, request):
    state = dict(active=False, fenced=False, encodes=0, revision="a", epoch=0, allowed=True, expire=False)
    provider = Provider(state)
    context = SimpleNamespace(
        scope="scope-binding",
        arguments=dict(
            tenant_id="t", source_account="a", owner_human_identity_id="owner",
            start=NOW - timedelta(days=1), now=NOW,
        ),
        session=object(), _source_allowed=lambda _msg, **args: args["now"] <= NOW,
    )
    messages = [
        dict(id="m1", conversation_id="one", index_text="Bia confirmou a reunião às 15 horas."),
        dict(id="m2", conversation_id="two", index_text="A reunião ficou marcada para 16 horas."),
    ]
    if hasattr(request, "param"):
        for msg, excerpt in zip(messages, request.param, strict=True):
            msg.update(conversation_id="one", index_text=excerpt)
    receipts = {}
    message_rows = {}
    rows = []
    memberships = {}
    conversations = defaultdict(list)
    for msg in messages:
        receipt_id = "receipt-" + msg["id"]
        msg.update(
            tenant_id="t", source_account="a", source="wwebjs", source_message_id=msg["id"],
            sender_actor_id="bia", external_sender_key=None, from_me=False, sent_at=NOW,
            reply_to_message_id=None, source_fingerprint="hash-" + msg["id"],
            context_revision="first", transcription_id=None, metadata={"artifact_receipt_id": receipt_id},
        )
        message_rows[msg["id"]] = ConversationMessageRow(id=msg["id"], message_type="TEXT")
        receipts[receipt_id] = ArtifactReceiptRow(id=receipt_id, context_revision="first", metadata_json={})
        conversations[msg["conversation_id"]].append(msg)
    for conversation in conversations.values():
        plan, = segment_messages(conversation, provider)
        rows.append(dict(
            id=plan["id"], similarity=0.8, content_text=plan["text"], content_hash=plan["content_hash"],
            source_hash=plan["source_hash"], indexed_embedding="[1,0]", segment_generation=1,
        ))
        memberships[plan["id"]] = [
            (member["message"]["conversation_id"], member["message"]["id"], ordinal,
             member["start"], member["end"], member["role"])
            for ordinal, member in enumerate(plan["members"])
        ]
    rows.sort(key=lambda row: row["id"])

    @contextmanager
    def transaction():
        assert not state["active"]
        state["active"] = True
        try:
            yield context.session
        finally:
            state.update(active=False, fenced=False)

    @contextmanager
    def consent(revision=None):
        if revision is not None and revision != state["revision"]:
            raise StagedReadRejected("consent")
        yield state["revision"], context._source_allowed

    def authenticate(_session, bearer, _scope):
        assert state["active"]
        if bearer != "owner" or not state["allowed"]:
            raise StagedReadRejected("owner")

    def authorize(_self, session, bearer, scope, _permits):
        authenticate(session, bearer, scope)
        return context

    def fence(_context):
        assert state["active"]
        state["fenced"] = True

    def pool(_session, _provider, _vector, *, include_versions=False, **_options):
        assert state["active"]
        if include_versions:
            assert state["fenced"]
        return deepcopy(rows)

    def sources(_context, _memberships, _filters, **_options):
        assert state["active"]
        authorized = defaultdict(list)
        for msg in messages:
            if not (state["expire"] and msg["id"] == "m1"):
                authorized[msg["conversation_id"]].append(msg)
        return authorized, receipts, {ArtifactReceiptRow: receipts, ConversationMessageRow: message_rows}

    monkeypatch.setattr(control.settings, "app_env", "test")
    @contextmanager
    def authorize_transaction(_self, bearer, scope, expected_revision=None):
        with consent(expected_revision) as (revision, permits), transaction() as session:
            authenticate(session, bearer, scope)
            yield revision, context, permits

    monkeypatch.setattr(staged.StagedClientContextAuthority, "_transaction", authorize_transaction)
    monkeypatch.setattr(staged, "fence_index", fence)
    monkeypatch.setattr(control.vectors, "scope", lambda *_: None)
    monkeypatch.setattr(control.vectors, "authorized", lambda *_: True)
    monkeypatch.setattr(control, "_pool", pool)
    monkeypatch.setattr(control, "_memberships", lambda *_: memberships)
    monkeypatch.setattr(control, "_read_sources", sources)
    original = SimpleNamespace(
        _sessions=SimpleNamespace(begin=transaction), _consent=SimpleNamespace(current=consent),
        _epoch=SimpleNamespace(token=lambda _: state["epoch"]), _scopes={"scope": SimpleNamespace(resource_id="r")},
        _require_owner=authenticate, _clock=lambda: state.get("delivery_time", NOW),
    )
    return SimpleNamespace(
        state=state, provider=provider, original=original, transaction=transaction,
        context=context, rows=rows, memberships=memberships, messages=messages, receipts=receipts,
    )


def reader(world, reserve=lambda _scope: True):
    return staged.StagedConversationEvidenceReader(
        world.original, provider=world.provider, reserve_query=reserve,
    )


@pytest.mark.parametrize("question", ["Em que horário ficou a reunião?", "Ela confirmou isso?"])
def test_staged_exact_control_parity_and_determinism(world, monkeypatch, question):
    with world.transaction():
        candidates = control.retrieve_evidence_candidates(
            world.context, world.original._epoch, question, provider=world.provider,
        )
    assert len(candidates) == 2
    expected = compile_evidence_candidates("t", question, candidates, now=NOW).internal_payload()
    encode = world.provider.encode

    def guarded_encode(*args, **kwargs):
        assert not world.state["active"]
        return encode(*args, **kwargs)

    monkeypatch.setattr(world.provider, "encode", guarded_encode)
    for name in ("segment_messages", "order_candidates"):
        original = getattr(control, name)

        def guarded(*args, operation=original, **kwargs):
            assert not world.state["active"]
            return operation(*args, **kwargs)

        monkeypatch.setattr(control, name, guarded)

    def deliver(body):
        assert world.state["active"] and world.state["fenced"]
        bodies.append(body)

    bodies = []
    boundary = reader(world)
    for _ in range(2):
        receipt = boundary.query_and_deliver(bearer="owner", scope_ref="scope", question=question, deliver=deliver)
        assert receipt.body_sha256 == sha256(bodies[-1]).hexdigest()
        assert receipt.body_size == len(bodies[-1])
    assert bodies[0] == bodies[1] == staged._encode(expected)
    assert world.state["encodes"] == 3


@pytest.mark.parametrize("change", [
    "added", "removed", "membership", "embedding", "index_text", "source", "receipt",
    "receipt_metadata", "expired", "expiry_at_delivery", "consent", "epoch", "owner", "model", "policy",
])
def test_abort_after_compute_on_any_changed_support(world, monkeypatch, change):
    original = staged._compile

    def compile_then_change(*args, **kwargs):
        assert not world.state["active"]
        answer = original(*args, **kwargs)
        msg = world.messages[0]
        receipt = world.receipts[msg["metadata"]["artifact_receipt_id"]]
        updates = {
            "added": lambda: world.rows.append(dict(world.rows[0], id="new")),
            "removed": lambda: world.rows.pop(),
            "membership": lambda: world.memberships[world.rows[0]["id"]].clear(),
            "embedding": lambda: world.rows[0].update(indexed_embedding="[0,1]"),
            "index_text": lambda: world.rows[0].update(content_text="different"),
            "source": lambda: msg.update(context_revision="second"),
            "receipt": lambda: setattr(receipt, "context_revision", "second"),
            "receipt_metadata": lambda: receipt.metadata_json.update(context_evidence_withdrawn=True),
            "expired": lambda: world.state.update(expire=True),
            "expiry_at_delivery": lambda: world.state.update(delivery_time=NOW + timedelta(seconds=1)),
            "consent": lambda: world.state.update(revision="b"),
            "epoch": lambda: world.state.update(epoch=1),
            "owner": lambda: world.state.update(allowed=False),
            "model": lambda: setattr(world.provider, "revision", "other"),
            "policy": lambda: monkeypatch.setattr(control, "load_policy", lambda: {"version": "other"}),
        }
        updates[change]()
        return answer

    monkeypatch.setattr(staged, "_compile", compile_then_change)
    with pytest.raises(StagedReadRejected):
        reader(world).query_and_deliver(
            bearer="owner", scope_ref="scope", question="Qual horário?", deliver=lambda _: pytest.fail("disclosed"),
        )


@pytest.mark.parametrize("bearer, reserved, calls", [("unknown", True, 0), ("owner", False, 1)])
def test_disallowed_preparation_does_not_encode(world, bearer, reserved, calls):
    reservations = []

    def reserve(scope):
        assert not world.state["active"]
        reservations.append(scope)
        return reserved

    with pytest.raises(StagedReadRejected):
        reader(world, reserve).query_and_deliver(
            bearer=bearer, scope_ref="scope", question="Qual horário?", deliver=lambda _: pytest.fail("disclosed"),
        )
    assert len(reservations) == calls and world.state["encodes"] == 0


def test_detached_index_tampering_cannot_supply_an_excerpt(world, monkeypatch):
    world.rows[0]["content_text"] = "PRIVATE INDEX TEXT"
    compile_snapshot = staged._compile

    def compute(snapshot, *args, **kwargs):
        assert b"PRIVATE INDEX TEXT" not in snapshot.content
        return compile_snapshot(snapshot, *args, **kwargs)

    monkeypatch.setattr(staged, "_compile", compute)
    bodies = []
    reader(world).query_and_deliver(bearer="owner", scope_ref="scope", question="reunião", deliver=bodies.append)
    assert json.loads(bodies[0])["candidate_count"] == 1
    assert world.rows[0]["id"] not in bodies[0].decode()


@pytest.mark.parametrize(("world", "expected_sources"), [
    pytest.param(
        ("A inspeção do moinho ficou para quinta-feira.", "A caldeira foi desligada na quarta-feira."),
        ["m1"], id="independent-neighbor",
    ),
    pytest.param(
        ("A inspeção do moinho ficou para quinta-feira.", "A inspeção do moinho ficou para sexta-feira."),
        ["m1", "m2"], id="two-supported-observations",
    ),
    pytest.param(
        ("A inspeção do moinho ficou para quinta-feira?", "A caldeira foi desligada na quarta-feira."),
        [], id="question-and-unrelated-time",
    ),
], indirect=["world"])
def test_staged_temporal_answer_cites_only_supported_excerpts(world, expected_sources):
    boundary = staged.StagedConversationEvidenceReader(
        world.original, provider=world.provider, reserve_query=lambda _scope: True,
        render_packet=owner_answer,
    )
    bodies = []

    def deliver(body):
        assert world.state["active"] and world.state["fenced"]
        bodies.append(body)

    with query_trace() as trace:
        receipt = boundary.query_and_deliver(
            bearer="owner", scope_ref="scope", question="Quando ficou a inspeção do moinho?",
            deliver=deliver,
        )

    body, = bodies
    assert receipt.body_sha256 == sha256(body).hexdigest()
    answer = json.loads(body)
    assert answer["correlation_id"] == trace
    assert answer["kind"] == ("OBSERVATION" if expected_sources else "INSUFFICIENT_EVIDENCE")
    assert answer["conclusion"] is None and answer["date"] is None
    assert [item["source_ref"] for item in answer["evidence"]] == expected_sources
    assert [item["excerpt"] for item in answer["evidence"]] == [
        msg["index_text"] for msg in world.messages if msg["id"] in expected_sources
    ]
