#!/usr/bin/python3 -I
"""Pinned, directly executable entry point for the V3 lifecycle package."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import sys


FILES = {
    "ATTENTION_LIVE_V3_MODULE_SHA256": "scripts/operations/andy_live_runtime_v3.py",
    "ATTENTION_LIVE_V3_HELPER_SHA256": "scripts/forensics/andy_worker_composition_snapshot.py",
}
INIT_FILES = ("scripts/__init__.py", "scripts/operations/__init__.py", "scripts/forensics/__init__.py")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def fail(code: str) -> int:
    print(json.dumps({"status": "blocked", "reason_code": code}, sort_keys=True))
    return 1


def owned_path(path: Path, *, directory: bool) -> bool:
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    return expected(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o022


def main() -> int:
    root_value = os.environ.get("ATTENTION_LIVE_PACKAGE_ROOT", "")
    root = Path(root_value)
    if not root_value or not root.is_absolute():
        return fail("PACKAGE_ROOT_INVALID")
    try:
        if not owned_path(root, directory=True):
            return fail("PACKAGE_PERMISSIONS_INVALID")
        if not owned_path(root / "scripts", directory=True):
            return fail("PACKAGE_PERMISSIONS_INVALID")
        for relative in INIT_FILES:
            item = root / relative
            if not owned_path(item.parent, directory=True) or not owned_path(item, directory=False):
                return fail("PACKAGE_PERMISSIONS_INVALID")
            with os.fdopen(os.open(item, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
                if source.read(1):
                    return fail("PACKAGE_DRIFT")
        for name, relative in FILES.items():
            expected = os.environ.get(name, "")
            if not SHA256.fullmatch(expected):
                return fail("PACKAGE_PIN_MISSING")
            item = root / relative
            if not owned_path(item.parent, directory=True) or not owned_path(item, directory=False):
                return fail("PACKAGE_PERMISSIONS_INVALID")
            with os.fdopen(os.open(item, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
                actual = hashlib.file_digest(source, "sha256").hexdigest()
            if not hmac.compare_digest(actual, expected):
                return fail("PACKAGE_DRIFT")
    except OSError:
        return fail("PACKAGE_UNAVAILABLE")

    # -I excludes caller PYTHONPATH and the working directory. Only the pinned
    # root is added, after both implementation files have been verified.
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    try:
        from scripts.operations.andy_live_runtime_v3 import main as lifecycle_main
    except ImportError:
        return fail("PACKAGE_IMPORT_FAILED")
    return lifecycle_main()


if __name__ == "__main__":
    raise SystemExit(main())
