"""Rollback detection adapter for the existing synthetic PolicyJournal.

Production must provide an independently protected CAS authority outside ALL
restoration domains. The synthetic adapter is a disposable lab double. The protected adapter requires
operator evidence of independent storage; neither protects against administrator
rollback of both directories. No operational storage is provisioned by this module.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import stat
import fcntl
import json
import os
from pathlib import Path
from typing import Protocol

from attention_router.application.semantic_governance import PolicyJournal
from attention_router.infrastructure.hashing import stable_hash


class RevisionAuthority(Protocol):
    def read(self) -> dict: ...
    def advance(self, expected: dict, proposed: dict) -> None: ...


def _stamp(state):
    return dict(revision=state['revision'], digest=stable_hash(state))


def _sync_directory(path):
    fd = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class FileRevisionAuthority:
    """Existing atomic file CAS primitive; bootstrap is always explicit."""
    format = 'SYNTHETIC_ANCHOR_V1'
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def _lock(self):
        with self.path.with_suffix('.lock').open('a') as stream:
            os.chmod(stream.name, 0o600)
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def _initialize(self):
        with self._lock():
            if self.path.exists():
                raise ValueError('ANCHOR_ALREADY_EXISTS')
            self._write(dict(revision=0, digest=None))

    def read(self):
        try:
            value = json.loads(self.path.read_text())
            stamp = value['stamp']
            if (value['format'] != self.format
                    or value['checksum'] != stable_hash(stamp)
                    or type(stamp['revision']) is not int or stamp['revision'] < 0
                    or (stamp['revision'] > 0 and (not isinstance(stamp['digest'], str)
                                                 or len(stamp['digest']) != 64))):
                raise ValueError()
            return stamp
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError('REVOCATION_AUTHORITY_UNAVAILABLE') from None

    def _write(self, stamp):
        payload = json.dumps(dict(format=self.format, stamp=stamp, checksum=stable_hash(stamp)))
        staging = self.path.with_suffix('.pending')
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, self.path)
        _sync_directory(self.path.parent)

    def advance(self, expected, proposed):
        with self._lock():
            if self.read() != expected or proposed['revision'] != expected['revision'] + 1:
                raise ValueError('REVOCATION_AUTHORITY_CAS_CONFLICT')
            self._write(proposed)


class SyntheticFileAuthority(FileRevisionAuthority):
    """Disposable adapter, never proof of operational backup independence."""
    def initialize_synthetic(self):
        self._initialize()


@dataclass(frozen=True)
class RestoreBoundary:
    """Operator evidence, NOT discovery or certification of backup exclusions.

    Paths stay in private configuration. An independently reviewed inventory must
    cover host snapshots, volume backups and external copies, not only pg_dump.
    The adapter checks placement/expiry but cannot attest an administrator's claim.
    """
    authority_directory: Path
    restored_roots: tuple[Path, ...]
    evidence_ref: str
    valid_until: datetime

    def require(self, path, now):
        root = self.authority_directory.resolve()
        if (not self.evidence_ref.strip() or not self.restored_roots
                or self.valid_until.utcoffset() is None or now >= self.valid_until
                or path.parent.resolve() != root or path.is_symlink()
                or self.authority_directory.is_symlink()):
            raise ValueError('RESTORE_BOUNDARY_UNVERIFIED')
        for restored in self.restored_roots:
            restored = restored.resolve()
            if root.is_relative_to(restored) or restored.is_relative_to(root):
                raise ValueError('SEPARATE_RESTORE_DOMAIN_REQUIRED')
        info = root.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('AUTHORITY_DIRECTORY_NOT_PRIVATE')
        if path.exists():
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError('AUTHORITY_FILE_NOT_PRIVATE')


class ProtectedFileAuthority(FileRevisionAuthority):
    """Single-host backend using an already provisioned independent directory.

    No directory, service, consent or backup exclusion is provisioned here.
    Restore of the authority itself is outside this guarantee and remains an
    operational gate. Loss/corruption/expired attestation always fails closed.
    """
    format = 'PROTECTED_REVISION_AUTHORITY_V1'

    def __init__(self, path, *, boundary, clock=lambda: datetime.now(UTC)):
        super().__init__(path)
        self.boundary, self.clock = boundary, clock

    def _require_boundary(self):
        try:
            self.boundary.require(self.path, self.clock())
        except (OSError, ValueError):
            raise ValueError('REVOCATION_AUTHORITY_BOUNDARY_UNAVAILABLE') from None

    def initialize_after_operational_approval(self):
        self._require_boundary()
        self._initialize()

    def read(self):
        self._require_boundary()
        return super().read()

    def advance(self, expected, proposed):
        self._require_boundary()
        return super().advance(expected, proposed)


class AnchoredPolicyJournal(PolicyJournal):
    """Uses the journal's existing lock and fsync implementation.

    Prepare payload -> advance independent authority -> publish payload. Failure
    after advance is closed, with roll-forward only from the exact prepared
    digest. Missing anchor is never initialized by a reader or writer.
    """
    def __init__(self, path: Path, authority: RevisionAuthority, *, journal_type=PolicyJournal):
        super().__init__(path)
        self.journal_type = journal_type
        self.grant_type = journal_type.grant_type
        self.format = journal_type.format
        self.authority = authority
        self.staged = journal_type(self.path.with_suffix('.prepared'))
        if isinstance(authority, FileRevisionAuthority) and authority.path.parent.resolve() == self.path.parent.resolve():
            raise ValueError('SEPARATE_RESTORE_DOMAIN_REQUIRED')

    def read(self):
        expected = self.authority.read()
        for journal in (self.journal_type(self.path), self.staged):
            try:
                state = journal.read()
            except ValueError:
                continue
            if _stamp(state) == expected:
                if journal is self.staged:
                    os.replace(self.staged.path, self.path)
                    _sync_directory(self.path.parent)
                return state
        raise ValueError('POLICY_ROLLBACK_OR_LOSS_DETECTED')

    def write(self, state):
        expected = self.authority.read()
        if state['revision'] != expected['revision']:
            raise ValueError('POLICY_REVISION_CONFLICT')
        self.staged.write(state)
        self.authority.advance(expected, _stamp(state))
        os.replace(self.staged.path, self.path)
        _sync_directory(self.path.parent)
