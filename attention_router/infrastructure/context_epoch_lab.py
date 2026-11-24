"""Synthetic CAS authority in a file outside the PostgreSQL backup domain."""

from contextlib import closing
from dataclasses import asdict
import json
from pathlib import Path
from secrets import token_hex
import sqlite3

from attention_router.application.context_epoch import EpochState, sign_state
from attention_router.config import settings
from attention_router.infrastructure.hashing import stable_json


class LabEpochAuthority:
    def __init__(self, path: Path, *, authority_id: str, key: bytes):
        if settings.app_env != "test":
            raise ValueError("CONTEXT_EPOCH_LAB_ONLY")
        self.path = path
        self.authority_id = authority_id
        self._key = key

    def initialize(self, domain: str, checkpoint: str) -> None:
        """Explicit enrollment. Readers and writers always open an existing file."""
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS epochs (domain TEXT PRIMARY KEY, state TEXT NOT NULL)",
            )
            state = EpochState(self.authority_id, domain, token_hex(16), 0, checkpoint)
            connection.execute("INSERT INTO epochs VALUES (?, ?)", (domain, stable_json(asdict(state))))

    def _connect(self):
        return sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)

    def read(self, domain: str, challenge: str) -> tuple[EpochState, bytes]:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute("SELECT state FROM epochs WHERE domain=?", (domain,)).fetchone()
            if row is None:
                raise OSError("CONTEXT_EPOCH_DOMAIN_UNKNOWN")
            state = EpochState(**json.loads(row[0]))
        except (sqlite3.Error, ValueError, TypeError) as error:
            raise OSError("CONTEXT_EPOCH_LAB_READ_FAILED") from error
        return state, sign_state(self._key, state, challenge)

    def advance(self, expected: EpochState, proposed: EpochState) -> None:
        if (
            expected.authority != self.authority_id or proposed.authority != expected.authority
            or proposed.domain != expected.domain or proposed.epoch != expected.epoch
            or proposed.revision != expected.revision + 1
        ):
            raise ValueError("CONTEXT_EPOCH_ADVANCE_INVALID")
        try:
            with closing(self._connect()) as connection, connection:
                cursor = connection.execute(
                    "UPDATE epochs SET state=? WHERE domain=? AND state=?",
                    (stable_json(asdict(proposed)), expected.domain, stable_json(asdict(expected))),
                )
                if cursor.rowcount != 1:
                    raise ValueError("CONTEXT_EPOCH_CAS_CONFLICT")
        except sqlite3.Error as error:
            raise OSError("CONTEXT_EPOCH_LAB_WRITE_FAILED") from error
