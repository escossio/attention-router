#!/usr/bin/env python3
"""Discover the exact -m postgres test files using pytest's selected items.

The worker runs this inside its exact-SHA checkout, with the same Python
environment, PostgreSQL instance and Alembic revision as the real suite.
A filename glob is not an adequate substitute for pytest marker selection.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath

import pytest


def selected_file_paths(nodeids: list[str]) -> list[str]:
    """Return a strict, sorted file manifest without dropping root-level tests."""
    if not nodeids:
        raise ValueError("CI_POSTGRES_NO_SELECTED_TESTS")
    files = set()
    for nodeid in nodeids:
        file_path = nodeid.split("::", 1)[0]
        path = PurePosixPath(file_path)
        if (
            not file_path.startswith("tests/")
            or path.suffix != ".py"
            or ".." in path.parts
            or any(part in ("", ".") for part in path.parts)
            or len(path.parts) < 2
        ):
            raise ValueError("CI_POSTGRES_SELECTED_PATH_INVALID")
        files.add(file_path)
    return sorted(files)


class PostgresItems:
    def __init__(self, expected: str | None = None) -> None:
        self.nodeids: list[str] | None = None
        self.expected = expected
        self.outcomes: set[str] = set()
        self.passed: set[str] = set()

    @pytest.hookimpl(trylast=True)
    def pytest_collection_finish(self, session: pytest.Session) -> None:
        # After pytest's -m deselection, session.items is authoritative.
        self.nodeids = [item.nodeid for item in session.items]
        if self.expected is not None and selection_digest(self.nodeids) != self.expected:
            raise pytest.UsageError("CI_POSTGRES_NODE_SELECTION_MISMATCH")

    def pytest_terminal_summary(self, terminalreporter) -> None:
        stats = terminalreporter.stats
        self.passed = {report.nodeid for report in stats.get("passed", []) if isinstance(report, pytest.TestReport)}
        self.outcomes = self.passed.union(
            report.nodeid
            for status in ("skipped", "xfailed", "xpassed")
            for report in stats.get(status, [])
            if isinstance(report, pytest.TestReport)
        )


def selection_digest(nodeids: list[str]) -> str:
    if not nodeids or len(nodeids) != len(set(nodeids)):
        raise pytest.UsageError("CI_POSTGRES_EMPTY_OR_DUPLICATE_NODEIDS")
    payload = json.dumps(sorted(nodeids), ensure_ascii=True, separators=(",", ":"))
    return sha256(payload.encode()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--node-output", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-selection")
    parser.add_argument("--durations", action="store_true")
    args = parser.parse_args()
    if args.execute and not args.expected_selection:
        parser.error("--execute requires --expected-selection")
    if not args.execute and args.output is None:
        parser.error("discovery requires --output")
    collector = PostgresItems(args.expected_selection)
    pytest_args = ["-m", "postgres", "-q"]
    if not args.execute:
        pytest_args = ["--collect-only", "-m", "postgres", "-q", "-p", "no:cacheprovider"]
    if args.durations:
        pytest_args += ["--durations=0", "--durations-min=0"]
    rc = pytest.main(pytest_args, plugins=[collector])
    if rc != pytest.ExitCode.OK or collector.nodeids is None:
        return int(rc) or 70
    if args.execute and (collector.outcomes != set(collector.nodeids) or not collector.passed):
        raise SystemExit("CI_POSTGRES_EXECUTION_INCOMPLETE")
    paths = selected_file_paths(collector.nodeids)
    if args.output:
        args.output.write_text("\n".join(paths) + "\n", encoding="utf-8")
    if args.node_output:
        args.node_output.write_text(json.dumps(sorted(collector.nodeids), indent=2) + "\n", encoding="utf-8")
    for path in paths:
        print(f"CI_POSTGRES_FILE={path}")
    print(f"CI_POSTGRES_FILE_COUNT={len(paths)}")
    print(f"CI_POSTGRES_TEST_COUNT={len(collector.nodeids)}")
    print(f"CI_POSTGRES_SELECTION_SHA256={selection_digest(collector.nodeids)}")
    if args.execute:
        print(f"CI_POSTGRES_PASSED_TEST_COUNT={len(collector.passed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
