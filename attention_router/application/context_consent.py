"""Explicit prospective source consent for the isolated context pilot."""

from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_hex
import sqlite3

from attention_router.config import settings
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import ConversationMessageRow


RETENTION_LAYERS = frozenset({"source", "transcription", "embedding", "episode", "hypothesis", "journal", "backup"})


@dataclass(frozen=True)
class SourceConsent:
    decision_ref: str
    tenant_id: str
    owner_id: str
    source_account: str
    conversation_id: str
    message_id: str
    receipt_id: str
    purpose: str
    starts_at: datetime
    expires_at: datetime
    sensitivity: str
    retention: tuple[tuple[str, datetime], ...]

    def __post_init__(self):
        deadlines = dict(self.retention)
        if (
            not all((self.decision_ref, self.tenant_id, self.owner_id, self.source_account,
                     self.conversation_id, self.message_id, self.receipt_id))
            or self.purpose != "prospective_context" or self.sensitivity != "NORMAL"
            or set(deadlines) != RETENTION_LAYERS or len(self.retention) != len(deadlines)
            or self.starts_at.utcoffset() is None or self.expires_at.utcoffset() is None
            or self.starts_at >= self.expires_at
            or any(value.utcoffset() is None or value <= self.starts_at for value in deadlines.values())
        ):
            raise ValueError("CONTEXT_CONSENT_INVALID")

    def permits(
        self, message: ConversationMessageRow, *, owner_id: str, now: datetime, required_layers=(),
    ) -> bool:
        occurred = message.sent_at
        if occurred.utcoffset() is None:
            occurred = occurred.replace(tzinfo=UTC)
        deadlines = dict(self.retention)
        return (
            self.tenant_id == message.tenant_id and self.owner_id == owner_id
            and self.source_account == message.source_account and message.source == "wwebjs"
            and self.conversation_id == message.conversation_id and self.message_id == message.id
            and self.receipt_id == (message.metadata_json or {}).get("artifact_receipt_id")
            and message.sensitivity_class == self.sensitivity
            and self.starts_at <= occurred <= now < self.expires_at
            and now < min(deadlines[kind] for kind in ("source", "episode", "hypothesis", *required_layers))
        )


class LabConsent:
    """Explicit synthetic enrollment. Restart retains withdrawals and revision."""

    def __init__(self, path: Path, grants: tuple[SourceConsent, ...]):
        if settings.app_env != "test":
            raise ValueError("CONTEXT_CONSENT_LAB_ONLY")
        self.path = path
        self._grants = {grant.message_id: grant for grant in grants}
        if len(self._grants) != len(grants):
            raise ValueError("CONTEXT_CONSENT_DUPLICATE")
        self._fingerprint = stable_hash([
            {
                **asdict(grant),
                "starts_at": grant.starts_at.isoformat(),
                "expires_at": grant.expires_at.isoformat(),
                "retention": sorted((layer, deadline.isoformat()) for layer, deadline in grant.retention),
            }
            for grant in sorted(grants, key=lambda item: item.message_id)
        ])

    def initialize(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS enrollment (id INTEGER PRIMARY KEY CHECK (id=1), "
                "enrollment_id TEXT NOT NULL, fingerprint TEXT NOT NULL, revision INTEGER NOT NULL)",
            )
            connection.execute("CREATE TABLE IF NOT EXISTS withdrawals (message_id TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO enrollment VALUES (1, ?, ?, 0)", (token_hex(16), self._fingerprint))

    @contextmanager
    def _transaction(self):
        try:
            connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
        except sqlite3.Error as error:
            raise ValueError("CONTEXT_CONSENT_UNAVAILABLE") from error
        with closing(connection), connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                enrolled = connection.execute(
                    "SELECT enrollment_id, fingerprint, revision FROM enrollment WHERE id=1",
                ).fetchone()
            except sqlite3.Error as error:
                raise ValueError("CONTEXT_CONSENT_UNAVAILABLE") from error
            if enrolled is None or enrolled[1] != self._fingerprint:
                raise ValueError("CONTEXT_CONSENT_ENROLLMENT_MISMATCH")
            yield connection, f"{enrolled[0]}:{enrolled[2]}"

    @contextmanager
    def current(self, expected_revision: str | None = None):
        with self._transaction() as (connection, revision):
            if expected_revision is not None and expected_revision != revision:
                raise ValueError("CONTEXT_CONSENT_REVISION_STALE")
            revoked = {row[0] for row in connection.execute("SELECT message_id FROM withdrawals")}

            def permits(message: ConversationMessageRow, *, owner_id: str, now: datetime, required_layers=()):
                grant = self._grants.get(message.id)
                return grant is not None and message.id not in revoked and grant.permits(
                    message, owner_id=owner_id, now=now, required_layers=required_layers,
                )

            yield revision, permits

    def revoke(self, message_id: str) -> bool:
        with self._transaction() as (connection, _):
            if message_id not in self._grants:
                return False
            cursor = connection.execute("INSERT OR IGNORE INTO withdrawals VALUES (?)", (message_id,))
            if cursor.rowcount != 1:
                return False
            connection.execute("UPDATE enrollment SET revision=revision+1 WHERE id=1")
            return True
