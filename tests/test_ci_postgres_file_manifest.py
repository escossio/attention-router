"""Regression for exact-SHA marker discovery, including root-level tests."""

from __future__ import annotations

from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
collector_module = runpy.run_path(str(ROOT / "scripts/ci_postgres_manifest.py"))
parser_module = runpy.run_path(str(
    ROOT / "ops/provisioning/distributed-ci-lab/control-plane/parse-postgres-discovery.py"
))
selected_file_paths = collector_module["selected_file_paths"]
parse_discovery = parser_module["parse_discovery"]
SHA = "a" * 40


def fake_output(paths: list[str], *, count: int | None = None, sha: str = SHA) -> str:
    return "\n".join([
        f"CI_SHA={sha}",
        f"CI_POSTGRES_DISCOVERY_SHA={sha}",
        *[f"CI_POSTGRES_FILE={path}" for path in paths],
        f"CI_POSTGRES_FILE_COUNT={len(paths) if count is None else count}",
        "CI_STATUS=PASS",
        "",
    ])


def test_67_plus_two_are_preserved_in_discovered_manifest():
    initial = [f"tests/integration/test_postgres_{i:03}.py" for i in range(67)]
    extras = ["tests/test_conversation_hybrid.py", "tests/test_passive_vectors.py"]
    files = initial + extras
    assert len(parse_discovery(fake_output(files), sha=SHA)) == 69
    assert set(extras).issubset(parse_discovery(fake_output(files), sha=SHA))
    assert set(selected_file_paths(
        f"{filename}::test_example[{index}]" for index, filename in enumerate(files)
    )) == set(files)


@pytest.mark.parametrize("bad,pattern", [
    (lambda p: fake_output(p, count=67), "INCOMPLETE"),
    (lambda p: fake_output(p, sha="b" * 40), "SHA_MISMATCH"),
    (lambda p: fake_output(p).replace("CI_STATUS=PASS", "CI_STATUS=FAIL"), "NOT_SUCCESSFUL"),
    (lambda p: fake_output(p + [p[0]]), "DUPLICATE_PATH"),
    (lambda p: fake_output(["tests/../secrets.py"]), "PATH_INVALID"),
    (lambda p: fake_output(p).replace("CI_POSTGRES_FILE_COUNT=2", ""), "COUNT_INVALID"),
])
def test_discovery_fails_closed(bad, pattern):
    paths = ["tests/integration/test_postgres_foo.py", "tests/test_passive_vectors.py"]
    with pytest.raises(ValueError, match=pattern):
        parse_discovery(bad(paths), sha=SHA)


def test_pytest_plugin_reads_selected_items_not_names():
    plugin = collector_module["PostgresItems"]()
    # pytest has already applied -m postgres deselection at collection_finish.
    plugin.pytest_collection_finish(SimpleNamespace(items=[
        SimpleNamespace(nodeid="tests/test_passive_vectors.py::test_pg"),
        SimpleNamespace(nodeid="tests/integration/test_postgres_foo.py::test_pg"),
    ]))
    assert selected_file_paths(plugin.nodeids) == [
        "tests/integration/test_postgres_foo.py",
        "tests/test_passive_vectors.py",
    ]


def test_empty_and_escaping_pytest_paths_are_refused():
    with pytest.raises(ValueError, match="NO_SELECTED_TESTS"):
        selected_file_paths([])
    with pytest.raises(ValueError, match="SELECTED_PATH_INVALID"):
        selected_file_paths(["../tests/test_escape.py::test_no"])
