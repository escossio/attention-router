#!/usr/bin/env python3
"""Discover the exact -m postgres test files using pytest's selected items.

The worker runs this inside its exact-SHA checkout, with the same Python
environment, PostgreSQL instance and Alembic revision as the real suite.
A filename glob is not an adequate substitute for pytest marker selection.
"""

from __future__ import annotations

import argparse
from pathlib import PurePosixPath

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
    def __init__(self) -> None:
        self.nodeids: list[str] | None = None

    @pytest.hookimpl(trylast=True)
    def pytest_collection_finish(self, session: pytest.Session) -> None:
        # After pytest's -m deselection, session.items is authoritative.
        self.nodeids = [item.nodeid for item in session.items]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    collector = PostgresItems()
    rc = pytest.main(
        ["--collect-only", "-m", "postgres", "-q", "-p", "no:cacheprovider"],
        plugins=[collector],
    )
    if rc != pytest.ExitCode.OK or collector.nodeids is None:
        raise SystemExit("CI_POSTGRES_COLLECTION_FAILED")
    paths = selected_file_paths(collector.nodeids)
    with open(args.output, "w", encoding="utf-8", newline="\n") as output:
        for path in paths:
            output.write(path + "\n")
    for path in paths:
        print(f"CI_POSTGRES_FILE={path}")
    print(f"CI_POSTGRES_FILE_COUNT={len(paths)}")
    print(f"CI_POSTGRES_TEST_COUNT={len(collector.nodeids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
