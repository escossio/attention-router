"""Opt-in paired G7 benchmark on a disposable database. Invoke this file explicitly."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from datetime import timedelta
from hashlib import sha256
import json
from math import ceil
from pathlib import Path
from resource import RUSAGE_SELF, getrusage
from sqlite3 import Error as SQLiteError
from threading import Barrier, Event
from time import monotonic, process_time, sleep

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from attention_router.application import conversation_hybrid, conversation_vectors
from attention_router.application.client_context import ClientContextReader, ClientContextScope
from attention_router.application.context_validity import ContextValidity
from attention_router.application.conversation_evidence_staged import StagedConversationEvidenceReader
from attention_router.application.staged_read import StagedReadRejected
from attention_router.infrastructure.models import ConversationMessageRow
from attention_router.observability.private_context import query_trace
from attention_router.web.private_context import owner_answer
from test_passive_vectors import provider  # noqa: F401
from test_postgres_client_context import context_reader, enroll_device, issue  # noqa: F401
from test_postgres_client_session import NOW, service
from test_postgres_cognitive_lab import enroll_role, lab  # noqa: F401
from test_postgres_conversation_evidence import evidence_world  # noqa: F401


pytestmark = pytest.mark.postgres
DATA = Path(__file__).resolve().parents[2] / "evals/governed-retrieval/cases.json"
TOP_K = 3


def digest(value):
    return sha256(value.encode()).hexdigest()


def _burst(fetch, cases, engine, Session, marker_id, arm):
    gate = Barrier(len(cases) + 1, timeout=30)
    has_reader = Event()

    def observing(connection, cursor, statement, parameters, context, executemany):
        if "FROM conversation_vector_segments g JOIN conversation_embeddings" in statement:
            has_reader.set()

    def query(index, case):
        key, category, question, expected = case
        gate.wait()
        start = monotonic()
        diagnostics = {}
        try:
            candidates, delivered = fetch(index, question)
            status = "ok"
        except DBAPIError as exc:
            code = getattr(exc.orig, "sqlstate", None)
            if code not in ("55P03", "57014"):
                raise
            candidates, delivered, status = [], [], code
        except (ValueError, StagedReadRejected) as exc:
            # Unknown errors are harness or product failures and must surface.
            if str(exc) not in ("CONTEXT_CONSENT_UNAVAILABLE", "STAGED_CONVERSATION_SUPPORT_CHANGED",
                                "CONTEXT_EPOCH_ANCHOR_UNAVAILABLE", "CONTEXT_INDEX_FENCE_BUSY"):
                raise
            candidates, delivered, status = [], [], str(exc)
            if status == "CONTEXT_CONSENT_UNAVAILABLE" and isinstance(exc.__cause__, SQLiteError):
                diagnostics["sqlite_error_code"] = getattr(exc.__cause__, "sqlite_errorcode", None)
            if status == "STAGED_CONVERSATION_SUPPORT_CHANGED":
                diagnostics["changed_components"] = [
                    note.removeprefix("g7_support_component=")
                    for note in getattr(exc, "__notes__", ())
                    if note.startswith("g7_support_component=")
                ]
        candidate_refs = list(dict.fromkeys(entry[0] for entry in candidates))
        found = list(dict.fromkeys(entry[0] for entry in delivered))
        hits = set(found).intersection(expected)
        return {
            "case": key, "category": category, "status": status, "milliseconds": (monotonic()-start)*1000,
            "diagnostics": diagnostics,
            "expected": expected, "candidates": candidate_refs, "evidence": found, "hits": len(hits),
            "complete_support": status == "ok" and (bool(expected) and hits == set(expected) or not expected and not found),
        }

    def writer():
        gate.wait()
        if not has_reader.wait(30):
            return {"attempted": False, "committed": False}
        start, attempts = monotonic(), 0
        while monotonic() - start < 15:
            attempts += 1
            try:
                with Session.begin() as db:
                    db.execute(text("SET LOCAL lock_timeout='250ms'"))
                    # A metadata-only update exercises the writer fence without changing judged content.
                    db.execute(text("""UPDATE conversation_messages
                        SET metadata=metadata::jsonb || jsonb_build_object('benchmark_arm', CAST(:arm AS text))
                        WHERE id=:id"""), {"arm": arm, "id": marker_id})
                return {"attempted": True, "committed": True, "attempts": attempts, "milliseconds": (monotonic()-start)*1000}
            except DBAPIError as exc:
                if getattr(exc.orig, "sqlstate", None) != "55P03":
                    raise
                sleep(0.025)
        return {"attempted": True, "committed": False, "attempts": attempts, "milliseconds": (monotonic()-start)*1000}

    before_cpu = process_time()
    start = monotonic()
    event.listen(engine, "before_cursor_execute", observing)
    try:
        with ThreadPoolExecutor(max_workers=len(cases) + 1) as pool:
            write = pool.submit(writer)
            requests = [pool.submit(query, n, case) for n, case in enumerate(cases)]
            rows = [task.result() for task in requests]
            progress = write.result()
    finally:
        event.remove(engine, "before_cursor_execute", observing)
    times = sorted(row["milliseconds"] for row in rows)
    answered = [row for row in rows if row["status"] == "ok"]
    references = sum(len(row["evidence"]) for row in answered)
    candidate_count = sum(len(row["candidates"]) for row in answered)
    candidate_hits = sum(len(set(row["expected"]).intersection(row["candidates"])) for row in answered)
    return {
        "requests": rows, "offered": len(rows), "answered": len(answered), "writer": progress,
        "candidate_precision_at_3": candidate_hits/candidate_count if candidate_count else None,
        "precision_at_3": sum(row["hits"] for row in answered)/references if references else None,
        "wall_seconds": monotonic()-start, "process_cpu_seconds": process_time()-before_cpu,
        "process_peak_rss_kib_cumulative": getrusage(RUSAGE_SELF).ru_maxrss,
        "all_attempt_latency_ms": {f"p{p}": times[ceil(p*len(times)/100)-1] for p in (50, 95, 99)},
    }


@pytest.mark.parametrize("lab", ["client-query"], indirect=True)
def test_governed_paired_readers_and_writer(Session, evidence_world, context_reader, lab, provider, capsys):  # noqa: F811
    fixture = json.loads(DATA.read_text())
    sources, cases = fixture["sources"], fixture["queries"]
    assert len(cases) == 16 and len({q[0] for q in cases}) == 16
    assert all(set(q[3]) <= sources.keys() for q in cases)
    seed, index, wire, labels, receipts, scope, pilot, engine, read_time = evidence_world
    scope_ref = "ctx_" + "0"*32
    sources = dict(sources)
    scope_id = ClientContextScope(scope["tenant_id"], scope["owner_human_identity_id"],
                                 scope["binding_id"], scope["source_account"], scope["start"], lab[-1]()["resource_id"])
    ids = {name: seed(name, body) for name, body in sources.items()}
    for n in range(200-len(sources)):
        ids[f"d{n:03d}"] = seed(f"d{n:03d}", f"A remessa de etiquetas do arquivo geral recebeu o protocolo {n}.")
        if n == 80:
            index(provider)
    forbidden = {"other-account", "other-tenant"}
    seed("other-account", "O código do cofre de safira é nove.", source_account="another-account")
    seed("other-tenant", "A altitude do planador de jade é novecentos metros.", source_tenant="other")
    index(provider)
    with Session.begin() as db:
        conversation_vectors.scope(db, scope_id.tenant_id, scope_id.source_account)
        assert db.scalar(text("""SELECT count(*) FROM conversation_vector_segments WHERE tenant_id=:tenant
            AND source_account=:account AND state='READY'"""), {"tenant": scope_id.tenant_id, "account": scope_id.source_account}) == 200

    role, sessions = context_reader
    _, consent = wire(None)
    def provenance(ref, value):
        return labels[ref], digest(value)

    def baseline(context, _epoch, question, _resource):
        results = conversation_hybrid.search(context.session, query=question, tenant_id=scope_id.tenant_id,
            source_account=scope_id.source_account, provider=provider, limit=TOP_K)["results"]
        found = []
        for item in results:
            members = item["messages"]
            refs = {m["id"] for m in members}
            if refs == context._permitted_sources(refs, required_layers=("embedding",)):
                found.extend(provenance(m["id"], m["text"]) for m in members)
        return found

    candidates_at_render = ContextVar("g7_packet_candidates")

    def render(packet):
        candidates_at_render.set([
            provenance(m["source_ref"], m["text"]) for item in packet.items for m in item.attributes["excerpts"]
        ])
        return owner_answer(packet)

    started = monotonic()
    boundary = ClientContextReader(sessions, role=role, client_sessions=service(), scope_registry={scope_ref: scope_id},
        epoch=pilot._epoch, consent=consent, read=baseline, clock=lambda: read_time+timedelta(seconds=monotonic()-started),
        auth_clock=lambda: NOW+timedelta(seconds=monotonic()-started))
    governed = StagedConversationEvidenceReader(boundary, reserve_query=lambda _scope: True, provider=provider,
                                               item_budget=TOP_K, render_packet=render)
    devices = [issue(Session, enroll_device(Session), NOW, tenant_id=scope_id.tenant_id) for _ in cases]

    def raw(slot, question):
        delivered = boundary.query(bearer=devices[slot].session_token, scope_ref=scope_ref, question=question)
        return delivered, delivered

    def staged(slot, question):
        bodies = []
        token = candidates_at_render.set(None)
        try:
            with query_trace() as correlation:
                receipt = governed.query_and_deliver(
                    bearer=devices[slot].session_token, scope_ref=scope_ref, question=question, deliver=bodies.append,
                )
            assert len(bodies) == 1 and receipt.body_size == len(bodies[0])
            answer = json.loads(bodies[0])
            assert answer["correlation_id"] == correlation
            assert answer["kind"] == ("OBSERVATION" if answer["evidence"] else "INSUFFICIENT_EVIDENCE")
            delivered = [provenance(row["source_ref"], row["excerpt"]) for row in answer["evidence"]]
            return candidates_at_render.get(), delivered
        finally:
            candidates_at_render.reset(token)

    runs = (("hybrid", raw), ("governed", staged))
    for _, fetch in runs:
        for ref in ("s11", "s12"):
            _, delivered = fetch(0, sources[ref])
            assert (ref, digest(sources[ref])) in delivered
    report = {arm: _burst(fetch, cases, engine, Session, ids["d000"], arm) for arm, fetch in runs}

    serial_ids = {"q03", "q04", "q06", "q07", "q09", "q10", "q12", "q16"}
    serial = {}
    for slot, (case, _, question, expected) in enumerate(cases):
        if case not in serial_ids:
            continue
        candidates, delivered = staged(slot, question)
        serial[case] = sorted({ref for ref, _ in delivered})
        assert not forbidden.intersection(ref for ref, _ in candidates + delivered)
        assert serial[case] == sorted(expected), case
    report["governed"]["focused_serial"] = serial

    corrected = "A caixa de amostras de quartzo chegou na terça-feira."
    with Session.begin() as db:
        db.get(ConversationMessageRow, ids["s11"]).text = corrected
    index(provider)
    with consent.current() as (_, permits):
        with Session.begin() as db:
            validity = ContextValidity(db, **scope, now=read_time, source_allowed=permits)
            pilot._epoch.advance(validity, validity.revoke_receipt, receipts["s12"])
    for arm, fetch in runs:
        _, current = fetch(0, corrected)
        _, withdrawn = fetch(0, sources["s12"])
        report[arm]["correction"] = ("s11", digest(corrected)) in current and ("s11", digest(sources["s11"])) not in current
        report[arm]["withdrawal"] = all(row[0] != "s12" for row in withdrawn)
        scoped = [fetch(0, question) for question in (
            "O código do cofre de safira é nove.", "A altitude do planador de jade é novecentos metros.",
        )]
        report[arm]["scope_isolated"] = (
            all(forbidden.isdisjoint(row["candidates"] + row["evidence"]) for row in report[arm]["requests"])
            and all(forbidden.isdisjoint(ref for ref, _ in candidates + delivered) for candidates, delivered in scoped)
        )
    with capsys.disabled():
        print("G7_PAIRED " + json.dumps(report, sort_keys=True), flush=True)
    assert all(result["correction"] and result["withdrawal"] and result["scope_isolated"] for result in report.values())
