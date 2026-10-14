#!/usr/bin/env python3
"""Fail CI if the canonical WhatsApp runtime regresses to host-native units."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
LEGACY_UNITS = re.compile(
    r"(?:attention-whatsapp-(?:transport|browser|xvfb|observer)"
    r"|andy-(?:browser-cdp-edge|transport-cdp-proxy|browser-netns|"
    r"transport-netns|transport-compat18103))\.service"
)
RUNTIME_ASSIGNMENT = re.compile(r"\bWHATSAPP_RUNTIME\s*=\s*(\w+)")
SCANNED_SUFFIXES = {".py", ".sh", ".service", ".yaml", ".yml", ".env", ".md"}
MASK_GUARD = Path("ops/whatsapp-container/boot/bootstrap.py")


def _without_mask_guard_list(relative: Path, content: str) -> str:
    # The boot controller may name retired units solely to assert they are masked.
    if relative != MASK_GUARD:
        return content
    lines = content.splitlines(keepends=True)
    try:
        nodes = ast.parse(content).body
    except SyntaxError:
        return content
    for node in nodes:
        if not isinstance(node, ast.Assign) or not any(
            isinstance(target, ast.Name) and target.id == "LEGACY" for target in node.targets
        ):
            continue
        if not isinstance(node.value, ast.Tuple) or not all(
            isinstance(value, ast.Constant) and isinstance(value.value, str)
            and LEGACY_UNITS.fullmatch(value.value) for value in node.value.elts
        ):
            return content
        for line_no in range(node.lineno - 1, node.end_lineno):
            lines[line_no] = "\n"
        return "".join(lines)
    return content


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
            content = path.read_text(errors="replace")
            if LEGACY_UNITS.search(path.name) or LEGACY_UNITS.search(
                _without_mask_guard_list(relative, content)
            ):
                errors.append(f"legacy WhatsApp unit in canonical source: {relative}")
            for runtime in RUNTIME_ASSIGNMENT.findall(content):
                if runtime != "CONTAINERIZED":
                    errors.append(f"host-native WhatsApp runtime declaration: {relative}")
    project_readme = (root / "README.md").read_text(errors="replace")
    if "WHATSAPP_RUNTIME=CONTAINERIZED" not in project_readme:
        errors.append("project README must declare containerized WhatsApp target")
    if LEGACY_UNITS.search(project_readme):
        errors.append("legacy WhatsApp unit in project README")
    return errors


if __name__ == "__main__":
    failures = check()
    for failure in failures:
        print(failure, file=sys.stderr)
    print("WHATSAPP_RUNTIME_ARCHITECTURE=" + ("FAIL" if failures else "PASS"))
    raise SystemExit(bool(failures))
