#!/usr/bin/env python3
"""Verify the exact-SHA pytest collection emitted by the PostgreSQL CI worker.

No filenames are guessed from a directory or prefix. Malformed, incomplete,
duplicate or stale discovery is an infrastructure failure, never an empty
profile or an excuse to drop coverage.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re

SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
FILE_PATTERN = re.compile(r"tests/(?:[A-Za-z0-9_.-]+/)*test_[A-Za-z0-9_.-]+\.py\Z")


def parse_discovery(log: str, *, sha: str) -> list[str]:
    if not SHA_PATTERN.fullmatch(sha):
        raise ValueError("CI_POSTGRES_DISCOVERY_INVALID_SHA")
    lines = log.splitlines()
    for label in ("CI_SHA", "CI_POSTGRES_DISCOVERY_SHA"):
        values = [line.split("=", 1)[1] for line in lines if line.startswith(label + "=")]
        if values != [sha]:
            raise ValueError("CI_POSTGRES_DISCOVERY_SHA_MISMATCH")
    if [line for line in lines if line.startswith("CI_STATUS=")] != ["CI_STATUS=PASS"]:
        raise ValueError("CI_POSTGRES_DISCOVERY_NOT_SUCCESSFUL")

    counts = [line.split("=", 1)[1] for line in lines
              if line.startswith("CI_POSTGRES_FILE_COUNT=")]
    if len(counts) != 1 or not counts[0].isdigit() or int(counts[0]) < 1:
        raise ValueError("CI_POSTGRES_DISCOVERY_COUNT_INVALID")

    paths = [line.split("=", 1)[1] for line in lines
             if line.startswith("CI_POSTGRES_FILE=")]
    if any(not FILE_PATTERN.fullmatch(path) or ".." in Path(path).parts for path in paths):
        raise ValueError("CI_POSTGRES_DISCOVERY_PATH_INVALID")
    if len(paths) != len(set(paths)):
        raise ValueError("CI_POSTGRES_DISCOVERY_DUPLICATE_PATH")
    if len(paths) != int(counts[0]):
        raise ValueError("CI_POSTGRES_DISCOVERY_INCOMPLETE")
    return sorted(paths)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    files = parse_discovery(args.log.read_text(errors="replace"), sha=args.sha)
    args.output.write_text("\n".join(files) + "\n", encoding="utf-8")
    print(f"CI_POSTGRES_DISCOVERY_VERIFIED_FILES={len(files)}")


if __name__ == "__main__":
    main()
