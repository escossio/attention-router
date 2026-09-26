from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "ops" / "provisioning" / "distributed-ci-lab" / "control-plane"
REGISTRY = CONTROL / "host-registry.py"
REPLAN = CONTROL / "replan-postgres.py"


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def test_host_registry_models_worker_capability_dependency_and_health(tmp_path: Path) -> None:
    db = tmp_path / "registry.sqlite3"

    _run(REGISTRY, "--db", str(db), "init")
    _run(
        REGISTRY, "--db", str(db), "upsert-host",
        "--host-id", "note", "--canonical-name", "note",
        "--role", "dependency", "--access-target", "note",
    )
    for host in ("ci01", "ci02", "ci03"):
        _run(
            REGISTRY, "--db", str(db), "upsert-host",
            "--host-id", host, "--canonical-name", host,
            "--role", "worker", "--access-target", host,
        )
        _run(
            REGISTRY, "--db", str(db), "set-capability",
            "--host-id", host, "--capability", "postgres-worker",
        )

    _run(
        REGISTRY, "--db", str(db), "add-dependency",
        "--host-id", "ci03", "--depends-on", "note", "--kind", "access",
    )
    _run(
        REGISTRY, "--db", str(db), "set-health",
        "--host-id", "ci01", "--state", "READY", "--reason", "worker_ready",
    )
    _run(
        REGISTRY, "--db", str(db), "set-health",
        "--host-id", "ci03", "--state", "UNAVAILABLE",
        "--reason", "dependency_unavailable:note",
    )

    workers = _run(
        REGISTRY, "--db", str(db), "list-workers",
        "--capability", "postgres-worker",
    ).stdout.splitlines()
    assert workers == ["ci01", "ci02", "ci03"]

    snapshot = json.loads(
        _run(
            REGISTRY, "--db", str(db), "snapshot",
            "--capability", "postgres-worker",
        ).stdout
    )
    by_id = {host["host_id"]: host for host in snapshot["hosts"]}

    assert by_id["ci01"]["health_state"] == "READY"
    assert by_id["ci03"]["health_state"] == "UNAVAILABLE"
    assert by_id["ci03"]["health_reason"] == "dependency_unavailable:note"
    assert by_id["ci03"]["dependencies"] == [
        {"dependency_host_id": "note", "dependency_kind": "access"}
    ]


def test_replan_degraded_pool_preserves_complete_file_set(tmp_path: Path) -> None:
    files = [
        "tests/integration/test_postgres_a.py",
        "tests/integration/test_postgres_b.py",
        "tests/integration/test_postgres_c.py",
        "tests/integration/test_postgres_d.py",
    ]
    weights = {
        files[0]: 40.0,
        files[1]: 30.0,
        files[2]: 20.0,
        files[3]: 10.0,
    }
    manifest = tmp_path / "profile.json"
    manifest.write_text(
        json.dumps({"file_weights_seconds": weights}),
        encoding="utf-8",
    )
    capacity = tmp_path / "capacity.json"
    capacity.write_text(
        json.dumps(
            {
                "pytest_full_seconds": {
                    "ci01": 100.0,
                    "ci02": 200.0,
                    "ci03": 80.0,
                }
            }
        ),
        encoding="utf-8",
    )
    pending = tmp_path / "pending.txt"
    pending.write_text("\n".join(files) + "\n", encoding="utf-8")
    output = tmp_path / "wave"

    _run(
        REPLAN,
        "--profile-manifest", str(manifest),
        "--capacity", str(capacity),
        "--workers", "ci01,ci02",
        "--files", str(pending),
        "--output-dir", str(output),
        "--wave", "2",
    )

    assigned: list[str] = []
    for worker in ("ci01", "ci02"):
        shard = output / f"postgres-{worker}.txt"
        assigned.extend(
            line for line in shard.read_text(encoding="utf-8").splitlines() if line
        )

    assert sorted(assigned) == sorted(files)
    assert len(assigned) == len(set(assigned))
    assert not (output / "postgres-ci03.txt").exists()

    run_manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert run_manifest["workers"] == ["ci01", "ci02"]
    assert run_manifest["file_count"] == len(files)
    assert sum(run_manifest["shard_file_count"].values()) == len(files)
