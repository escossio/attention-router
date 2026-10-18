"""Disposable lifecycle tests: no real Docker command or production file access."""

import json

import pytest

from scripts.operations import andy_live_runtime_v2 as lifecycle


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
    rendered = {"services": {"worker": {"image": "sha256:" + "b" * 64,
                                         "environment": env, "restart": "unless-stopped"}}}
    manifest = {
        "schema_version": "andy-live-worker-lifecycle-v2", "project": "synthetic",
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
        return {"Id": "sha256:" + "b" * 64}
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
