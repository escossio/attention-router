"""Stage an inert, root-owned V3 lifecycle package in an empty directory."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil


SOURCE_ROOT = Path(__file__).resolve().parents[2]
SOURCES = (
    "scripts/operations/andy_live_runtime_v3.py",
    "scripts/forensics/andy_worker_composition_snapshot.py",
    "scripts/operations/andy_live_runtime_v3_entrypoint.py",
)
INIT_FILES = ("scripts/__init__.py", "scripts/operations/__init__.py", "scripts/forensics/__init__.py")


def build(destination: Path) -> dict:
    if not destination.is_absolute() or destination.exists():
        raise ValueError("PACKAGE_DESTINATION_INVALID")
    destination.mkdir(mode=0o700, parents=False)
    hashes = {}
    for relative in (*SOURCES, *INIT_FILES):
        target = destination / relative
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        source = SOURCE_ROOT / relative
        if relative in INIT_FILES:
            target.write_bytes(b"")
        else:
            shutil.copyfile(source, target)
        target.chmod(0o755 if relative.endswith("_entrypoint.py") else 0o644)
        hashes[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
    receipt = {"schema_version": "andy-live-runtime-v3-package-v1", "files": hashes}
    item = destination / "package.sha256.json"
    item.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    item.chmod(0o400)
    destination.chmod(0o755)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = build(args.destination)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "reason_code": str(exc) if isinstance(exc, ValueError) else "PACKAGE_BUILD_FAILED"}))
        return 1
    print(json.dumps({"status": "ok", "schema_version": receipt["schema_version"],
                      "files": receipt["files"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
