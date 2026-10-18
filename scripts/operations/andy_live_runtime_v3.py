"""Candidate boot controller: fail closed on a pinned OFF or ON Worker composition.

This file is inert until installed by a separately approved operational change.
All manifests and Compose inputs remain private. Errors deliberately omit values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time

from scripts.forensics.andy_worker_composition_snapshot import baseline_checks


ROLES = ("base", "otel", "otel_e2e", "execution", "groups", "visitor_recovery")
CRITICAL = (
    "AGENT_DECISION_DEFAULT_BLUEPRINT_ID",
    "AGENT_DECISION_DEFAULT_VISITOR_ONLY",
    "GROUP_CONTEXTUAL_PRESENCE_ENABLED",
    "GROUP_CONTEXTUAL_PRESENCE_GROUP_ID",
    "GROUP_CONTEXTUAL_PRESENCE_TENANT_ID",
    "CLIENT_SESSION_ENABLED",
    "AGENT_EXECUTION_ENABLED",
    "ANDY_AGENT_ENABLED",
    "ATTENTION_ROUTER_BUILD_SHA",
    "RUNTIME_HEAD",
    "FIRST_CONTACT_ENABLED",
    "FIRST_CONTACT_TENANT_ID",
    "FIRST_CONTACT_SOURCE_ACCOUNT",
)
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SOURCE_SHA = re.compile(r"[0-9a-f]{40}\Z")
LEGACY_CRITICAL = CRITICAL[:-3]


class InvariantError(RuntimeError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise InvariantError(code)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def pinned_bytes(path: str, *, private: bool = False) -> bytes:
    item = Path(path)
    info = item.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o022, "INPUT_PERMISSIONS_INVALID")
    require(not private or not info.st_mode & 0o077, "PRIVATE_INPUT_INVALID")
    require(info.st_size <= 1024 * 1024, "INPUT_TOO_LARGE")
    with os.fdopen(os.open(item, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
        return source.read()


def invoke(args: list[str], *, cwd: str | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False, timeout=90)
    require(result.returncode == 0, "DOCKER_COMMAND_FAILED")
    return result.stdout


def inspect(kind: str, name: str) -> dict:
    rows = json.loads(invoke(["docker", kind, "inspect", name]))
    require(type(rows) is list and len(rows) == 1, "INSPECT_INVALID")
    return rows[0]


def load_manifest(*, previous: bool = False) -> dict:
    suffix = "_PREVIOUS" if previous else ""
    path = os.environ.get("ATTENTION_LIVE_WORKER_MANIFEST" + suffix, "")
    expected = os.environ.get("ATTENTION_LIVE_WORKER_MANIFEST_SHA256" + suffix, "")
    require(bool(path) and bool(SHA256.fullmatch(expected)), "MANIFEST_PIN_MISSING")
    raw = pinned_bytes(path, private=True)
    require(digest(raw) == expected, "MANIFEST_DRIFT")
    def unique(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            require(key not in result, "MANIFEST_DUPLICATE_KEY")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique)
    valid_schemas = ("andy-live-worker-lifecycle-v2", "andy-live-worker-lifecycle-v3") if previous else ("andy-live-worker-lifecycle-v3",)
    require(type(value) is dict and value.get("schema_version") in valid_schemas, "MANIFEST_SCHEMA_INVALID")
    return value


def compose_command(manifest: dict, *, previous: bool = False) -> list[str]:
    files = manifest.get("compose_files")
    require(type(files) is list and len(files) == len(ROLES), "COMPOSE_INPUT_COUNT_INVALID")
    require([row.get("role") for row in files] == list(ROLES), "COMPOSE_INPUT_ORDER_INVALID")
    paths = [row.get("path") for row in files]
    require(all(type(path) is str and Path(path).is_absolute() for path in paths) and len(set(paths)) == len(paths), "COMPOSE_PATH_INVALID")
    if not previous:
        configured = os.environ.get("ATTENTION_LIVE_WORKER_COMPOSE_FILES", "").split(":")
        require(configured == paths, "COMPOSE_BOOT_INPUT_DRIFT")
    command = ["docker", "compose", "-p", manifest["project"]]
    for row in files:
        expected = row.get("sha256")
        require(type(expected) is str and bool(SHA256.fullmatch(expected)), "COMPOSE_PIN_INVALID")
        require(digest(pinned_bytes(row["path"])) == expected, "COMPOSE_INPUT_DRIFT")
        command.extend(["-f", row["path"]])
    return command


def preflight(*, previous: bool = False) -> tuple[dict, dict, dict, list[str]]:
    manifest = load_manifest(previous=previous)
    for field, env_name in (("project", "ATTENTION_LIVE_PROJECT"), ("workdir", "ATTENTION_LIVE_WORKDIR"),
                            ("worker_container", "ATTENTION_LIVE_WORKER_CONTAINER"), ("db_container", "ATTENTION_LIVE_DB_CONTAINER"),
                            ("service", "ATTENTION_LIVE_WORKER_SERVICE")):
        require(type(manifest.get(field)) is str and manifest[field] and manifest[field] == os.environ.get(env_name), "BOOT_SETTING_DRIFT")
    require(Path(manifest["workdir"]).is_dir(), "WORKDIR_MISSING")
    require(bool(SHA256.fullmatch(manifest.get("render_sha256", ""))), "RENDER_PIN_INVALID")
    require(bool(SOURCE_SHA.fullmatch(manifest.get("source_sha", ""))), "SOURCE_PIN_INVALID")
    require(bool(re.fullmatch(r"sha256:[0-9a-f]{64}", manifest.get("image_id", ""))), "IMAGE_PIN_INVALID")
    command = compose_command(manifest, previous=previous)
    rendered = json.loads(invoke(command + ["config", "--format", "json"], cwd=manifest["workdir"]))
    require(digest(canonical(rendered)) == manifest["render_sha256"], "RENDER_DRIFT")
    require(set(rendered.get("services", {})) == {manifest["service"]}, "COMPOSE_SERVICE_SCOPE_INVALID")
    service = rendered["services"][manifest["service"]]
    env = service.get("environment") or {}
    legacy = previous and manifest["schema_version"] == "andy-live-worker-lifecycle-v2"
    critical = LEGACY_CRITICAL if legacy else CRITICAL
    require(type(env) is dict and set(manifest.get("critical_environment_sha256", {})) == set(critical), "CRITICAL_ENV_SCHEMA_INVALID")
    for key in critical:
        require(type(env.get(key)) is str and digest(env[key].encode()) == manifest["critical_environment_sha256"][key], "CRITICAL_ENV_DRIFT")
    require(env["AGENT_DECISION_DEFAULT_BLUEPRINT_ID"].strip() != "", "VISITOR_DEFAULT_MISSING")
    require(env["AGENT_DECISION_DEFAULT_VISITOR_ONLY"] == "true", "VISITOR_GUARD_DISABLED")
    require(all(env[key] == "true" for key in ("GROUP_CONTEXTUAL_PRESENCE_ENABLED", "CLIENT_SESSION_ENABLED", "AGENT_EXECUTION_ENABLED", "ANDY_AGENT_ENABLED")), "REQUIRED_CAPABILITY_DISABLED")
    require(env["GROUP_CONTEXTUAL_PRESENCE_GROUP_ID"].strip() != "" and env["GROUP_CONTEXTUAL_PRESENCE_TENANT_ID"].strip() != "", "GROUP_SCOPE_MISSING")
    require(env["ATTENTION_ROUTER_BUILD_SHA"] == env["RUNTIME_HEAD"] == manifest["source_sha"], "SOURCE_PROVENANCE_DRIFT")
    mode = "off" if legacy else manifest.get("first_contact_mode")
    require(mode in ("off", "on"), "FIRST_CONTACT_MODE_INVALID")
    require(env.get("FIRST_CONTACT_ENABLED", "false") == ("true" if mode == "on" else "false"), "FIRST_CONTACT_MODE_DRIFT")
    if mode == "on":
        require(bool(env["FIRST_CONTACT_TENANT_ID"].strip()) and bool(env["FIRST_CONTACT_SOURCE_ACCOUNT"].strip()), "FIRST_CONTACT_SCOPE_MISSING")
        require(env["FIRST_CONTACT_TENANT_ID"] == os.environ.get("ATTENTION_LIVE_FIRST_CONTACT_TENANT_ID") and
                env["FIRST_CONTACT_SOURCE_ACCOUNT"] == os.environ.get("ATTENTION_LIVE_FIRST_CONTACT_SOURCE_ACCOUNT"),
                "FIRST_CONTACT_BOOT_SCOPE_DRIFT")
    image = inspect("image", service["image"])
    require(image.get("Id") == manifest["image_id"], "IMAGE_DRIFT")
    require(service["image"] == manifest["image_id"], "IMAGE_REFERENCE_UNPINNED")
    require(image.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision") == manifest["source_sha"], "IMAGE_REVISION_DRIFT")
    require(service.get("restart") == "unless-stopped", "WORKER_RESTART_POLICY_DRIFT")
    return manifest, rendered, image, command


def verify(manifest: dict, rendered: dict, image: dict, *, require_running: bool = True) -> None:
    worker = inspect("container", manifest["worker_container"])
    db = inspect("container", manifest["db_container"])
    label = worker.get("Config", {}).get("Labels", {}).get("com.docker.compose.project.config_files", "")
    require(label.split(",") == [row["path"] for row in manifest["compose_files"]], "RUNNING_COMPOSE_INPUT_DRIFT")
    require(baseline_checks(rendered, worker, image, service=manifest["service"])["all_match"], "RUNTIME_PARITY_FAILED")
    for item in (worker, db):
        require(item["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped", "RESTART_POLICY_DRIFT")
        if require_running:
            require(item["State"]["Status"] == "running" and item["State"].get("Health", {}).get("Status") == "healthy", "SERVICE_UNHEALTHY")


def wait_healthy(name: str, seconds: int) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        item = inspect("container", name)
        if item["State"]["Status"] == "running" and item["State"].get("Health", {}).get("Status") == "healthy":
            return
        time.sleep(1)
    raise InvariantError("SERVICE_HEALTH_TIMEOUT")


def same_scope(source: dict, target: dict) -> bool:
    return all(source[key] == target[key] for key in
               ("project", "workdir", "worker_container", "db_container", "service"))


def reconcile(manifest: dict, rendered: dict, image: dict, command: list[str], *, previous: tuple | None = None) -> None:
    seconds = int(os.environ.get("ATTENTION_LIVE_HEALTH_TIMEOUT_SECONDS", "60"))
    require(0 < seconds <= 600, "HEALTH_TIMEOUT_INVALID")
    # Verify a pinned source composition before any write. A changed target cannot
    # be treated as the current runtime. The source manifest remains a rollback pin.
    if previous is None:
        verify(manifest, rendered, image, require_running=False)
    else:
        old_manifest, old_rendered, old_image, _ = previous
        require(same_scope(old_manifest, manifest), "TRANSITION_SCOPE_DRIFT")
        verify(old_manifest, old_rendered, old_image, require_running=False)
    invoke(["docker", "update", "--restart", "unless-stopped", manifest["db_container"]])
    invoke(["docker", "start", manifest["db_container"]])
    wait_healthy(manifest["db_container"], seconds)
    fresh_manifest, fresh_rendered, fresh_image, fresh_command = preflight()
    require(fresh_manifest == manifest and fresh_rendered == rendered and fresh_image == image and
            fresh_command == command, "TARGET_CHANGED_BEFORE_UP")
    if previous is not None:
        require(preflight(previous=True) == previous, "SOURCE_CHANGED_BEFORE_UP")
        verify(old_manifest, old_rendered, old_image, require_running=False)
    invoke(command + ["up", "-d", "--no-deps", "--pull", "never", "--no-build", manifest["service"]], cwd=manifest["workdir"])
    wait_healthy(manifest["worker_container"], seconds)
    verify(manifest, rendered, image)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="read-only parity check")
    parser.add_argument("--transition-check", action="store_true", help="read-only source and target validation")
    args = parser.parse_args()
    try:
        manifest, rendered, image, command = preflight()
        previous = preflight(previous=True) if args.transition_check or os.environ.get("ATTENTION_LIVE_WORKER_MANIFEST_PREVIOUS") else None
        if args.check:
            verify(manifest, rendered, image)
        elif args.transition_check:
            require(previous is not None, "PREVIOUS_MANIFEST_PIN_MISSING")
            old_manifest, old_rendered, old_image, _ = previous
            require(same_scope(old_manifest, manifest), "TRANSITION_SCOPE_DRIFT")
            verify(old_manifest, old_rendered, old_image)
        else:
            reconcile(manifest, rendered, image, command, previous=previous)
    except (InvariantError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        code = str(exc) if isinstance(exc, InvariantError) else "LIFECYCLE_INPUT_INVALID"
        print(json.dumps({"status": "blocked", "reason_code": code}, sort_keys=True))
        return 1
    print(json.dumps({"status": "ok", "mode": "check" if args.check else "transition-check" if args.transition_check else "reconcile"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
