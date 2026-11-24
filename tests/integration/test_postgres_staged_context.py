"""Exercise the opt-in ASGI slice with real ClientSession, PostgreSQL and LabConsent."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from secrets import token_hex
from threading import Event

import anyio
import pytest
from fastapi.testclient import TestClient

from andy_client_context import STAGED_PATH, rank_evidence, read, staged_app
from attention_router.application.client_context import ClientContextReader, ClientContextScope
from test_postgres_client_context import context_reader, enroll_device, issue  # noqa: F401
from test_postgres_client_session import NOW, service
from test_postgres_cognitive_lab import lab  # noqa: F401
from test_postgres_context_preactivation import enroll_role  # noqa: F401

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("lab", ["client-query"], indirect=True)
@pytest.mark.parametrize("withdrawal", ["consent_before", "postgres_before", "retention_before", "consent_during"])
def test_staged_client_session(Session, lab, context_reader, withdrawal):  # noqa: F811
    ids, clock, observe, _, change, pilot, scope, consent, no_effects, config = lab
    role, restricted = context_reader
    bearer = issue(Session, enroll_device(Session), NOW).session_token
    scope_ref = "ctx_" + token_hex(24)
    foreign_ref = "ctx_" + token_hex(24)
    owner_scope = ClientContextScope(
        scope["tenant_id"], scope["owner_human_identity_id"],
        scope["binding_id"], scope["source_account"], scope["start"], config()["resource_id"],
    )
    original = ClientContextReader(
        restricted,
        role=role, client_sessions=service(),
        scope_registry={
            scope_ref: owner_scope,
            foreign_ref: replace(owner_scope, tenant_id="other"),
        },
        consent=consent, epoch=pilot._epoch, read=read,
        clock=pilot._clock, auth_clock=lambda: NOW,
    )

    observe("plan")
    clock(day=2)
    observe("proposal")
    clock(day=3)
    observe("confirm")
    legacy = original.query(
        bearer=bearer, scope_ref=scope_ref, question="Em que data ficou a viagem?",
    )
    assert legacy["kind"] == "OBSERVATION"
    expected = sorted(legacy["evidence"], key=lambda row: row["observed_at"], reverse=True)
    assert {row["source_ref"] for row in expected} == {ids["proposal"], ids["confirm"]}

    def ask(client, ref=scope_ref):
        return client.post(
            STAGED_PATH, json={"scope_ref": ref, "question": "domingo"},
            headers={"Authorization": "Bearer " + bearer},
        )

    with TestClient(staged_app(original)) as client:
        for ref in (foreign_ref, "ctx_" + token_hex(24)):
            forged = ask(client, ref)
            assert forged.status_code == 503 and forged.content == b""
        accepted = ask(client)
    assert accepted.status_code == 200
    assert accepted.headers["cache-control"] == "no-store"
    assert accepted.json() == {"evidence": expected}

    if withdrawal != "consent_during":
        def compute(evidence, question):
            # Both operations use the writer paths and can commit after capture closes.
            if withdrawal == "consent_before":
                assert consent.revoke(ids["confirm"])
            elif withdrawal == "postgres_before":
                change(lambda context: context.revoke_message(ids["confirm"]))
            else:
                # No epoch or consent revision changes. Exact source revalidation
                # must still catch the expiry of the captured dependency set.
                clock(day=31)
            return rank_evidence(evidence, question)

        with TestClient(staged_app(original, compute)) as client:
            denied = ask(client)
        assert denied.status_code == 503 and denied.content == b""
    else:
        application = staged_app(original)
        contended = Event()
        ordering = []

        def revoke():
            with sqlite3.connect(consent.path, timeout=0) as connection:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    connection.execute("BEGIN IMMEDIATE")
            contended.set()
            assert consent.revoke(ids["confirm"])
            ordering.append("revocation_committed")

        with ThreadPoolExecutor(max_workers=1) as pool:
            writers = []

            async def instrumented(scope, receive, send):
                async def fenced_send(message):
                    if message.get("body"):
                        writer = pool.submit(revoke)
                        writers.append(writer)
                        assert await anyio.to_thread.run_sync(contended.wait, 1)
                        assert not writer.done() and ordering == []
                        await send(message)
                        ordering.append("body_send_returned")
                    else:
                        await send(message)

                await application(scope, receive, fenced_send)

            with TestClient(instrumented) as client:
                disclosed = ask(client)
            assert len(writers) == 1
            writers[0].result(timeout=5)

        assert disclosed.json() == {"evidence": expected}
        assert ordering == ["body_send_returned", "revocation_committed"]

    no_effects()
