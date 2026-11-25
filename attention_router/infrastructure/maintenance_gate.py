"""Cooperative local maintenance admission, independent of release authority.

One private, host-local directory shared by explicitly enrolled participants.
Stable flock inode + atomic fsynced state. Never reap a reservation on timeout,
PID death or restart: an abandoned cycle may have an unknown external effect.
This is NOT a distributed lock, proof of an exhaustive writer roster, or
permission to migrate a database. Unconfigured runtimes retain their behavior.
"""

from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from uuid import uuid4

ENV = "ANDY_MAINTENANCE_GATE_DIR"
MAX_ACTIVE = 1024
MAX_BYTES = 1024 * 1024
NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
TOKEN = re.compile(r"[0-9a-f]{32}")


class GateError(RuntimeError):
    """A refusal, never permission to continue without the configured gate."""


def require(ok, reason):
    if not ok:
        raise GateError(reason)


def _name(value):
    return isinstance(value, str) and NAME.fullmatch(value) is not None


def _token(value):
    return isinstance(value, str) and TOKEN.fullmatch(value) is not None


def _private(info, kind, mode):
    return kind(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == mode


class MaintenanceGate:
    """Persistent, same-host gate. All callers must use the same private directory."""

    def __init__(self, directory):
        self.directory = Path(directory)
        require(self.directory.is_absolute(), "GATE_ABSOLUTE_PATH_REQUIRED")

    def _dir(self):
        require(_private(self.directory.lstat(), stat.S_ISDIR, 0o700), "GATE_DIRECTORY_INVALID")

    @classmethod
    def initialize(cls, directory, participants):
        gate = cls(directory)
        require(isinstance(participants, (tuple, list)) and 0 < len(participants) <= 32,
                "GATE_PARTICIPANTS_INVALID")
        require(all(_name(p) for p in participants) and len(set(participants)) == len(participants),
                "GATE_PARTICIPANTS_INVALID")
        # A restart must never initialize or reset an existing gate.
        gate.directory.mkdir(mode=0o700, exist_ok=False)
        identities = {}
        for name in ("control", "execution"):
            fd = os.open(gate.directory / (name + ".lock"),
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                info = os.fstat(fd)
                identities[name] = [info.st_dev, info.st_ino]
                os.fsync(fd)
            finally:
                os.close(fd)
        gate._save({
            "version": 2, "gate_id": uuid4().hex, "lock_identity": identities["control"],
            "execution_lock_identity": identities["execution"], "execution": None,
            "participants": sorted(participants), "seen": [], "window": None,
            "hold": None, "active": {},
        })
        return gate

    def _save(self, state):
        data = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
        require(len(data) <= MAX_BYTES, "GATE_LEDGER_LIMIT")
        fd, name = tempfile.mkstemp(prefix=".state-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.directory / "state.json")
            dfd = os.open(self.directory, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        finally:
            Path(name).unlink(missing_ok=True)

    def _load(self, lock_info):
        fd = os.open(self.directory / "state.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            require(_private(os.fstat(stream.fileno()), stat.S_ISREG, 0o600), "GATE_STATE_FILE_INVALID")
            raw = stream.read(MAX_BYTES + 1)
        require(len(raw) <= MAX_BYTES, "GATE_LEDGER_LIMIT")

        def unique(pairs):
            result = {}
            for key, value in pairs:
                require(key not in result, "GATE_DUPLICATE_FIELD")
                result[key] = value
            return result

        state = json.loads(raw, object_pairs_hook=unique)
        require(isinstance(state, dict) and set(state) == {
            "version", "gate_id", "lock_identity", "execution_lock_identity", "execution",
            "participants", "seen", "window", "hold", "active",
        }, "GATE_STATE_INVALID")
        require(type(state["version"]) is int and state["version"] == 2 and _token(state["gate_id"]),
                "GATE_STATE_INVALID")
        require(state["lock_identity"] == [lock_info.st_dev, lock_info.st_ino], "GATE_LOCK_REPLACED")
        roster, seen, active = state["participants"], state["seen"], state["active"]
        require(isinstance(roster, list) and 0 < len(roster) <= 32
                and all(_name(x) for x in roster) and roster == sorted(set(roster)),
                "GATE_ROSTER_INVALID")
        require(isinstance(seen, list) and all(_name(x) for x in seen)
                and seen == sorted(set(seen)) and set(seen) <= set(roster), "GATE_SEEN_INVALID")
        require(state["window"] is None or _token(state["window"]), "GATE_WINDOW_INVALID")
        require(state["hold"] is None or (_token(state["hold"]) and state["window"] is not None),
                "GATE_HOLD_INVALID")
        require(isinstance(active, dict) and len(active) <= MAX_ACTIVE, "GATE_LEDGER_INVALID")
        for key, item in active.items():
            require(_token(key) and isinstance(item, dict) and set(item) == {"participant", "uncertain"}
                    and item["participant"] in seen and type(item["uncertain"]) is bool,
                    "GATE_RESERVATION_INVALID")
        require(state["hold"] is None or (not active and seen == roster), "GATE_HOLD_INVALID")
        identity = state["execution_lock_identity"]
        require(isinstance(identity, list) and len(identity) == 2
                and all(type(x) is int and x >= 0 for x in identity), "GATE_EXECUTION_LOCK_INVALID")
        execution = state["execution"]
        if execution is not None:
            require(state["hold"] is not None and isinstance(execution, dict)
                    and set(execution) == {"operation", "approval_ref", "comment_id", "status"},
                    "GATE_EXECUTION_INVALID")
            require(execution["operation"] in ("schema_apply", "schema_rollback")
                    and isinstance(execution["approval_ref"], str)
                    and re.fullmatch(r"[0-9a-f]{64}", execution["approval_ref"]) is not None
                    and type(execution["comment_id"]) is int and execution["comment_id"] > 0
                    and execution["status"] in ("STARTED", "COMPLETED", "UNCONFIRMED"),
                    "GATE_EXECUTION_INVALID")
        return state

    @contextmanager
    def _locked(self):
        # Never unlink the lock. Refuse replaced inodes, missing state and unsafe
        # filesystem objects; do not silently recreate a configured gate.
        try:
            self._dir()
            path = self.directory / "control.lock"
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                info = os.fstat(fd)
                require(_private(info, stat.S_ISREG, 0o600), "GATE_LOCK_INVALID")
                deadline = time.monotonic() + 1.0
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        require(time.monotonic() < deadline, "GATE_LOCK_TIMEOUT")
                        time.sleep(0.005)
                current = path.lstat()
                require((current.st_dev, current.st_ino) == (info.st_dev, info.st_ino),
                        "GATE_LOCK_REPLACED")
                state = self._load(info)
                yield state
            finally:
                os.close(fd)
        except (OSError, ValueError, TypeError) as exc:
            raise GateError("GATE_STORAGE_UNAVAILABLE") from exc

    @staticmethod
    def _observe(state, participant):
        require(participant in state["participants"], "GATE_PARTICIPANT_NOT_ENROLLED")
        if participant in state["seen"]:
            return False
        state["seen"] = sorted((*state["seen"], participant))
        return True

    def observe(self, participant):
        """Record that a configured participant has reached the gate (not liveness)."""
        with self._locked() as state:
            if self._observe(state, participant):
                self._save(state)

    @staticmethod
    def _summary(state):
        active = state["active"]
        missing = sorted(set(state["participants"]) - set(state["seen"]))
        phase = ("RUNNING" if state["window"] is None else
                 "DRAINING" if active or missing else "DRAINED")
        return {
            "state": phase, "gate_id": state["gate_id"], "window": state["window"],
            "maintenance_held": state["hold"] is not None,
            "execution_state": state["execution"]["status"] if state["execution"] is not None else None,
            "participants": list(state["participants"]), "missing": missing,
            "active": len(active),
            "uncertain": sum(item["uncertain"] for item in active.values()),
            "active_by_participant": {
                p: sum(item["participant"] == p for item in active.values())
                for p in state["participants"]
            },
        }

    def status(self):
        with self._locked() as state:
            return self._summary(state)

    def request_drain(self):
        # Fresh random generation; never accept a caller-supplied stale window.
        with self._locked() as state:
            require(state["window"] is None, "GATE_OTHER_WINDOW_ACTIVE")
            state["window"] = uuid4().hex
            self._save(state)
            return self._summary(state)

    def wait_drained(self, window, timeout):
        require(isinstance(timeout, (int, float)) and math.isfinite(timeout)
                and 0 <= timeout <= 600, "GATE_TIMEOUT_INVALID")
        deadline = time.monotonic() + timeout
        while True:
            result = self.status()
            require(result["window"] == window and _token(window), "GATE_WINDOW_MISMATCH")
            if result["state"] == "DRAINED":
                return result
            # Timeout does not reopen admission or remove any reservation.
            require(time.monotonic() < deadline, "GATE_DRAIN_TIMEOUT")
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))

    def claim_maintenance(self, window):
        """Hold a drained cooperative gate; not authorization to perform DDL."""
        with self._locked() as state:
            require(_token(window) and state["window"] == window, "GATE_WINDOW_MISMATCH")
            require(self._summary(state)["state"] == "DRAINED", "GATE_NOT_DRAINED")
            require(state["hold"] is None, "GATE_MAINTENANCE_ALREADY_HELD")
            state["hold"] = uuid4().hex
            self._save(state)
            return state["hold"]

    @contextmanager
    def _execution_lock(self):
        """A separate kernel lock protects the hold through the DB commit.

        Order is always execution.lock then control.lock. Regular admissions
        only use control.lock; they can still promptly refuse new work.
        """
        try:
            self._dir()
            fd = os.open(self.directory / "execution.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            raise GateError("GATE_EXECUTION_LOCK_UNAVAILABLE") from exc
        try:
            info = os.fstat(fd)
            require(_private(info, stat.S_ISREG, 0o600), "GATE_EXECUTION_LOCK_INVALID")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise GateError("GATE_EXECUTION_BUSY") from exc
            with self._locked() as state:
                self._verify_execution_lock(state, info)
            yield info
        finally:
            os.close(fd)

    def _verify_execution_lock(self, state, info):
        current = (self.directory / "execution.lock").lstat()
        identity = [info.st_dev, info.st_ino]
        require(_private(current, stat.S_ISREG, 0o600)
                and [current.st_dev, current.st_ino] == identity
                and state["execution_lock_identity"] == identity, "GATE_EXECUTION_LOCK_REPLACED")

    @staticmethod
    def _verify_held(state, binding):
        require(isinstance(binding, dict) and set(binding) == {
            "gate_id", "window", "hold", "participants",
        }, "GATE_BINDING_INVALID")
        require(all(_token(binding[k]) for k in ("gate_id", "window", "hold"))
                and all(state[k] == binding[k] for k in ("gate_id", "window", "hold", "participants")),
                "GATE_BINDING_MISMATCH")
        require(MaintenanceGate._summary(state)["state"] == "DRAINED",
                "GATE_NOT_DRAINED")

    @contextmanager
    def execute_maintenance(self, binding, *, operation, approval_ref, comment_id):
        """Consume this hold for one attempt, not an authorization itself.

        STARTED is durable before opening the database. A crash leaves it
        intact; no automatic retry, release or resumption is possible. The
        caller must keep this context around the entire transaction and commit.
        """
        require(operation in ("schema_apply", "schema_rollback")
                and isinstance(approval_ref, str) and re.fullmatch(r"[0-9a-f]{64}", approval_ref)
                and type(comment_id) is int and comment_id > 0, "GATE_EXECUTION_INVALID")
        marker = {"operation": operation, "approval_ref": approval_ref,
                  "comment_id": comment_id, "status": "STARTED"}
        with self._execution_lock() as info:
            with self._locked() as state:
                self._verify_execution_lock(state, info)
                self._verify_held(state, binding)
                require(state["execution"] is None, "GATE_EXECUTION_ALREADY_ATTEMPTED")
                state["execution"] = marker
                self._save(state)

            def check():
                with self._locked() as state:
                    self._verify_execution_lock(state, info)
                    self._verify_held(state, binding)
                    require(state["execution"] == marker, "GATE_EXECUTION_CHANGED")

            def finish(status):
                with self._locked() as state:
                    self._verify_execution_lock(state, info)
                    self._verify_held(state, binding)
                    require(state["execution"] == marker, "GATE_EXECUTION_CHANGED")
                    state["execution"] = {**marker, "status": status}
                    self._save(state)

            try:
                yield check
            except BaseException:
                finish("UNCONFIRMED")
                raise
            else:
                # If this persistence fails after commit, STARTED or an
                # uncertain receipt remains. Never auto-release the hold.
                finish("COMPLETED")

    def release_maintenance(self, window, hold, *, reconciled=False):
        """Release only outside execution; attempted work requires reconciliation.

        The trusted operator must verify the real transaction and effects
        before supplying reconciled=True. This flag is not a recovery proof.
        """
        with self._execution_lock() as info:
            with self._locked() as state:
                self._verify_execution_lock(state, info)
                require(_token(window) and state["window"] == window and _token(hold)
                        and state["hold"] == hold, "GATE_HOLD_MISMATCH")
                require(state["execution"] is None or reconciled is True,
                        "GATE_RECONCILIATION_REQUIRED")
                state["execution"] = None
                state["hold"] = None
                self._save(state)

    def resume(self, window):
        """Explicit resume or abort after all tracked work has ended, no hold."""
        with self._locked() as state:
            require(_token(window) and state["window"] == window, "GATE_WINDOW_MISMATCH")
            require(state["hold"] is None and not state["active"], "GATE_RESUME_BLOCKED")
            state["window"] = None
            self._save(state)
            return self._summary(state)

    def _admit(self, participant):
        with self._locked() as state:
            changed = self._observe(state, participant)
            if state["window"] is not None:
                if changed:
                    self._save(state)
                return None
            require(len(state["active"]) < MAX_ACTIVE, "GATE_LEDGER_LIMIT")
            token = uuid4().hex
            state["active"][token] = {"participant": participant, "uncertain": False}
            # Work starts only after the reservation is durable.
            self._save(state)
            return token

    def _finish(self, token, participant, uncertain):
        with self._locked() as state:
            require(state["active"].get(token) == {"participant": participant, "uncertain": False},
                    "GATE_RESERVATION_MISMATCH")
            if uncertain:
                state["active"][token]["uncertain"] = True
            else:
                del state["active"][token]
            self._save(state)

    @contextmanager
    def admission(self, participant):
        token = self._admit(participant)
        if token is None:
            yield False
            return
        try:
            yield True
        except BaseException:
            # A caught failure may have committed an external effect. Do not
            # pretend it finished safely or automatically reclaim its token.
            self._finish(token, participant, uncertain=True)
            raise
        else:
            self._finish(token, participant, uncertain=False)


def configured_gate():
    if ENV not in os.environ:
        return None
    # Empty, malformed or missing configured paths never mean disabled.
    return MaintenanceGate(os.environ[ENV])


@contextmanager
def runtime_admission(participant):
    gate = configured_gate()
    if gate is None:
        yield True
        return
    with gate.admission(participant) as admitted:
        yield admitted


def observe_runtime(participant):
    gate = configured_gate()
    if gate is not None:
        gate.observe(participant)
