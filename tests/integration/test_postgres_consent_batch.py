from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import event, inspect, select

from attention_router.application.client_context_staged import _authorized_context, _prepare_transaction
from attention_router.application.context_pg_consent import PgSourceConsent
from attention_router.application.conversation_evidence_staged import _check_expiry
from attention_router.application.staged_read import DetachedEvidence, SourceVersion, StagedReadRejected
from attention_router.infrastructure.artifact_models import ArtifactReceiptRow
from attention_router.infrastructure.context_consent_models import ContextSourceConsentRow
from attention_router.infrastructure.models import ConversationMessageRow
from test_postgres_source_consent import consent_clients, context_reader, lab  # noqa: F401
from test_postgres_context_preactivation import enroll_role  # noqa: F401

pytestmark = [pytest.mark.postgres, pytest.mark.parametrize("lab", ["client-query"], indirect=True)]


@contextmanager
def measure(db, results, phase):
    conn = db.connection()
    statements = []

    def capture(conn, cursor, sql, parameters, context, executemany):
        if sql.lstrip().upper().startswith("SELECT"):
            statements.append(sql)

    event.listen(conn, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(conn, "before_cursor_execute", capture)
        results[phase] = len(statements)


def fields(row):
    return {prop.key: getattr(row, prop.key) for prop in inspect(type(row)).column_attrs}


@pytest.mark.parametrize("size", [32, 257])
def test_batch_contract_and_phase_counts(Session, consent_clients, size):  # noqa: F811
    args, writer, staged, decision, _ = consent_clients
    template = decision("confirm")
    assert writer.grant(**args, **template)
    authority = staged()._pipeline._authority
    reader = authority._reader
    scope = reader._scopes["owner"]
    now = reader._clock()
    valid = []
    invalid = []
    receipt_ids = {}
    engine = PgSourceConsent()

    pending_grants = []
    with Session.begin() as db:
        base_message = db.get(ConversationMessageRow, template["message_id"])
        base_receipt = db.get(ArtifactReceiptRow, template["receipt_id"])
        base_grant = db.scalar(select(ContextSourceConsentRow))
        for index in range(size + (6 if size > 32 else 0)):
            message_id = f"fixture-batch-message-{index:04d}"
            receipt_id = f"fixture-batch-receipt-{size + 6 - index:04d}"
            receipt_ids[message_id] = receipt_id
            metadata = base_message.metadata_json | {"artifact_receipt_id": receipt_id}
            message = fields(base_message) | dict(
                id=message_id, source_message_id=message_id, metadata_json=metadata, message_type="TEXT"
            )
            receipt = fields(base_receipt) | dict(id=receipt_id, external_receipt_id=receipt_id)
            grant = fields(base_grant) | dict(message_id=message_id, receipt_id=receipt_id)
            state = index - size
            if state == 0:
                message["text"] = "corrected synthetic source"
            elif state == 1:
                grant["revoked_at"] = now
            elif state == 2:
                grant["expires_at"] = now
            elif state == 5:
                message["message_type"] = "VOICE"
                grant["retention"] = grant["retention"] | {"transcription": now.isoformat()}
            db.add(ConversationMessageRow(**message))
            if state != 3:
                db.add(ArtifactReceiptRow(**receipt))
            if state != 4:
                pending_grants.append(grant)
            (valid if state < 0 else invalid).append(message_id)

        db.flush()
        messages = {row.id: row for row in db.scalars(select(ConversationMessageRow).where(
            ConversationMessageRow.id.in_([*valid, *invalid])
        ))}
        receipts = {row.id: row for row in db.scalars(select(ArtifactReceiptRow).where(
            ArtifactReceiptRow.id.in_(receipt_ids.values())
        ))}
        for grant in pending_grants:
            grant["message_revision"] = messages[grant["message_id"]].context_revision
            if receipt := receipts.get(grant["receipt_id"]):
                grant["receipt_revision"] = receipt.context_revision
            db.add(ContextSourceConsentRow(**grant))

    results = {}
    missing = "fixture-batch-missing"
    foreign = [decision(name)["message_id"] for name in ("other_tenant", "other_account")]

    for batch in (False, True):
        with reader._sessions() as db:
            with db.begin():
                _prepare_transaction(reader, db, args["bearer"], scope, pg_consent=True)
                _, permit = engine.current(db, scope, batch=batch)
                context = _authorized_context(
                    reader, db, args["bearer"], scope, permit,
                    source_allowed_many=getattr(permit, "filter_rows", None),
                )
                with measure(db, results, "filtragem") as statements:
                    rows = context.lock_permitted_source_rows(
                        [*reversed(valid), *invalid, *foreign, missing, valid[0]],
                        required_layers=("embedding",),
                    )
                expected = [*valid, *invalid[-1:]]
                assert [row.id for row in rows] == expected
                if batch:
                    for table in ("context_source_consents", "artifact_receipts"):
                        queries = [sql for sql in statements if f"FROM {table}" in sql]
                        assert len(queries) == (1 if size == 32 else 2)
                        assert all(sql.count("%(") <= 260 for sql in queries)

                selected = rows[:size]
                receipts = {ref: db.get(ArtifactReceiptRow, receipt_ids[ref]) for ref in valid}
                versions = tuple(SourceVersion(
                    row.id, row.context_revision, receipts[row.id].id,
                    receipts[row.id].context_revision, "synthetic-fingerprint"
                ) for row in selected)
                locked = {
                    ConversationMessageRow: {row.id: row for row in rows},
                    ArtifactReceiptRow: {receipt.id: receipt for receipt in receipts.values()},
                }
                snapshot = DetachedEvidence(b"{}", versions)
                with measure(db, results, "verificacao"):
                    engine.verify_sources(context, scope, snapshot, permit, clock=reader._clock)
                with measure(db, results, "revalidacao_final"):
                    _check_expiry(context, snapshot, locked, now)
                if invalid:
                    voice = rows[-1]
                    receipt = db.get(ArtifactReceiptRow, receipt_ids[voice.id])
                    locked[ArtifactReceiptRow][receipt.id] = receipt
                    expiry = SimpleNamespace(sources=(SimpleNamespace(
                        source_ref=voice.id, receipt_ref=receipt.id,
                    ),))
                    with pytest.raises(StagedReadRejected, match="SUPPORT_EXPIRED"):
                        _check_expiry(context, expiry, locked, now)
            with db.begin(), pytest.raises(ValueError, match="TRANSACTION_ENDED"):
                if batch:
                    permit.filter_rows((), owner_id=scope.owner_id, now=now)
                else:
                    permit(rows[0], owner_id=scope.owner_id, now=now)
        print(f"SQL_COUNTS size={size} batch={batch} {results}", flush=True)
        if batch:
            # Bound all three measured intervals. An optimization may lower
            # the counts, but a per-source SQL regression must fail the test.
            limits = {
                32: {"filtragem": 4, "verificacao": 6, "revalidacao_final": 2},
                257: {"filtragem": 6, "verificacao": 11, "revalidacao_final": 4},
            }[size]
            assert set(results) == set(limits)
            assert all(0 < results[phase] <= ceiling for phase, ceiling in limits.items())
