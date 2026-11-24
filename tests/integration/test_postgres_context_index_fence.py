"""Tenant-fence contract in the disposable PostgreSQL fixture."""

from types import SimpleNamespace

from andy_client_context import enroll_fixture_fence
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from attention_router.application.context_index_fence import fence_index
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from test_postgres_client_context import context_reader  # noqa: F401
from test_postgres_cognitive_lab import lab  # noqa: F401
from test_postgres_context_preactivation import enroll_role  # noqa: F401
from test_postgres_source_consent import consent_clients  # noqa: F401

pytestmark = pytest.mark.postgres


def _shared(db, tenant=DEFAULT_TENANT_ID):
    fence_index(SimpleNamespace(session=db, arguments={"tenant_id": tenant}))


@pytest.mark.parametrize("lab", ["client-query"], indirect=True)
def test_source_and_enqueue_writers_conflict_by_tenant(Session, consent_clients):  # noqa: F811
    _, _, _, decision, _ = consent_clients
    first = decision("confirm")
    second = decision("other_tenant")
    update = text("UPDATE artifact_receipts SET metadata = metadata WHERE id = :id")
    enqueue = text("SELECT enqueue_conversation_vector(:id)")
    conversation = {"id": first["conversation_id"]}
    receipt = {"id": first["receipt_id"]}

    with Session.begin() as reader:
        _shared(reader)
        with Session.begin() as another_reader:
            _shared(another_reader)
        with Session.begin() as independent:
            assert independent.execute(update, {"id": second["receipt_id"]}).rowcount == 1
        for statement, params in ((update, receipt), (enqueue, conversation)):
            with pytest.raises(DBAPIError, match="CONTEXT_INDEX_FENCE_BUSY") as refused:
                with Session.begin() as writer:
                    writer.execute(statement, params)
            assert refused.value.orig.sqlstate == "55P03"

    with Session.begin() as writer:
        assert writer.execute(update, receipt).rowcount == 1
        with pytest.raises(DBAPIError, match="CONTEXT_INDEX_FENCE_BUSY"):
            with Session.begin() as reader:
                _shared(reader)
        # Unenrolled tenants cannot open protected reads. Their ordinary
        # writers remain unaffected by another tenant's index fence.
        with pytest.raises(DBAPIError, match="CONTEXT_INDEX_FENCE_TENANT_NOT_ENROLLED"):
            with Session.begin() as independent:
                _shared(independent, "other")
        with Session.begin() as independent:
            assert independent.execute(update, {"id": second["receipt_id"]}).rowcount == 1

    with Session.begin() as reader:
        _shared(reader)


def test_reader_refuses_incomplete_coverage_and_truncate(Session, request):
    enroll_fixture_fence(Session, request)
    with pytest.raises(DBAPIError, match="CONTEXT_INDEX_FENCE_TRUNCATE_DENIED"):
        with Session.begin() as db:
            db.execute(text("TRUNCATE public.conversation_vector_memberships"))
    with pytest.raises(ValueError, match="CONTEXT_INDEX_FENCE_INCOMPLETE"):
        with Session.begin() as db:
            db.execute(text("ALTER TABLE public.artifact_receipts DISABLE TRIGGER andy_context_index_row"))
            _shared(db)
    with Session.begin() as db:
        _shared(db)


@pytest.mark.parametrize("lab", ["client-query"], indirect=True)
def test_vector_worker_requeues_fence_contention_without_poisoning(Session, consent_clients):  # noqa: F811
    """Contention must not strand a claimed worker job for its 180s lease."""
    import json
    import time

    from attention_router.application import conversation_vectors as vectors
    from attention_router.infrastructure.models import QueueRow

    _, writer, _, decision, _ = consent_clients
    scope = writer._reader._scopes["owner"]
    item = decision("confirm")
    queue_id = "vector:" + item["conversation_id"]
    payload = {
        "tenant_id": scope.tenant_id,
        "source_account": scope.source_account,
        "conversation_id": item["conversation_id"],
        "generation": 7,
        "next_at": 0,
        "attempts": 0,
    }
    with Session.begin() as setup:
        setup.execute(text("DELETE FROM queue WHERE kind=:kind"), {"kind": vectors.KIND})
        setup.execute(text(
            "INSERT INTO queue(id,kind,status,payload,created_at) "
            "VALUES(:id,:kind,'PENDING',CAST(:payload AS jsonb),now())"
        ), {"id": queue_id, "kind": vectors.KIND, "payload": json.dumps(payload)})

    class NeverEncode:
        dimensions = 384

        def encode(self, *args, **kwargs):
            pytest.fail("embedding inference must not run when fenced")

    with Session.begin() as reader:
        _shared(reader, scope.tenant_id)
        assert vectors.cycle(Session, NeverEncode()) == 0

    with Session() as check:
        job = check.get(QueueRow, queue_id)
        assert job.status == "PENDING"
        assert job.payload["generation"] == 7
        assert job.payload["attempts"] == 0
        assert job.payload["lease_until"] == 0
        assert 0 < job.payload["next_at"] - time.time() <= 1.5

def test_no_enrollment_preserves_legacy_writes_and_truncate(Session, request):
    enroll_fixture_fence(Session, request)
    with Session.begin() as admin:
        assert admin.execute(text(
            "UPDATE public.andy_context_fenced_tenants SET enabled=false WHERE enabled=true"
        )).rowcount == 1
    with pytest.raises(DBAPIError, match="CONTEXT_INDEX_FENCE_TENANT_NOT_ENROLLED"):
        with Session.begin() as reader:
            _shared(reader)
    # Global TRUNCATE is permitted only when no protected tenant exists.
    with Session.begin() as writer:
        writer.execute(text("TRUNCATE public.queue"))
    with Session.begin() as admin:
        admin.execute(text(
            "UPDATE public.andy_context_fenced_tenants SET enabled=true "
            "WHERE tenant_id=:id"
        ), {"id": DEFAULT_TENANT_ID})
    with pytest.raises(DBAPIError, match="CONTEXT_INDEX_FENCE_TRUNCATE_DENIED"):
        with Session.begin() as writer:
            writer.execute(text("TRUNCATE public.queue"))
