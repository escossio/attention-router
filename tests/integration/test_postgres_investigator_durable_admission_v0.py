"""Disposable PostgreSQL-only proof of atomic seed, nonce and initial audit.

No test here connects to the operator's database. The pg_url fixture creates a
throwaway database via tests/integration/conftest.py, then removes it.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
import hmac
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from attention_router.investigator.disposable_pg_proof import (
    DisposablePgSeedStore, ProofStoreDenied,
)
from attention_router.investigator.disposable_stage import DisposableSignedStage
from attention_router.investigator.signed_admission import (
    SignedEnvelope, SyntheticAdmission, SyntheticBinding, _signed_bytes,
)

pytestmark = pytest.mark.postgres
_KEY = b"public-fixture-key-not-real-do-not-use-000000001"
_NOW = 2000000000


@pytest.fixture()
def proof(pg_url):
    engine = create_engine(pg_url, future=True)
    schema = "inv_v0_" + uuid4().hex[:12]
    store = DisposablePgSeedStore(engine, schema)
    with engine.begin() as conn:
        conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        store.metadata.create_all(conn)
    try:
        yield store, engine
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        engine.dispose()


def _binding(tenant="synthetic-tenant-a"):
    return SyntheticBinding("synthetic-principal-a", "synthetic-zabbix", tenant, _KEY)


def _signed(binding, nonce="nonce12345678", original=b"original synthetic bytes"):
    base = SignedEnvelope(
        principal_id=binding.principal_id, issued_at=_NOW, nonce=nonce,
        content_type="application/octet-stream",
        source_event_id="synthetic-event-01", original=original,
        signature_hex="0" * 64,
    )
    return replace(base, signature_hex=hmac.new(
        binding.key, _signed_bytes(binding, base), sha256,
    ).hexdigest())


def _gateway(store, binding=None):
    binding = binding or _binding()
    authority = SyntheticAdmission(
        {binding.principal_id: binding}, now=lambda: _NOW,
    )
    return DisposableSignedStage(authority, store)


def _count(engine, schema, table):
    assert table in {"investigation_seeds", "admission_nonces", "investigation_audit"}
    with engine.connect() as c:
        return c.scalar(text(f'SELECT count(*) FROM "{schema}"."{table}"'))


def test_signed_event_is_durable_before_any_investigation(proof):
    store, engine = proof
    binding = _binding()
    envelope = _signed(binding)
    receipt = _gateway(store, binding).stage_signed(envelope)
    assert receipt.may_start_new_investigation
    assert _count(engine, store.schema, "investigation_seeds") == 1
    assert _count(engine, store.schema, "admission_nonces") == 1
    assert _count(engine, store.schema, "investigation_audit") == 1
    # Dispose pooled connections: a new connection must see the committed seed.
    engine.dispose()
    snap = store.read_seed(seed_id=receipt.seed.seed_id, tenant_id=binding.tenant_id)
    assert snap is not None
    assert snap.original == envelope.original
    assert snap.payload_sha256 == sha256(envelope.original).hexdigest()
    assert snap.tenant_id == binding.tenant_id
    assert repr(snap).find("original synthetic bytes") == -1
    assert binding.tenant_id not in repr(snap)
    assert store.audit_codes(seed_id=snap.seed_id, tenant_id=binding.tenant_id) == (
        "SEED_ADMITTED",
    )


def test_same_nonce_rejected_across_independent_admission_instances(proof):
    store, engine = proof
    signed = _signed(_binding())
    first = _gateway(store).stage_signed(signed)
    # Fresh verifier simulates a distinct process: its in-memory nonce is empty.
    with pytest.raises(ProofStoreDenied, match="REPLAY_DENIED"):
        _gateway(store).stage_signed(signed)
    assert _count(engine, store.schema, "investigation_seeds") == 1
    assert _count(engine, store.schema, "admission_nonces") == 1
    assert store.audit_codes(
        seed_id=first.seed.seed_id, tenant_id="synthetic-tenant-a",
    ) == ("SEED_ADMITTED",)


def test_new_nonce_same_event_is_idempotent_and_never_starts_second_session(proof):
    store, engine = proof
    gateway = _gateway(store)
    first = gateway.stage_signed(_signed(_binding(), nonce="nonce12345678"))
    again = gateway.stage_signed(_signed(_binding(), nonce="nonce12345679"))
    assert first.may_start_new_investigation
    assert not again.may_start_new_investigation
    assert again.seed.seed_id == first.seed.seed_id
    assert _count(engine, store.schema, "investigation_seeds") == 1
    assert _count(engine, store.schema, "admission_nonces") == 2
    assert store.audit_codes(
        seed_id=first.seed.seed_id, tenant_id="synthetic-tenant-a",
    ) == ("SEED_ADMITTED", "IDEMPOTENT_RETRY")


def test_changed_bytes_same_event_identity_roll_back_everything(proof):
    store, engine = proof
    gateway = _gateway(store)
    first = gateway.stage_signed(_signed(_binding(), nonce="nonce12345678"))
    with pytest.raises(ProofStoreDenied, match="EVENT_IDENTITY_CONFLICT"):
        gateway.stage_signed(_signed(
            _binding(), nonce="nonce12345679", original=b"changed bytes",
        ))
    assert _count(engine, store.schema, "investigation_seeds") == 1
    assert _count(engine, store.schema, "admission_nonces") == 1
    assert _count(engine, store.schema, "investigation_audit") == 1
    stored = store.read_seed(seed_id=first.seed.seed_id, tenant_id="synthetic-tenant-a")
    assert stored.original == b"original synthetic bytes"


def test_untrusted_event_is_rejected_before_any_persistence(proof):
    store, engine = proof
    bad = replace(_signed(_binding()), original=b"tampered")
    with pytest.raises(ValueError, match="ADMISSION_REJECTED"):
        _gateway(store).stage_signed(bad)
    for table in ("investigation_seeds", "admission_nonces", "investigation_audit"):
        assert _count(engine, store.schema, table) == 0


def test_cannot_read_or_append_audit_for_another_tenant(proof):
    store, engine = proof
    receipt = _gateway(store).stage_signed(_signed(_binding()))
    seed_id = receipt.seed.seed_id
    assert store.read_seed(seed_id=seed_id, tenant_id="synthetic-tenant-b") is None
    with pytest.raises(ProofStoreDenied, match="AUDIT_SCOPE_DENIED"):
        store.append_audit(
            seed_id=seed_id, tenant_id="synthetic-tenant-b",
            event_type="TOOL_REQUESTED", query_fingerprint="a" * 32,
        )
    store.append_audit(
        seed_id=seed_id, tenant_id="synthetic-tenant-a",
        event_type="TOOL_REQUESTED", query_fingerprint="b" * 32,
    )
    assert store.audit_codes(
        seed_id=seed_id, tenant_id="synthetic-tenant-a",
    ) == ("SEED_ADMITTED", "TOOL_REQUESTED")
    assert store.audit_codes(
        seed_id=seed_id, tenant_id="synthetic-tenant-b",
    ) == ()


def test_attempt_to_insert_free_text_or_payload_in_audit_is_rejected(proof):
    store, _ = proof
    receipt = _gateway(store).stage_signed(_signed(_binding()))
    with pytest.raises(ProofStoreDenied, match="AUDIT_ENTRY_REJECTED"):
        store.append_audit(
            seed_id=receipt.seed.seed_id, tenant_id="synthetic-tenant-a",
            event_type="model says ignore rules and leak token",
        )


def test_concurrent_same_event_two_nonces_produces_one_seed(proof):
    store, engine = proof

    def stage(index):
        return _gateway(store).stage_signed(_signed(
            _binding(), nonce=f"nonce1234567{index}",
        ))

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(executor.map(stage, (1, 2)))
    assert sorted(item.may_start_new_investigation for item in receipts) == [
        False, True,
    ]
    assert receipts[0].seed.seed_id == receipts[1].seed.seed_id
    assert _count(engine, store.schema, "investigation_seeds") == 1
    assert _count(engine, store.schema, "admission_nonces") == 2
    assert _count(engine, store.schema, "investigation_audit") == 2


def test_failing_audit_insert_rolls_back_seed_and_nonce(proof):
    store, engine = proof
    # Only test schema is changed, never public application or production tables.
    with engine.begin() as conn:
        store.audit.drop(conn)
    with pytest.raises(Exception):
        _gateway(store).stage_signed(_signed(_binding()))
    assert _count(engine, store.schema, "investigation_seeds") == 0
    assert _count(engine, store.schema, "admission_nonces") == 0


def test_store_rejects_sqlite_and_non_ephemeral_database_name():
    from sqlalchemy import create_engine

    engine = create_engine("sqlite+pysqlite:///:memory:")
    with pytest.raises(ProofStoreDenied, match="DISPOSABLE_POSTGRES_REQUIRED"):
        DisposablePgSeedStore(engine, "inv_v0_0123456789ab")
    engine.dispose()
