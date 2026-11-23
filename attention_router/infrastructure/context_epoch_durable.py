"""Durable CAS anchor for an explicitly provisioned private context canary.

Lives outside the primary PostgreSQL backup domain. No file or epoch is
created on a read. Does not reuse the SQLite write-locked consent laboratory.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
import json
import os
from pathlib import Path
from secrets import token_hex
import sqlite3
from stat import S_ISDIR, S_ISREG

from attention_router.application.context_epoch import EpochState, sign_state
from attention_router.config import settings
from attention_router.infrastructure.hashing import stable_json


class DurableEpochAuthority:
    """Multi-process SQLite WAL authority, with read-only lookups and CAS writes."""

    def __init__(self, path: Path, *, authority_id: str, key: bytes):
        if not (settings.app_env == "test" or settings.context_epoch_authority_canary_enabled):
            raise ValueError("CONTEXT_EPOCH_DURABLE_DISABLED")
        if not isinstance(key, bytes) or len(key) < 32 or not authority_id:
            raise ValueError("CONTEXT_EPOCH_DURABLE_CONFIGURATION_INVALID")
        self.path = Path(path)
        self.authority_id = authority_id
        self._key = key
        self._verify_file()

    def _verify_file(self):
        if not self.path.is_absolute() or self.path != self.path.resolve():
            raise ValueError("CONTEXT_EPOCH_DURABLE_PATH_INVALID")
        try:
            directory = self.path.parent.stat()
            state = self.path.lstat()
        except OSError as error:
            raise ValueError("CONTEXT_EPOCH_DURABLE_MISSING") from error
        if (
            not S_ISDIR(directory.st_mode) or not S_ISREG(state.st_mode)
            or directory.st_uid != os.getuid() or state.st_uid != os.getuid()
            or (directory.st_mode & 0o077) or (state.st_mode & 0o077)
        ):
            raise ValueError("CONTEXT_EPOCH_DURABLE_PERMISSIONS_INVALID")

    @classmethod
    def provision(
        cls, path: Path, *, authority_id: str, key: bytes,
        domain: str, checkpoint: str,
    ) -> "DurableEpochAuthority":
        """Explicit, privileged enrollment, NEVER called by the query path."""
        path = Path(path)
        if not path.is_absolute() or path != path.resolve() or not domain or not checkpoint:
            raise ValueError("CONTEXT_EPOCH_DURABLE_PATH_INVALID")
        directory = path.parent.stat()
        if (
            not S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
            or directory.st_mode & 0o077
        ):
            raise ValueError("CONTEXT_EPOCH_DURABLE_PERMISSIONS_INVALID")
        if not path.exists():
            flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags, 0o600)
            os.close(fd)
        self = cls(path, authority_id=authority_id, key=key)
        with closing(self._connect(read_only=False)) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS epochs (domain TEXT PRIMARY KEY, state TEXT NOT NULL)")
            state = EpochState(authority_id, domain, token_hex(16), 0, checkpoint)
            try:
                conn.execute(
                    "INSERT INTO epochs(domain,state) VALUES(?,?)",
                    (domain, stable_json(asdict(state))),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("CONTEXT_EPOCH_DURABLE_ALREADY_PROVISIONED") from error
        return self

    def _connect(self, *, read_only):
        if not (settings.app_env == "test" or settings.context_epoch_authority_canary_enabled):
            raise ValueError("CONTEXT_EPOCH_DURABLE_DISABLED")
        self._verify_file()
        return sqlite3.connect(
            self.path.as_uri() + ("?mode=ro" if read_only else "?mode=rw"),
            timeout=5, uri=True,
        )

    def read(self, domain: str, challenge: str) -> tuple[EpochState, bytes]:
        if not domain or not challenge:
            raise OSError("CONTEXT_EPOCH_DURABLE_INVALID_CHALLENGE")
        try:
            with closing(self._connect(read_only=True)) as conn:
                row = conn.execute("SELECT state FROM epochs WHERE domain=?", (domain,)).fetchone()
            if row is None:
                raise OSError("CONTEXT_EPOCH_DURABLE_DOMAIN_UNKNOWN")
            state = EpochState(**json.loads(row[0]))
        except (sqlite3.Error, ValueError, TypeError) as error:
            raise OSError("CONTEXT_EPOCH_DURABLE_READ_UNAVAILABLE") from error
        if state.authority != self.authority_id or state.domain != domain:
            raise OSError("CONTEXT_EPOCH_DURABLE_AUTHORITY_MISMATCH")
        return state, sign_state(self._key, state, challenge)

    def advance(self, expected: EpochState, proposed: EpochState) -> None:
        if (
            expected.authority != self.authority_id or proposed.authority != expected.authority
            or proposed.domain != expected.domain or proposed.epoch != expected.epoch
            or proposed.revision != expected.revision + 1 or not proposed.checkpoint
        ):
            raise ValueError("CONTEXT_EPOCH_ADVANCE_INVALID")
        try:
            with closing(self._connect(read_only=False)) as conn, conn:
                cursor = conn.execute(
                    "UPDATE epochs SET state=? WHERE domain=? AND state=?",
                    (stable_json(asdict(proposed)), expected.domain, stable_json(asdict(expected))),
                )
                if cursor.rowcount != 1:
                    raise ValueError("CONTEXT_EPOCH_CAS_CONFLICT")
        except sqlite3.Error as error:
            raise OSError("CONTEXT_EPOCH_DURABLE_WRITE_UNAVAILABLE") from error
