"""GitHub App publishers prove NOT_APPLICABLE without dispatching workers."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "ops/provisioning/github-app-control-plane"
SHA = "c" * 40
REPO = "escossio/attention-router"


def load(name):
    fake = types.ModuleType("probe")
    fake.CONFIG = Path("/nonexistent")
    fake.INSTALLATION_CONFIG = Path("/nonexistent")
    fake.load_env = lambda _path: {}
    fake.make_jwt = lambda *_a, **_kw: "jwt"
    fake.request_json = lambda *_a, **_kw: (503, {})
    old = sys.modules.get("probe")
    sys.modules["probe"] = fake
    try:
        spec = importlib.util.spec_from_file_location(f"ci_impact_test_{name}", APP / name)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if old is None:
            del sys.modules["probe"]
        else:
            sys.modules["probe"] = old


def api_request(path):
    if path == f"/repos/{REPO}/pulls/19":
        return 200, {
            "head": {"sha": SHA, "repo": {"full_name": REPO}},
            "base": {"ref": "main"}, "state": "open", "changed_files": 1,
        }
    if path == f"/repos/{REPO}/pulls/19/files?per_page=100&page=1":
        return 200, [{"filename": "docs/readme.md", "status": "modified"}]
    raise RuntimeError(f"unexpected request: {path}")


@pytest.mark.parametrize("module_name,suite", [
    ("distributed_shadow_checks.py", "python"),
    ("distributed_shadow_checks.py", "transport"),
    ("distributed_shadow_checks.py", "docker"),
    ("distributed_postgres_check.py", "postgres"),
])
def test_docs_only_publisher_records_not_applicable_without_worker(monkeypatch, module_name, suite):
    mod=load(module_name)
    monkeypatch.setattr(mod, "installation_token", lambda: "token")
    monkeypatch.setattr(mod, "current_pr", lambda *_a: {"state":"open", "head":{"sha":SHA}})
    monkeypatch.setattr(mod, "request_json", lambda path, token: api_request(path))
    monkeypatch.setattr(mod, "create_check", lambda *_a: 987)
    captured=[]
    monkeypatch.setattr(mod, "complete_check", lambda *args: captured.append(args))
    monkeypatch.setattr(mod, "run_command", lambda *_a: pytest.fail("worker must not execute for docs only"))
    if suite == "postgres":
        result=mod.execute_exact_sha(REPO,19,SHA)
    else:
        result=mod.execute_exact_sha(REPO,19,SHA,suite)
    assert result[0]=="NOT_APPLICABLE" and result[2]==987
    assert len(captured)==1 and captured[0][3]=="success"
    assert "NOT APPLICABLE" in captured[0][4]
    assert "CI_IMPACT=NOT_APPLICABLE" in captured[0][5]


@pytest.mark.parametrize("module_name,suite", [
    ("distributed_shadow_checks.py", "python"),
    ("distributed_postgres_check.py", "postgres"),
])
def test_api_unavailable_must_run_suites(monkeypatch, module_name, suite):
    mod=load(module_name)
    monkeypatch.setattr(mod, "installation_token", lambda: "token")
    monkeypatch.setattr(mod, "current_pr", lambda *_a: {"state":"open", "head":{"sha":SHA}})
    monkeypatch.setattr(mod, "request_json", lambda *_a: (_ for _ in ()).throw(OSError("synthetic API outage")))
    monkeypatch.setattr(mod, "create_check", lambda *_a: 987)
    captured=[]
    monkeypatch.setattr(mod, "complete_check", lambda *args: captured.append(args))
    dispatched=[]
    monkeypatch.setattr(mod, "run_command", lambda args: (dispatched.append(args), subprocess.CompletedProcess(args,0,"PASSED"))[1])
    monkeypatch.setattr(mod, "extract_summary", lambda _output: {"status": "PASS", "sha": SHA})
    if suite == "postgres":
        result=mod.execute_exact_sha(REPO,19,SHA)
    else:
        result=mod.execute_exact_sha(REPO,19,SHA,suite)
    assert result[0]=="PASS" and len(dispatched)==1
    assert len(captured)==1 and captured[0][3]=="success"
