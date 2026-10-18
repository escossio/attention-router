"""Compare captured Worker state and prepare private, inert deployment models.

Inputs are already captured JSON, never credentials supplied on the command
line. This module does not invoke Docker, build images, change a runtime, or
approve deployment. The operator must separately certify the proposed image.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import stat


DEFAULT_BLUEPRINT_KEY = "AGENT_DECISION_DEFAULT_BLUEPRINT_ID"
VISITOR_ONLY_KEY = "AGENT_DECISION_DEFAULT_VISITOR_ONLY"
PROVENANCE_KEYS = ("ATTENTION_ROUTER_BUILD_SHA", "RUNTIME_HEAD")
_PINNED_IMAGE = re.compile(r"(?:sha256:[0-9a-f]{64}|[^\s@]+@sha256:[0-9a-f]{64})\Z")
_SOURCE_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ns|us|ms|s|m|h)")


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ValueError(code)


def _environment(values: object) -> dict:
    if isinstance(values, dict):
        return {key: str(value) for key, value in values.items()}
    _require(isinstance(values, list), "COMPOSITION_ENVIRONMENT_INVALID")
    _require(all(isinstance(item, str) and "=" in item for item in values), "COMPOSITION_ENVIRONMENT_INVALID")
    return dict(item.split("=", 1) for item in values)


def _duration_ns(value: object) -> int:
    if value is None:
        return 0
    if type(value) is int:
        return value
    _require(type(value) is str, "COMPOSITION_DURATION_INVALID")
    parts = list(_DURATION.finditer(value))
    _require("".join(part[0] for part in parts) == value and bool(parts), "COMPOSITION_DURATION_INVALID")
    scale = {"ns": 1, "us": 1000, "ms": 1000000, "s": 1000000000, "m": 60000000000, "h": 3600000000000}
    return int(sum(float(part[1]) * scale[part[2]] for part in parts))


def baseline_checks(composition: dict, runtime: dict, image: dict, *, service: str) -> dict:
    """Return only dimensions and booleans; never include captured values."""
    declared = composition["services"][service]
    image_config = image["Config"]
    running = runtime["Config"]
    host = runtime["HostConfig"]
    expected_env = _environment(image_config.get("Env") or [])
    expected_env.update(_environment(declared.get("environment") or {}))
    health = declared.get("healthcheck") or {}
    image_health = image_config.get("Healthcheck") or {}
    running_health = running.get("Healthcheck") or {}
    health_equal = health.get("test", image_health.get("Test")) == running_health.get("Test")
    for field in ("interval", "timeout", "start_period", "start_interval"):
        runtime_field = "".join(part.title() for part in field.split("_"))
        expected = _duration_ns(health[field]) if field in health else image_health.get(runtime_field, 0)
        health_equal = health_equal and expected == running_health.get(runtime_field, 0)
    health_equal = health_equal and health.get("retries", image_health.get("Retries", 0)) == running_health.get("Retries", 0)
    networks = declared.get("networks") or {}
    actual_networks = runtime["NetworkSettings"]["Networks"]
    expected_names = {
        composition["networks"][name].get("name", composition["name"] + "_" + name)
        for name in networks
    }
    network_equal = expected_names == set(actual_networks)
    for name, options in networks.items():
        target = composition["networks"][name].get("name", composition["name"] + "_" + name)
        actual = actual_networks.get(target, {})
        options = options or {}
        for key, actual_key in (("ipv4_address", "IPAddress"), ("ipv6_address", "GlobalIPv6Address")):
            network_equal = network_equal and (key not in options or options[key] == actual.get(actual_key))
        network_equal = network_equal and set(options.get("aliases") or []) <= set(actual.get("Aliases") or [])
    mounts = {row["Destination"]: row for row in runtime.get("Mounts") or []}
    volumes = declared.get("volumes") or []
    volume_equal = {row["target"] for row in volumes} == set(mounts)
    for row in volumes:
        actual = mounts.get(row["target"], {})
        expected_source = row.get("source")
        source_key = "Source"
        if row["type"] == "volume":
            source_key = "Name"
            expected_source = composition.get("volumes", {}).get(expected_source, {}).get(
                "name", composition["name"] + "_" + str(expected_source)
            )
        volume_equal = volume_equal and (
            row["type"] == actual.get("Type") and expected_source == actual.get(source_key)
            and actual.get("RW") is (not row.get("read_only", False))
        )
    restart = declared.get("restart") or "no"
    restart_name, _, retry_count = restart.partition(":")
    expected_command = declared.get("command")
    if expected_command is None:
        expected_command = image_config.get("Cmd")
    expected_entrypoint = declared.get("entrypoint")
    if expected_entrypoint is None:
        expected_entrypoint = image_config.get("Entrypoint")
    checks = {
        "image_id": runtime["Image"] == image["Id"],
        "image_reference": declared.get("image") == running.get("Image"),
        "environment_including_image_defaults": expected_env == _environment(running.get("Env") or []),
        "command": expected_command == running.get("Cmd"),
        "entrypoint": expected_entrypoint == running.get("Entrypoint"),
        "healthcheck": bool(health_equal),
        "restart": restart_name == host["RestartPolicy"]["Name"] and int(retry_count or 0) == host["RestartPolicy"].get("MaximumRetryCount", 0),
        "networks_and_declared_aliases": bool(network_equal),
        "mounts": bool(volume_equal),
        "no_published_ports": not declared.get("ports") and not host.get("PortBindings"),
        "read_only_rootfs": declared.get("read_only", False) == host.get("ReadonlyRootfs", False),
        "privileged": declared.get("privileged", False) == host.get("Privileged", False),
        "user": declared.get("user", image_config.get("User", "")) == running.get("User", ""),
        "working_directory": declared.get("working_dir", image_config.get("WorkingDir", "")) == running.get("WorkingDir", ""),
    }
    return {"all_match": all(checks.values()), "checks": checks, "mismatched_dimensions": [key for key, value in checks.items() if not value]}


def verify_worker_delta(baseline: dict, proposed: dict, *, service: str, candidate_source_sha: str | None = None) -> None:
    """Check scope; supplying a candidate SHA also checks both provenance keys."""
    if candidate_source_sha is not None:
        _require(bool(_SOURCE_SHA.fullmatch(candidate_source_sha)), "COMPOSITION_SOURCE_SHA_INVALID")
        environment = proposed["services"][service].get("environment") or {}
        _require(all(environment.get(key) == candidate_source_sha for key in PROVENANCE_KEYS), "COMPOSITION_PROVENANCE_MISMATCH")
    before, after = deepcopy(baseline), deepcopy(proposed)
    for config in (before, after):
        worker = config["services"][service]
        worker.pop("image", None)
        for key in (DEFAULT_BLUEPRINT_KEY, VISITOR_ONLY_KEY, *PROVENANCE_KEYS):
            worker.setdefault("environment", {}).pop(key, None)
    _require(before == after, "COMPOSITION_UNAPPROVED_DELTA")


def plan_worker_models(baseline: dict, runtime: dict, worker_overlay: dict, *, service: str, candidate_image_ref: str, candidate_source_sha: str) -> dict:
    """Prepare JSON models only; the new code/image is a separate approved delta."""
    _require(set(worker_overlay) == {DEFAULT_BLUEPRINT_KEY, VISITOR_ONLY_KEY}, "COMPOSITION_OVERLAY_SCOPE_INVALID")
    value = worker_overlay[DEFAULT_BLUEPRINT_KEY]
    _require(type(value) is str and bool(value.strip()), "COMPOSITION_BLUEPRINT_REFERENCE_INVALID")
    _require(worker_overlay[VISITOR_ONLY_KEY] == "true", "COMPOSITION_VISITOR_GUARD_REQUIRED")
    _require(type(candidate_image_ref) is str and bool(_PINNED_IMAGE.fullmatch(candidate_image_ref)), "COMPOSITION_IMAGE_NOT_PINNED")
    _require(type(candidate_source_sha) is str and bool(_SOURCE_SHA.fullmatch(candidate_source_sha)), "COMPOSITION_SOURCE_SHA_INVALID")
    _require(bool(_PINNED_IMAGE.fullmatch(runtime["Image"])), "COMPOSITION_ROLLBACK_IMAGE_INVALID")
    proposed = deepcopy(baseline)
    proposed["services"][service].setdefault("environment", {}).update(worker_overlay)
    proposed["services"][service]["environment"].update({key: candidate_source_sha for key in PROVENANCE_KEYS})
    proposed["services"][service]["image"] = candidate_image_ref
    rollback = deepcopy(baseline)
    rollback["services"][service]["image"] = runtime["Image"]
    verify_worker_delta(baseline, proposed, service=service, candidate_source_sha=candidate_source_sha)
    verify_worker_delta(baseline, rollback, service=service)
    return {
        "proposed": proposed, "rollback": rollback,
        "review": {
            "applies_changes": False, "operational_approval_granted": False,
            "new_image_requires_exact_sha_certification": True,
            "candidate_image_bytes_verified": False,
            "changes_image_reference": candidate_image_ref != baseline["services"][service]["image"],
            "allowed_worker_environment_delta": [DEFAULT_BLUEPRINT_KEY, VISITOR_ONLY_KEY, *PROVENANCE_KEYS],
            "provenance_metadata_explicitly_updated_to_source_sha": candidate_source_sha,
            "other_services_networks_mounts_and_settings_preserved": True,
            "rollback_pins_captured_image_id": True,
            "rollback_restores_captured_default_presence_and_value": True,
            "rollback_restores_captured_visitor_guard_presence_and_value": True,
            "rollback_restores_captured_provenance_presence_and_values": True,
        },
    }


def _private_write(path: Path, value: dict) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "w", encoding="utf-8") as output:
        output.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _private_directory(path: Path) -> bool:
    details = path.lstat()
    return stat.S_ISDIR(details.st_mode) and details.st_uid == 0 and details.st_mode & 0o077 == 0


def _private_read_json(path: Path) -> dict:
    details = path.lstat()
    _require(stat.S_ISREG(details.st_mode) and details.st_uid == 0 and details.st_mode & 0o077 == 0, "COMPOSITION_INPUT_NOT_PRIVATE")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), encoding="utf-8") as source:
        return json.load(source)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--worker-overlay", type=Path, required=True)
    parser.add_argument("--candidate-image-ref", required=True)
    parser.add_argument("--candidate-source-sha", required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    try:
        _require(_private_directory(args.snapshot), "COMPOSITION_SNAPSHOT_NOT_PRIVATE")
        _require(_private_directory(args.output_directory.parent), "COMPOSITION_OUTPUT_PARENT_NOT_PRIVATE")
        metadata = _private_read_json(args.snapshot / "manifest.private.json")
        baseline = _private_read_json(args.snapshot / "composition.render.private.json")
        runtime = _private_read_json(args.snapshot / "worker.inspect.private.json")[0]
        image = _private_read_json(args.snapshot / "image.inspect.private.json")
        overlay = _private_read_json(args.worker_overlay)
        parity = baseline_checks(baseline, runtime, image, service=metadata["service"])
        _require(parity["all_match"], "COMPOSITION_RUNTIME_PARITY_FAILED")
        models = plan_worker_models(baseline, runtime, overlay, service=metadata["service"], candidate_image_ref=args.candidate_image_ref, candidate_source_sha=args.candidate_source_sha)
        args.output_directory.mkdir(mode=0o700)
        _private_write(args.output_directory / "proposed.render.private.json", models["proposed"])
        _private_write(args.output_directory / "rollback.render.private.json", models["rollback"])
        report = {"baseline": parity, **models["review"]}
        _private_write(args.output_directory / "review.sanitized.json", report)
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"valid": False, "reason_code": "COMPOSITION_REVIEW_FAILED_CLOSED"}))
        return 1
    print(json.dumps({"valid": True, **report}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
