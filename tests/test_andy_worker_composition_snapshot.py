"""Synthetic snapshot checks: no Docker, production files, credentials or I/O."""

from copy import deepcopy
import json

import pytest

from scripts.forensics.andy_worker_composition_snapshot import (
    DEFAULT_BLUEPRINT_KEY, PROVENANCE_KEYS, VISITOR_ONLY_KEY, _private_write, baseline_checks,
    plan_worker_models, verify_worker_delta,
)


LIVE_IMAGE = "sha256:" + "a" * 64
NEW_IMAGE = "registry.invalid/worker@sha256:" + "b" * 64
CANDIDATE_SHA = "c" * 40


def visitor_overlay(reference="synthetic-blueprint"):
    return {DEFAULT_BLUEPRINT_KEY: reference, VISITOR_ONLY_KEY: "true"}


def fixtures():
    service = {
        "image": "worker:captured", "command": ["python", "-m", "worker"],
        "environment": {"AGENT_ENABLED": "true", "SECRET": "synthetic-secret"},
        "healthcheck": {"test": ["CMD", "health"], "interval": "30s", "timeout": "5s", "retries": 3},
        "networks": {"application": {"aliases": ["worker"]}}, "restart": "unless-stopped",
        "volumes": [{"type": "bind", "source": "/synthetic/data", "target": "/data", "read_only": True}],
    }
    composition = {"name": "synthetic", "services": {"worker": service, "api": {"image": "api:untouched"}}, "networks": {"application": {"name": "synthetic-network"}}}
    image = {"Id": LIVE_IMAGE, "Config": {"Env": ["PATH=/synthetic/bin"], "Entrypoint": None, "Cmd": ["default"], "User": "", "WorkingDir": ""}}
    runtime = {
        "Image": LIVE_IMAGE,
        "Config": {"Image": "worker:captured", "Env": ["PATH=/synthetic/bin", "AGENT_ENABLED=true", "SECRET=synthetic-secret"], "Cmd": service["command"], "Entrypoint": None, "Healthcheck": {"Test": ["CMD", "health"], "Interval": 30000000000, "Timeout": 5000000000, "Retries": 3}, "User": "", "WorkingDir": ""},
        "HostConfig": {"RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0}, "PortBindings": {}, "ReadonlyRootfs": False, "Privileged": False},
        "NetworkSettings": {"Networks": {"synthetic-network": {"Aliases": ["worker", "runtime-alias"]}}},
        "Mounts": [{"Destination": "/data", "Type": "bind", "Source": "/synthetic/data", "RW": False}],
    }
    return composition, runtime, image


def test_runtime_projection_matches_image_defaults_without_exporting_values():
    composition, runtime, image = fixtures()
    result = baseline_checks(composition, runtime, image, service="worker")
    assert result["all_match"] and result["mismatched_dimensions"] == []
    assert "synthetic-secret" not in json.dumps(result)
    assert "/synthetic" not in json.dumps(result)


@pytest.mark.parametrize("mutation,dimension", [
    ("environment", "environment_including_image_defaults"),
    ("command", "command"), ("health", "healthcheck"), ("restart", "restart"),
    ("network", "networks_and_declared_aliases"), ("mount", "mounts"),
    ("ports", "no_published_ports"), ("privileged", "privileged"),
])
def test_runtime_drift_fails_closed_without_dumping_state(mutation, dimension):
    composition, runtime, image = fixtures()
    if mutation == "environment":
        runtime["Config"]["Env"].append("UNAPPROVED=true")
    if mutation == "command":
        runtime["Config"]["Cmd"] = ["different"]
    if mutation == "health":
        runtime["Config"]["Healthcheck"]["Timeout"] = 1
    if mutation == "restart":
        runtime["HostConfig"]["RestartPolicy"]["Name"] = "always"
    if mutation == "network":
        runtime["NetworkSettings"]["Networks"]["unexpected"] = {}
    if mutation == "mount":
        runtime["Mounts"][0]["RW"] = True
    if mutation == "ports":
        runtime["HostConfig"]["PortBindings"] = {"9999/tcp": []}
    if mutation == "privileged":
        runtime["HostConfig"]["Privileged"] = True
    result = baseline_checks(composition, runtime, image, service="worker")
    assert not result["all_match"] and dimension in result["mismatched_dimensions"]


def test_explicit_empty_entrypoint_and_null_command_keep_compose_semantics():
    composition, runtime, image = fixtures()
    composition["services"]["worker"]["entrypoint"] = []
    composition["services"]["worker"]["command"] = None
    image["Config"]["Entrypoint"] = ["image-entrypoint"]
    runtime["Config"]["Entrypoint"] = []
    runtime["Config"]["Cmd"] = image["Config"]["Cmd"]
    assert baseline_checks(composition, runtime, image, service="worker")["all_match"]


def test_missing_mount_permission_is_not_assumed_to_match():
    composition, runtime, image = fixtures()
    runtime["Mounts"][0].pop("RW")
    assert "mounts" in baseline_checks(composition, runtime, image, service="worker")["mismatched_dimensions"]


def test_proposal_and_rollback_preserve_every_other_service_and_setting():
    composition, runtime, _ = fixtures()
    before = deepcopy(composition)
    models = plan_worker_models(composition, runtime, visitor_overlay(), service="worker", candidate_image_ref=NEW_IMAGE, candidate_source_sha=CANDIDATE_SHA)
    assert composition == before
    assert models["proposed"]["services"]["worker"]["image"] == NEW_IMAGE
    assert models["rollback"]["services"]["worker"]["image"] == LIVE_IMAGE
    assert DEFAULT_BLUEPRINT_KEY not in models["rollback"]["services"]["worker"]["environment"]
    assert VISITOR_ONLY_KEY not in models["rollback"]["services"]["worker"]["environment"]
    assert models["proposed"]["services"]["worker"]["environment"][VISITOR_ONLY_KEY] == "true"
    assert all(models["proposed"]["services"]["worker"]["environment"][key] == CANDIDATE_SHA for key in PROVENANCE_KEYS)
    assert all(key not in models["rollback"]["services"]["worker"]["environment"] for key in PROVENANCE_KEYS)
    assert models["proposed"]["services"]["api"] == before["services"]["api"]
    assert models["review"]["applies_changes"] is False
    assert models["review"]["candidate_image_bytes_verified"] is False


def test_rollback_preserves_an_existing_default_instead_of_assuming_absence():
    composition, runtime, _ = fixtures()
    composition["services"]["worker"]["environment"][DEFAULT_BLUEPRINT_KEY] = "old-reference"
    composition["services"]["worker"]["environment"][VISITOR_ONLY_KEY] = "false"
    composition["services"]["worker"]["environment"].update({key: "old-provenance" for key in PROVENANCE_KEYS})
    models = plan_worker_models(composition, runtime, visitor_overlay("new-reference"), service="worker", candidate_image_ref=NEW_IMAGE, candidate_source_sha=CANDIDATE_SHA)
    assert models["rollback"]["services"]["worker"]["environment"][DEFAULT_BLUEPRINT_KEY] == "old-reference"
    assert models["rollback"]["services"]["worker"]["environment"][VISITOR_ONLY_KEY] == "false"
    assert all(models["rollback"]["services"]["worker"]["environment"][key] == "old-provenance" for key in PROVENANCE_KEYS)


@pytest.mark.parametrize("overlay,image,reason", [
    ({**visitor_overlay("new"), "EXTERNAL_DELIVERY_ENABLED": "true"}, NEW_IMAGE, "OVERLAY_SCOPE_INVALID"),
    (visitor_overlay(""), NEW_IMAGE, "BLUEPRINT_REFERENCE_INVALID"),
    (visitor_overlay("new"), "worker:mutable", "IMAGE_NOT_PINNED"),
    ({DEFAULT_BLUEPRINT_KEY: "new"}, NEW_IMAGE, "OVERLAY_SCOPE_INVALID"),
    ({DEFAULT_BLUEPRINT_KEY: "new", VISITOR_ONLY_KEY: "false"}, NEW_IMAGE, "VISITOR_GUARD_REQUIRED"),
])
def test_plan_rejects_unapproved_gates_or_unpinned_image(overlay, image, reason):
    composition, runtime, _ = fixtures()
    with pytest.raises(ValueError, match="COMPOSITION_" + reason):
        plan_worker_models(composition, runtime, overlay, service="worker", candidate_image_ref=image, candidate_source_sha=CANDIDATE_SHA)


@pytest.mark.parametrize("source_sha", ["short", "c" * 39, "C" * 40, "c" * 41])
def test_candidate_source_requires_exact_lowercase_commit_sha(source_sha):
    composition, runtime, _ = fixtures()
    with pytest.raises(ValueError, match="COMPOSITION_SOURCE_SHA_INVALID"):
        plan_worker_models(composition, runtime, visitor_overlay(), service="worker", candidate_image_ref=NEW_IMAGE, candidate_source_sha=source_sha)


@pytest.mark.parametrize("key", PROVENANCE_KEYS)
def test_candidate_provenance_cannot_disagree_with_certified_source(key):
    baseline, runtime, _ = fixtures()
    models = plan_worker_models(baseline, runtime, visitor_overlay(), service="worker", candidate_image_ref=NEW_IMAGE, candidate_source_sha=CANDIDATE_SHA)
    models["proposed"]["services"]["worker"]["environment"][key] = "d" * 40
    with pytest.raises(ValueError, match="COMPOSITION_PROVENANCE_MISMATCH"):
        verify_worker_delta(baseline, models["proposed"], service="worker", candidate_source_sha=CANDIDATE_SHA)


def test_independent_model_drift_is_rejected():
    baseline, _, _ = fixtures()
    changed = deepcopy(baseline)
    changed["services"]["api"]["image"] = "api:changed"
    with pytest.raises(ValueError, match="COMPOSITION_UNAPPROVED_DELTA"):
        verify_worker_delta(baseline, changed, service="worker")


def test_private_output_refuses_symlink_and_preserves_existing_file(tmp_path):
    target = tmp_path / "existing.json"
    target.write_text("existing")
    alias = tmp_path / "alias.json"
    alias.symlink_to(target)
    with pytest.raises(FileExistsError):
        _private_write(alias, {"replacement": True})
    with pytest.raises(FileExistsError):
        _private_write(target, {"replacement": True})
    assert target.read_text() == "existing"


def test_private_output_permissions(tmp_path):
    target = tmp_path / "private.json"
    _private_write(target, {"synthetic": True})
    assert target.stat().st_mode & 0o777 == 0o600
