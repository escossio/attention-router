#!/usr/bin/env python3
"""Fail CI if the canonical WhatsApp runtime regresses to host-native units."""

from __future__ import annotations

from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
LEGACY_UNITS = re.compile(
    r"(?:attention-whatsapp-(?:transport|browser|xvfb|observer)"
    r"|andy-(?:browser-cdp-edge|transport-cdp-proxy|browser-netns|"
    r"transport-netns|transport-compat18103))\.service"
)
SCANNED_SUFFIXES = {".py", ".sh", ".service", ".yaml", ".yml", ".env", ".md"}


def check(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    package = root / "ops" / "whatsapp-container"
    contract = package / "runtime-contract.env"
    compose = package / "compose.yaml"
    if not contract.is_file() or "WHATSAPP_RUNTIME=CONTAINERIZED" not in contract.read_text():
        errors.append("missing containerized runtime contract")
    if not compose.is_file():
        errors.append("missing versioned WhatsApp Compose")
    else:
        content = compose.read_text()
        for service in ("browser:", "transport:", "observer:"):
            if f"  {service}" not in content:
                errors.append(f"missing Compose service: {service}")
    for prefix in ("ops", "scripts", ".github", "docs/architecture", "docs/guides"):
        folder = root / prefix
        if not folder.exists():
            continue
        for path in folder.rglob("*"):
            if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
                continue
            relative = path.relative_to(root)
            if relative.parts[:2] == ("ops", "historical"):
                continue
            if LEGACY_UNITS.search(path.name) or LEGACY_UNITS.search(path.read_text(errors="replace")):
                errors.append(f"legacy WhatsApp unit in canonical source: {relative}")
    if LEGACY_UNITS.search((root / "README.md").read_text(errors="replace")):
        errors.append("legacy WhatsApp unit in project README")
    return errors


if __name__ == "__main__":
    failures = check()
    for failure in failures:
        print(failure, file=sys.stderr)
    print("WHATSAPP_RUNTIME_ARCHITECTURE=" + ("FAIL" if failures else "PASS"))
    raise SystemExit(bool(failures))
