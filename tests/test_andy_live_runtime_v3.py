"""Disposable lifecycle tests: no real Docker command or production file access."""

import json

import pytest

from scripts.operations import andy_live_runtime_v3 as lifecycle


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    paths = []
    for role in lifecycle.ROLES:
        path = tmp_path / f"{role}.yaml"
        path.write_text(f"# synthetic {role}\n")
        path.chmod(0o644)
        paths.append(path)
    env = {key: "opaque" for key in lifecycle.CRITICAL}
    for key in ("AGENT_DECISION_DEFAULT_VISITOR_ONLY", "GROUP_CONTEXTUAL_PRESENCE_ENABLED",
                "CLIENT_SESSION_ENABLED", "AGENT_EXECUTION_ENABLED", "ANDY_AGENT_ENABLED"):
        env[key] = "true"
    env["ATTENTION_ROUTER_BUILD_SHA"] = env["RUNTIME_HEAD"] = "a" * 40
    env["FIRST_CONTACT_ENABLED"] = "false"
    env["FIRST_CONTACT_TENANT_ID"] = ""
    env["FIRST_CONTACT_SOURCE_ACCOUNT"] = ""
    rendered = {"services": {"worker": {"image": "sha256:" + "b" * 64,
                                         "environment": env, "restart": "unless-stopped"}}}
    manifest = {
        "schema_version": "andy-live-worker-lifecycle-v3", "first_contact_mode": "off", "project": "synthetic",
        "workdir": str(tmp_path), "worker_container": "synthetic-worker",
        "db_container": "synthetic-db", "service": "worker",
        "compose_files": [{"role": role, "path": str(path), "sha256": lifecycle.digest(path.read_bytes())}
                          for role, path in zip(lifecycle.ROLES, paths)],
        "render_sha256": lifecycle.digest(lifecycle.canonical(rendered)),
        "image_id": "sha256:" + "b" * 64, "source_sha": "a" * 40,
        "critical_environment_sha256": {key: lifecycle.digest(value.encode()) for key, value in env.items()},
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    manifest_path.chmod(0o600)
    settings = {
        "ATTENTION_LIVE_WORKER_MANIFEST": str(manifest_path),
        "ATTENTION_LIVE_WORKER_MANIFEST_SHA256": lifecycle.digest(manifest_path.read_bytes()),
        "ATTENTION_LIVE_WORKER_COMPOSE_FILES": ":".join(map(str, paths)),
        "ATTENTION_LIVE_PROJECT": "synthetic", "ATTENTION_LIVE_WORKDIR": str(tmp_path),
        "ATTENTION_LIVE_WORKER_CONTAINER": "synthetic-worker", "ATTENTION_LIVE_DB_CONTAINER": "synthetic-db",
        "ATTENTION_LIVE_WORKER_SERVICE": "worker",
    }
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    calls = []
    def fake_invoke(args, *, cwd=None):
        calls.append(args)
        assert args[-3:] == ["config", "--format", "json"]
        return json.dumps(rendered)
    def fake_inspect(kind, name):
        assert kind == "image"
        return {"Id": "sha256:" + "b" * 64,
                "Config": {"Labels": {"org.opencontainers.image.revision": "a" * 40}}}
    monkeypatch.setattr(lifecycle, "invoke", fake_invoke)
    monkeypatch.setattr(lifecycle, "inspect", fake_inspect)
    return manifest, manifest_path, rendered, calls, paths


def repin(scenario, monkeypatch):
    manifest, path, *_ = scenario
    path.write_text(json.dumps(manifest))
    monkeypatch.setenv("ATTENTION_LIVE_WORKER_MANIFEST_SHA256", lifecycle.digest(path.read_bytes()))


def test_complete_six_input_preflight(scenario):
    manifest, rendered, image, command = lifecycle.preflight()
    assert len(manifest["compose_files"]) == 6
    assert rendered["services"]["worker"]["environment"]["AGENT_DECISION_DEFAULT_VISITOR_ONLY"] == "true"
    assert command.count("-f") == 6
    assert image["Id"] == manifest["image_id"]


def test_pinned_v2_target_is_off_without_first_contact_fields(scenario, monkeypatch):
    manifest, _, rendered, _, _ = scenario
    manifest["schema_version"] = "andy-live-worker-lifecycle-v2"
    manifest.pop("first_contact_mode")
    env = rendered["services"]["worker"]["environment"]
    for key in ("FIRST_CONTACT_ENABLED", "FIRST_CONTACT_TENANT_ID", "FIRST_CONTACT_SOURCE_ACCOUNT"):
        env.pop(key)
        manifest["critical_environment_sha256"].pop(key)
    manifest["render_sha256"] = lifecycle.digest(lifecycle.canonical(rendered))
    repin(scenario, monkeypatch)
    assert lifecycle.preflight()[0]["schema_version"] == "andy-live-worker-lifecycle-v2"
    env["FIRST_CONTACT_ENABLED"] = "true"
    manifest["render_sha256"] = lifecycle.digest(lifecycle.canonical(rendered))
    repin(scenario, monkeypatch)
    with pytest.raises(lifecycle.InvariantError, match="LEGACY_FIRST_CONTACT_FIELDS_INVALID"):
        lifecycle.preflight()


def test_v2_target_rejects_on_claim_and_scope(scenario, monkeypatch):
    manifest, _, rendered, _, _ = scenario
    manifest["schema_version"] = "andy-live-worker-lifecycle-v2"
    for key in ("FIRST_CONTACT_ENABLED", "FIRST_CONTACT_TENANT_ID", "FIRST_CONTACT_SOURCE_ACCOUNT"):
        manifest["critical_environment_sha256"].pop(key)
    manifest["first_contact_mode"] = "on"
    repin(scenario, monkeypatch)
    with pytest.raises(lifecycle.InvariantError, match="LEGACY_FIRST_CONTACT_FIELDS_INVALID"):
        lifecycle.preflight()
    manifest.pop("first_contact_mode")
    rendered["services"]["worker"]["environment"]["FIRST_CONTACT_TENANT_ID"] = "synthetic-tenant"
    manifest["render_sha256"] = lifecycle.digest(lifecycle.canonical(rendered))
    repin(scenario, monkeypatch)
    with pytest.raises(lifecycle.InvariantError, match="LEGACY_FIRST_CONTACT_FIELDS_INVALID"):
        lifecycle.preflight()


def test_v2_off_rollback_target_accepts_v3_on_source(scenario, monkeypatch):
    manifest, _, rendered, _, _ = scenario
    manifest["schema_version"] = "andy-live-worker-lifecycle-v2"
    manifest.pop("first_contact_mode")
    env = rendered["services"]["worker"]["environment"]
    for key in ("FIRST_CONTACT_ENABLED", "FIRST_CONTACT_TENANT_ID", "FIRST_CONTACT_SOURCE_ACCOUNT"):
        env.pop(key)
        manifest["critical_environment_sha256"].pop(key)
    manifest["render_sha256"] = lifecycle.digest(lifecycle.canonical(rendered))
    repin(scenario, monkeypatch)
    target = lifecycle.preflight()
    source_manifest = dict(target[0], schema_version="andy-live-worker-lifecycle-v3", first_contact_mode="on")
    source = (source_manifest, {"services": {"worker": "on"}}, {"Id": source_manifest["image_id"]}, ["source"])
    calls = []
    monkeypatch.setattr(lifecycle, "verify", lambda item, *_args, **_kwargs: calls.append(("verify", item["schema_version"])))
    monkeypatch.setattr(lifecycle, "preflight", lambda *, previous=False: source if previous else target)
    monkeypatch.setattr(lifecycle, "invoke", lambda args, **_kwargs: calls.append(("invoke", args)) or "")
    monkeypatch.setattr(lifecycle, "wait_healthy", lambda *_args: calls.append(("healthy", None)))
    lifecycle.reconcile(*target, previous=source)
    assert calls[0] == ("verify", "andy-live-worker-lifecycle-v3")
    assert calls[-1] == ("verify", "andy-live-worker-lifecycle-v2")


def test_on_mode_requires_scoped_tenant_and_account(scenario, monkeypatch):
    manifest, _, rendered, _, _ = scenario
    env = rendered["services"]["worker"]["environment"]
    manifest["first_contact_mode"] = "on"
    env["FIRST_CONTACT_ENABLED"] = "true"
    manifest["critical_environment_sha256"]["FIRST_CONTACT_ENABLED"] = lifecycle.digest(b"true")
    manifest["render_sha256"] = lifecycle.digest(lifecycle.canonical(rendered))
    repin(scenario, monkeypatch)
    with pytest.raises(lifecycle.InvariantError, match="FIRST_CONTACT_SCOPE_MISSING"):
        lifecycle.preflight()
    for key, value in (("FIRST_CONTACT_TENANT_ID", "synthetic-tenant"),
                       ("FIRST_CONTACT_SOURCE_ACCOUNT", "synthetic-account")):
        env[key] = value
        manifest["critical_environment_sha256"][key] = lifecycle.digest(value.encode())
    manifest["render_sha256"] = lifecycle.digest(lifecycle.canonical(rendered))
    repin(scenario, monkeypatch)
    with pytest.raises(lifecycle.InvariantError, match="FIRST_CONTACT_BOOT_SCOPE_DRIFT"):
        lifecycle.preflight()
    monkeypatch.setenv("ATTENTION_LIVE_FIRST_CONTACT_TENANT_ID", "synthetic-tenant")
    monkeypatch.setenv("ATTENTION_LIVE_FIRST_CONTACT_SOURCE_ACCOUNT", "synthetic-account")
    assert lifecycle.preflight()[0]["first_contact_mode"] == "on"


def test_on_mode_cannot_be_enabled_by_render_alone(scenario, monkeypatch):
    manifest, _, rendered, _, _ = scenario
    rendered["services"]["worker"]["environment"]["FIRST_CONTACT_ENABLED"] = "true"
    manifest["critical_environment_sha256"]["FIRST_CONTACT_ENABLED"] = lifecycle.digest(b"true")
    manifest["render_sha256"] = lifecycle.digest(lifecycle.canonical(rendered))
    repin(scenario, monkeypatch)
    with pytest.raises(lifecycle.InvariantError, match="FIRST_CONTACT_MODE_DRIFT"):
        lifecycle.preflight()


def test_image_reference_and_revision_must_match_pin(scenario, monkeypatch):
    manifest, _, rendered, _, _ = scenario
    rendered["services"]["worker"]["image"] = "mutable:latest"
    manifest["render_sha256"] = lifecycle.digest(lifecycle.canonical(rendered))
    repin(scenario, monkeypatch)
    with pytest.raises(lifecycle.InvariantError, match="IMAGE_REFERENCE_UNPINNED"):
        lifecycle.preflight()
    rendered["services"]["worker"]["image"] = manifest["image_id"]
    manifest["render_sha256"] = lifecycle.digest(lifecycle.canonical(rendered))
    repin(scenario, monkeypatch)
    monkeypatch.setattr(lifecycle, "inspect", lambda *_: {
        "Id": manifest["image_id"],
        "Config": {"Labels": {"org.opencontainers.image.revision": "c" * 40}},
    })
    with pytest.raises(lifecycle.InvariantError, match="IMAGE_REVISION_DRIFT"):
        lifecycle.preflight()


def test_transition_accepts_pinned_v2_source_and_checks_before_write(scenario, monkeypatch):
    manifest, _, rendered, _, paths = scenario
    previous = json.loads(json.dumps(manifest))
    previous["schema_version"] = "andy-live-worker-lifecycle-v2"
    previous.pop("first_contact_mode")
    for key in ("FIRST_CONTACT_ENABLED", "FIRST_CONTACT_TENANT_ID", "FIRST_CONTACT_SOURCE_ACCOUNT"):
        previous["critical_environment_sha256"].pop(key)
    source_path = manifest["workdir"] + "/previous.json"
    with open(source_path, "w", encoding="utf-8") as handle:
        json.dump(previous, handle)
    import os
    os.chmod(source_path, 0o600)
    monkeypatch.setenv("ATTENTION_LIVE_WORKER_MANIFEST_PREVIOUS", source_path)
    with open(source_path, "rb") as handle:
        monkeypatch.setenv("ATTENTION_LIVE_WORKER_MANIFEST_SHA256_PREVIOUS", lifecycle.digest(handle.read()))
    target_paths = []
    for path, row in zip(paths, manifest["compose_files"]):
        target = path.with_name("target-" + path.name)
        target.write_bytes(path.read_bytes())
        target.chmod(0o644)
        row["path"] = str(target)
        target_paths.append(target)
    monkeypatch.setenv("ATTENTION_LIVE_WORKER_COMPOSE_FILES", ":".join(map(str, target_paths)))
    repin(scenario, monkeypatch)
    assert lifecycle.preflight(previous=True)[0]["schema_version"] == "andy-live-worker-lifecycle-v2"
    target = lifecycle.preflight()
    source = lifecycle.preflight(previous=True)
    calls = []
    monkeypatch.setattr(lifecycle, "verify", lambda *args, **kwargs: calls.append("verify"))
    monkeypatch.setattr(lifecycle, "invoke", lambda args, **kwargs: calls.append(args) or
                        (json.dumps(rendered) if args[-3:] == ["config", "--format", "json"] else ""))
    monkeypatch.setattr(lifecycle, "wait_healthy", lambda *args: calls.append("healthy"))
    lifecycle.reconcile(*target, previous=source)
    assert calls[0] == "verify"
    assert calls[1][:2] == ["docker", "update"]
    assert sum(isinstance(call, list) and "up" in call for call in calls) == 1
    assert calls[-1] == "verify"


@pytest.mark.parametrize("mutation,code", [
    ("drop_groups", "COMPOSE_INPUT_COUNT_INVALID"),
    ("swap_groups", "COMPOSE_INPUT_ORDER_INVALID"),
    ("wrong_image", "IMAGE_DRIFT"),
    ("wrong_render", "RENDER_DRIFT"),
    ("wrong_source", "SOURCE_PROVENANCE_DRIFT"),
])
def test_manifest_drift_fails_before_write(scenario, monkeypatch, mutation, code):
    manifest, path, rendered, calls, files = scenario
    if mutation == "drop_groups":
        manifest["compose_files"].pop(4)
    elif mutation == "swap_groups":
        manifest["compose_files"][4:6] = reversed(manifest["compose_files"][4:6])
    elif mutation == "wrong_image":
        manifest["image_id"] = "sha256:" + "c" * 64
    elif mutation == "wrong_render":
        manifest["render_sha256"] = "c" * 64
    elif mutation == "wrong_source":
        manifest["source_sha"] = "c" * 40
    repin(scenario, monkeypatch)
    with pytest.raises(lifecycle.InvariantError, match=code):
        lifecycle.preflight()
    assert not any("up" in args or "start" in args or "update" in args for args in calls)


def test_modified_overlay_and_manifest_pin_fail_closed(scenario, monkeypatch):
    _, _, _, calls, files = scenario
    files[5].write_text("# changed\n")
    with pytest.raises(lifecycle.InvariantError, match="COMPOSE_INPUT_DRIFT"):
        lifecycle.preflight()
    monkeypatch.setenv("ATTENTION_LIVE_WORKER_MANIFEST_SHA256", "0" * 64)
    with pytest.raises(lifecycle.InvariantError, match="MANIFEST_DRIFT"):
        lifecycle.preflight()
    assert calls == []


def test_group_writable_compose_input_fails_closed(scenario):
    _, _, _, calls, files = scenario
    files[1].chmod(0o664)
    with pytest.raises(lifecycle.InvariantError, match="INPUT_PERMISSIONS_INVALID"):
        lifecycle.preflight()
    assert calls == []


def test_wrong_boot_file_list_fails_closed(scenario, monkeypatch):
    _, _, _, calls, files = scenario
    monkeypatch.setenv("ATTENTION_LIVE_WORKER_COMPOSE_FILES", ":".join(map(str, files[:4])))
    with pytest.raises(lifecycle.InvariantError, match="COMPOSE_BOOT_INPUT_DRIFT"):
        lifecycle.preflight()
    assert calls == []


@pytest.mark.parametrize("key,value,code", [
    ("AGENT_DECISION_DEFAULT_VISITOR_ONLY", "false", "VISITOR_GUARD_DISABLED"),
    ("GROUP_CONTEXTUAL_PRESENCE_ENABLED", "false", "REQUIRED_CAPABILITY_DISABLED"),
    ("CLIENT_SESSION_ENABLED", "false", "REQUIRED_CAPABILITY_DISABLED"),
    ("GROUP_CONTEXTUAL_PRESENCE_GROUP_ID", "", "GROUP_SCOPE_MISSING"),
])
def test_approved_render_cannot_drop_recovery_controls(scenario, monkeypatch, key, value, code):
    manifest, _, rendered, _, _ = scenario
    rendered["services"]["worker"]["environment"][key] = value
    manifest["critical_environment_sha256"][key] = lifecycle.digest(value.encode())
    manifest["render_sha256"] = lifecycle.digest(lifecycle.canonical(rendered))
    repin(scenario, monkeypatch)
    with pytest.raises(lifecycle.InvariantError, match=code):
        lifecycle.preflight()


def test_reconcile_preflight_failure_never_mutates(scenario, monkeypatch):
    _, _, _, calls, _ = scenario
    monkeypatch.setenv("ATTENTION_LIVE_WORKER_MANIFEST_SHA256", "0" * 64)
    monkeypatch.setattr("sys.argv", ["andy_live_runtime_v2", "--check"])
    assert lifecycle.main() == 1
    assert calls == []


def test_boot_preflight_accepts_stopped_containers_without_accepting_drift(scenario, monkeypatch):
    manifest, _, rendered, _, _ = scenario
    worker = {
        "Config": {"Labels": {"com.docker.compose.project.config_files": ",".join(
            row["path"] for row in manifest["compose_files"])}},
        "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
        "State": {"Status": "exited"},
    }
    db = {"HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
          "State": {"Status": "exited"}}
    monkeypatch.setattr(lifecycle, "inspect", lambda kind, name: worker if name == "synthetic-worker" else db)
    monkeypatch.setattr(lifecycle, "baseline_checks", lambda *_args, **_kwargs: {"all_match": True})
    lifecycle.verify(manifest, rendered, {"Id": manifest["image_id"]}, require_running=False)
    with pytest.raises(lifecycle.InvariantError, match="SERVICE_UNHEALTHY"):
        lifecycle.verify(manifest, rendered, {"Id": manifest["image_id"]})
    worker["Config"]["Labels"]["com.docker.compose.project.config_files"] = "only-four"
    with pytest.raises(lifecycle.InvariantError, match="RUNNING_COMPOSE_INPUT_DRIFT"):
        lifecycle.verify(manifest, rendered, {"Id": manifest["image_id"]}, require_running=False)


def test_explicit_health_required_for_running_worker_and_db(scenario, monkeypatch):
    manifest, _, rendered, _, _ = scenario
    worker = {"Config": {"Labels": {"com.docker.compose.project.config_files": ",".join(
        row["path"] for row in manifest["compose_files"])}},
        "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
        "State": {"Status": "running"}}
    db = {"HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
          "State": {"Status": "running", "Health": {"Status": "healthy"}}}
    monkeypatch.setattr(lifecycle, "inspect", lambda kind, name: worker if name == "synthetic-worker" else db)
    monkeypatch.setattr(lifecycle, "baseline_checks", lambda *_args, **_kwargs: {"all_match": True})
    with pytest.raises(lifecycle.InvariantError, match="SERVICE_UNHEALTHY"):
        lifecycle.verify(manifest, rendered, {"Id": manifest["image_id"]})
    worker["State"]["Health"] = {"Status": "healthy"}
    lifecycle.verify(manifest, rendered, {"Id": manifest["image_id"]})


def test_wait_healthy_rejects_missing_healthcheck(monkeypatch):
    polls = []
    monkeypatch.setattr(lifecycle, "inspect", lambda *_: polls.append(True) or {"State": {"Status": "running"}})
    monkeypatch.setattr(lifecycle.time, "sleep", lambda _: None)
    with pytest.raises(lifecycle.InvariantError, match="SERVICE_HEALTH_TIMEOUT"):
        lifecycle.wait_healthy("synthetic-worker", 0.01)
    assert polls


def test_rollback_direction_verifies_on_source_before_off_target(scenario, monkeypatch):
    target = lifecycle.preflight()
    source_manifest = json.loads(json.dumps(target[0]))
    source_manifest["first_contact_mode"] = "on"
    source_manifest["image_id"] = "sha256:" + "c" * 64
    source = (source_manifest, {"services": {"worker": "on"}}, {"Id": source_manifest["image_id"]}, ["source"])
    calls = []
    monkeypatch.setattr(lifecycle, "verify", lambda manifest, *_args, **_kwargs: calls.append(("verify", manifest["first_contact_mode"])))
    monkeypatch.setattr(lifecycle, "preflight", lambda *, previous=False: source if previous else target)
    monkeypatch.setattr(lifecycle, "invoke", lambda args, **_kwargs: calls.append(("invoke", args)) or "")
    monkeypatch.setattr(lifecycle, "wait_healthy", lambda name, seconds: calls.append(("healthy", name)))
    lifecycle.reconcile(*target, previous=source)
    assert calls[0] == ("verify", "on")
    assert calls[-1] == ("verify", "off")
    assert sum("up" in args for kind, args in calls if kind == "invoke") == 1


def test_target_drift_after_db_start_prevents_worker_recreate(scenario, monkeypatch):
    target = lifecycle.preflight()
    calls = []
    monkeypatch.setattr(lifecycle, "verify", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(lifecycle, "invoke", lambda args, **_kwargs: calls.append(args) or "")
    monkeypatch.setattr(lifecycle, "wait_healthy", lambda *_args: None)
    changed = dict(target[0], image_id="sha256:" + "c" * 64)
    monkeypatch.setattr(lifecycle, "preflight", lambda **_kwargs: (changed, *target[1:]))
    with pytest.raises(lifecycle.InvariantError, match="TARGET_CHANGED_BEFORE_UP"):
        lifecycle.reconcile(*target)
    assert not any("up" in args for args in calls)
