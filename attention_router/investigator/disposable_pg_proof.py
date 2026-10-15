"""PostgreSQL admission + initial audit proof in a disposable test database only.

This module cannot open a production database: it accepts solely the project's
explicitly named ephemeral test database. It creates NO tables or migrations.
DDL is confined to tests and never added to Alembic/main application metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re
from uuid import uuid4

from sqlalchemy import (
    Column, DateTime, ForeignKey, Integer, LargeBinary, MetaData, String,
    Table, UniqueConstraint, func, select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from .model import SourceEvent

_SCHEMA_RE = re.compile(r"^inv_v0_[0-9a-f]{12}$")
_DB_RE = re.compile(r"^attention_router_test_[0-9a-f]{10}$")
_AUDIT_CODES = frozenset({
    "SEED_ADMITTED", "IDEMPOTENT_RETRY", "TOOL_REQUESTED",
    "TOOL_DENIED", "TOOL_FAILED", "EVIDENCE_RECORDED",
    "INVESTIGATION_STARTED", "INVESTIGATION_FINISHED",
})
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{16,64}$")


class ProofStoreDenied(RuntimeError):
    """No payloads, tenant IDs or database addresses in error messages."""


@dataclass(frozen=True)
class StagedSeed:
    seed_id: str
    created: bool
    payload_sha256: str


@dataclass(frozen=True)
class SeedSnapshot:
    seed_id: str
    tenant_id: str
    source: str
    source_event_id: str
    original: bytes
    payload_sha256: str


class DisposablePgSeedStore:
    """Only a deliberately created isolated schema within a disposable DB."""

    def __init__(self, engine: Engine, schema: str) -> None:
        url = engine.url
        if (
            engine.dialect.name != "postgresql"
            or url.drivername != "postgresql+psycopg"
            or url.username != "ar_test"
            or url.host not in {"localhost", "127.0.0.1", "postgres"}
            or not _DB_RE.fullmatch(url.database or "")
            or url.query
            or not _SCHEMA_RE.fullmatch(schema)
        ):
            raise ProofStoreDenied("DISPOSABLE_POSTGRES_REQUIRED")

        self.schema = schema
        self.session_factory = sessionmaker(bind=engine, future=True)
        self.metadata = MetaData(schema=schema)
        self.seeds = Table(
            "investigation_seeds", self.metadata,
            Column("seed_id", String(36), primary_key=True),
            Column("principal_id", String(80), nullable=False),
            Column("tenant_id", String(80), nullable=False),
            Column("source", String(80), nullable=False),
            Column("source_event_id", String(100), nullable=False),
            Column("original", LargeBinary, nullable=False),
            Column("payload_sha256", String(64), nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False,
                   server_default=func.now()),
            UniqueConstraint("principal_id", "source_event_id",
                             name="uq_inv_seed_principal_event"),
        )
        self.nonces = Table(
            "admission_nonces", self.metadata,
            Column("principal_id", String(80), primary_key=True),
            Column("nonce", String(80), primary_key=True),
            Column("seed_id", String(36), ForeignKey(
                f"{schema}.investigation_seeds.seed_id"), nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False,
                   server_default=func.now()),
        )
        self.audit = Table(
            "investigation_audit", self.metadata,
            Column("seq", Integer, primary_key=True, autoincrement=True),
            Column("seed_id", String(36), ForeignKey(
                f"{schema}.investigation_seeds.seed_id"), nullable=False),
            Column("tenant_id", String(80), nullable=False),
            Column("event_type", String(40), nullable=False),
            Column("query_fingerprint", String(64), nullable=True),
            Column("created_at", DateTime(timezone=True), nullable=False,
                   server_default=func.now()),
        )

    def stage_verified(
        self, *, principal_id: str, nonce: str, event: SourceEvent,
    ) -> StagedSeed:
        """Atomically claim event, nonce, original bytes and first audit entry.

        Caller MUST authenticate the envelope and derive event source/tenant
        from a trusted principal binding before calling this method. It is not
        a generic admission or authorization API.
        """
        if (
            not isinstance(principal_id, str) or not principal_id
            or not isinstance(nonce, str) or not nonce
            or event.source_event_id is None
            or not isinstance(event.original, bytes)
            or len(event.original) > 65536
        ):
            raise ProofStoreDenied("INVALID_PROOF_ADMISSION")

        digest = sha256(event.original).hexdigest()
        seed_id = str(uuid4())
        with self.session_factory.begin() as db:
            created_id = db.execute(
                pg_insert(self.seeds).values(
                    seed_id=seed_id, principal_id=principal_id,
                    tenant_id=event.tenant_id, source=event.source,
                    source_event_id=event.source_event_id,
                    original=event.original, payload_sha256=digest,
                ).on_conflict_do_nothing(
                    index_elements=["principal_id", "source_event_id"],
                ).returning(self.seeds.c.seed_id)
            ).scalar_one_or_none()

            existing = db.execute(
                select(self.seeds).where(
                    self.seeds.c.principal_id == principal_id,
                    self.seeds.c.source_event_id == event.source_event_id,
                )
            ).mappings().one()
            if (
                existing["tenant_id"] != event.tenant_id
                or existing["source"] != event.source
                or bytes(existing["original"]) != event.original
                or existing["payload_sha256"] != digest
            ):
                # Rollback covers any pending seed/nonce/audit changes.
                raise ProofStoreDenied("EVENT_IDENTITY_CONFLICT")

            actual_id = existing["seed_id"]
            claimed_nonce = db.execute(
                pg_insert(self.nonces).values(
                    principal_id=principal_id, nonce=nonce, seed_id=actual_id,
                ).on_conflict_do_nothing(
                    index_elements=["principal_id", "nonce"],
                ).returning(self.nonces.c.nonce)
            ).scalar_one_or_none()
            if claimed_nonce is None:
                raise ProofStoreDenied("REPLAY_DENIED")

            created = created_id is not None
            db.execute(self.audit.insert().values(
                seed_id=actual_id, tenant_id=event.tenant_id,
                event_type="SEED_ADMITTED" if created else "IDEMPOTENT_RETRY",
            ))
            return StagedSeed(actual_id, created, digest)

    def read_seed(self, *, seed_id: str, tenant_id: str) -> SeedSnapshot | None:
        """Explicit tenant scope for reading any original bytes."""
        with self.session_factory() as db:
            row = db.execute(select(self.seeds).where(
                self.seeds.c.seed_id == seed_id,
                self.seeds.c.tenant_id == tenant_id,
            )).mappings().one_or_none()
            if row is None:
                return None
            return SeedSnapshot(
                row["seed_id"], row["tenant_id"], row["source"],
                row["source_event_id"], bytes(row["original"]),
                row["payload_sha256"],
            )

    def append_audit(
        self, *, seed_id: str, tenant_id: str,
        event_type: str, query_fingerprint: str | None = None,
    ) -> None:
        """Restrict audit to stable codes; never serialize tool observations."""
        if event_type not in _AUDIT_CODES or (
            query_fingerprint is not None
            and (not isinstance(query_fingerprint, str)
                 or not _FINGERPRINT_RE.fullmatch(query_fingerprint))
        ):
            raise ProofStoreDenied("AUDIT_ENTRY_REJECTED")
        with self.session_factory.begin() as db:
            authorized = db.execute(select(self.seeds.c.seed_id).where(
                self.seeds.c.seed_id == seed_id,
                self.seeds.c.tenant_id == tenant_id,
            )).scalar_one_or_none()
            if authorized is None:
                raise ProofStoreDenied("AUDIT_SCOPE_DENIED")
            db.execute(self.audit.insert().values(
                seed_id=seed_id, tenant_id=tenant_id, event_type=event_type,
                query_fingerprint=query_fingerprint,
            ))

    def audit_codes(self, *, seed_id: str, tenant_id: str) -> tuple[str, ...]:
        with self.session_factory() as db:
            return tuple(db.execute(
                select(self.audit.c.event_type).where(
                    self.audit.c.seed_id == seed_id,
                    self.audit.c.tenant_id == tenant_id,
                ).order_by(self.audit.c.seq)
            ).scalars().all())
