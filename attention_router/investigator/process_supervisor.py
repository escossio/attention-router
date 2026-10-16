"""External hard deadline for a single *isolated* investigator subprocess.

A Python SDK cooperative timeout cannot safely stop a hung entire agent run.
This supervisor starts a new process group, never uses a shell, and terminates
the group on timeout. This does NOT guarantee provider billing is stopped.
The calling deployment must pass a minimal, authorized environment.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
import signal
import subprocess
import time
from typing import Mapping, Sequence


@dataclass(frozen=True)
class SupervisedResult:
    exit_code: int | None
    timed_out: bool
    duration_seconds: float


def run_with_deadline(
    argv: Sequence[str], *,
    deadline_seconds: float,
    env: Mapping[str, str],
    cwd: str | None = None,
) -> SupervisedResult:
    """Run one independent child with no inherited stdin/stdout or shell.

    The child may read only credentials explicitly supplied through env.
    Do not use a long-lived process, send operational host mounts, or print argv.
    """
    if (
        not isinstance(argv, (tuple, list))
        or not argv
        or not all(type(part) is str and part for part in argv)
        or type(deadline_seconds) not in (int, float)
        or not math.isfinite(deadline_seconds)
        or not 0 < deadline_seconds <= 120
        or not isinstance(env, Mapping)
        or not all(type(key) is str and type(value) is str for key, value in env.items())
    ):
        raise ValueError("INVALID_SUPERVISOR_CONTRACT")
    started = time.monotonic()
    child = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        start_new_session=True,
        close_fds=True,
        cwd=cwd,
        env=dict(env),
    )
    timed_out = False
    try:
        child.wait(timeout=deadline_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        # The child is the process-group leader, including its descendants.
        # Send TERM, then forcibly remove any members that ignore it.
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
    return SupervisedResult(
        exit_code=None if timed_out else child.returncode,
        timed_out=timed_out,
        duration_seconds=time.monotonic() - started,
    )
