#!/usr/bin/env python3
"""Run every canonical L0 manifest against the real PostgreSQL database."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

from attention_router.infrastructure.db import SessionLocal
from attention_router.platform.l0_executor import discover_l0_manifests, execute_l0_manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", default="config/platform/scenarios")
    parser.add_argument("--source-sha", default=os.getenv("L0_EXECUTOR_SOURCE_SHA", "unknown"))
    parser.add_argument("--runtime-sha", default=os.getenv("L0_RUNTIME_SOURCE_SHA", "unknown"))
    args = parser.parse_args()
    entries = discover_l0_manifests(Path(args.catalog))
    results = []
    with SessionLocal() as session:
        for path, manifest in entries:
            result = execute_l0_manifest(
                session,
                manifest=manifest,
                manifest_path=str(path),
                source_sha=args.source_sha,
                runtime_sha=args.runtime_sha,
            )
            results.append(asdict(result))
        session.commit()
    print(json.dumps({"catalog_count": len(entries), "results": results}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
