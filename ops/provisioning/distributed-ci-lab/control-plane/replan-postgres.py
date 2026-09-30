#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib


def parse_workers(raw: str) -> list[str]:
    workers = [item.strip() for item in raw.split(",") if item.strip()]
    if not workers:
        raise SystemExit("no active workers")
    if len(set(workers)) != len(workers):
        raise SystemExit("duplicate active worker")
    return workers


def load_files(path: pathlib.Path) -> list[str]:
    files = [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not files:
        raise SystemExit("empty postgres file set")
    if len(set(files)) != len(files):
        raise SystemExit("duplicate postgres file")
    return files


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile-manifest", required=True)
    parser.add_argument("--capacity", required=True)
    parser.add_argument("--workers", required=True)
    parser.add_argument("--files", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--wave", type=int, default=1)
    args = parser.parse_args()
    workers = parse_workers(args.workers)
    manifest = json.loads(pathlib.Path(args.profile_manifest).read_text())
    capacity = json.loads(pathlib.Path(args.capacity).read_text())
    files = load_files(pathlib.Path(args.files))

    full = capacity.get("pytest_full_seconds") or {}
    missing_capacity = [worker for worker in workers if worker not in full]
    if missing_capacity:
        raise SystemExit(
            "missing worker capacity: " + ",".join(sorted(missing_capacity))
        )

    weights = manifest.get("file_weights_seconds") or {}
    missing_weights = [path for path in files if path not in weights]
    if missing_weights:
        raise SystemExit(
            "profile missing file weights: " + ",".join(sorted(missing_weights))
        )

    fastest = min(float(full[worker]) for worker in workers)
    speeds = {
        worker: fastest / float(full[worker])
        for worker in workers
    }
    loads = {worker: 0.0 for worker in workers}
    shards: dict[str, list[str]] = {worker: [] for worker in workers}

    for path in sorted(files, key=lambda item: float(weights[item]), reverse=True):
        worker = min(
            workers,
            key=lambda name: (
                (loads[name] + float(weights[path])) / speeds[name],
                name,
            ),
        )
        shards[worker].append(path)
        loads[worker] += float(weights[path])
    out = pathlib.Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    for worker, paths in shards.items():
        (out / f"postgres-{worker}.txt").write_text(
            "\n".join(paths) + ("\n" if paths else "")
        )

    run_manifest = {
        "schema_version": 1,
        "wave": args.wave,
        "workers": workers,
        "file_count": len(files),
        "worker_speed_relative": speeds,
        "worker_profile_load_seconds": loads,
        "worker_predicted_seconds": {
            worker: loads[worker] / speeds[worker]
            for worker in workers
        },
        "shard_file_count": {
            worker: len(shards[worker])
            for worker in workers
        },
    }
    (out / "manifest.json").write_text(
        json.dumps(run_manifest, indent=2) + "\n"
    )
    print(json.dumps(run_manifest, indent=2))


if __name__ == "__main__":
    main()
