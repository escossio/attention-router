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


def parse_discovery(log: str, *, sha: str, profile: bool = False) -> list[str]:
    if not SHA_PATTERN.fullmatch(sha):
        raise ValueError("CI_POSTGRES_DISCOVERY_INVALID_SHA")
    lines = log.splitlines()
    labels = ("CI_SHA",) if profile else ("CI_SHA", "CI_POSTGRES_DISCOVERY_SHA")
    for label in labels:
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


def parse_selection(log: str, *, profile: bool = False) -> tuple[int, str]:
    fields = {}
    for line in log.splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        if key in {"CI_SUITE", "CI_POSTGRES_TEST_COUNT", "CI_POSTGRES_SELECTION_SHA256",
                   "CI_POSTGRES_PASSED_TEST_COUNT"}:
            if key in fields:
                raise ValueError("CI_POSTGRES_SELECTION_DUPLICATE_FIELD")
            fields[key] = value
    count = fields.get("CI_POSTGRES_TEST_COUNT", "")
    digest = fields.get("CI_POSTGRES_SELECTION_SHA256", "")
    passed = fields.get("CI_POSTGRES_PASSED_TEST_COUNT", "")
    if (fields.get("CI_SUITE") != "postgres" or not re.fullmatch(r"[1-9][0-9]*", count)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or (profile and (not re.fullmatch(r"[1-9][0-9]*", passed) or int(passed) > int(count)))):
        raise ValueError("CI_POSTGRES_SELECTION_INVALID")
    return int(count), digest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--selection-output", required=True, type=Path)
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args()

    log = args.log.read_text(errors="replace")
    files = parse_discovery(log, sha=args.sha, profile=args.profile)
    count, digest = parse_selection(log, profile=args.profile)
    if count < len(files):
        raise SystemExit("CI_POSTGRES_SELECTION_TOO_FEW_TESTS")
    args.output.write_text("\n".join(files) + "\n", encoding="utf-8")
    args.selection_output.write_text(f"{count}\n{digest}\n", encoding="utf-8")
    print(f"CI_POSTGRES_DISCOVERY_VERIFIED_FILES={len(files)}")


if __name__ == "__main__":
    main()
