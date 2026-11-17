"""Rollback detection adapter for the existing synthetic PolicyJournal.

Production must provide an independently protected CAS authority outside ALL
restoration domains. The file adapter is exclusively a disposable lab double;
it does not protect against administrator rollback of both directories.
"""
from __future__ import annotations

from contextlib import contextmanager
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


class SyntheticFileAuthority:
    """Explicit synthetic bootstrap, never implicitly re-created after loss."""
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def _lock(self):
        with self.path.with_suffix('.lock').open('a') as stream:
            os.chmod(stream.name, 0o600)
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def initialize_synthetic(self):
        with self._lock():
            if self.path.exists():
                raise ValueError('ANCHOR_ALREADY_EXISTS')
            self._write(dict(revision=0, digest=None))

    def read(self):
        try:
            value = json.loads(self.path.read_text())
            stamp = value['stamp']
            if (value['format'] != 'SYNTHETIC_ANCHOR_V1'
                    or value['checksum'] != stable_hash(stamp)
                    or type(stamp['revision']) is not int or stamp['revision'] < 0
                    or (stamp['revision'] > 0 and (not isinstance(stamp['digest'], str)
                                                 or len(stamp['digest']) != 64))):
                raise ValueError()
            return stamp
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError('REVOCATION_AUTHORITY_UNAVAILABLE') from None

    def _write(self, stamp):
        payload = json.dumps(dict(format='SYNTHETIC_ANCHOR_V1', stamp=stamp, checksum=stable_hash(stamp)))
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
        if isinstance(authority, SyntheticFileAuthority) and authority.path.parent.resolve() == self.path.parent.resolve():
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
