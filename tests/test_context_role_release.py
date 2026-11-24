"""Role release is scope-bound, explicit, and off outside production."""

from pathlib import Path
import runpy

import pytest

from attention_router.config import settings


ROOT = Path(__file__).resolve().parents[1]
roles = runpy.run_path(str(ROOT / "ops/context-canary/release_roles.py"))


def test_role_identifiers_are_fixed_to_entire_scope():
    names = roles["role_names"](
        tenant="tenant-a", owner="owner-a", account="account-a",
    )
    assert len(names) == 2 and names[0] != names[1]
    assert all(name.startswith("andy_ctx_") and len(name) == 24 for name in names)
    assert names == roles["role_names"](
        tenant="tenant-a", owner="owner-a", account="account-a",
    )
    for other in (
        dict(tenant="tenant-b", owner="owner-a", account="account-a"),
        dict(tenant="tenant-a", owner="owner-b", account="account-a"),
        dict(tenant="tenant-a", owner="owner-a", account="account-b"),
    ):
        assert names != roles["role_names"](**other)


def test_role_release_refuses_nonproduction_without_database_access(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "test")
    with pytest.raises(RuntimeError, match="REQUIRES_PRODUCTION_MODE"):
        roles["run"](
            "check", root=ROOT, acl_path=ROOT / "missing-acl.json",
            catalog_path=ROOT / "missing-catalog.json",
            scope_path=ROOT / "missing-scope.json", approval_path=None,
        )


def test_role_release_requires_an_operator_window_for_changes(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.delenv("ANDY_ROLES_RELEASE_EXPLICIT", raising=False)
    with pytest.raises(RuntimeError, match="OPERATOR_WINDOW_NOT_OPEN"):
        roles["run"](
            "apply", root=ROOT, acl_path=ROOT / "missing-acl.json",
            catalog_path=ROOT / "missing-catalog.json",
            scope_path=ROOT / "missing-scope.json", approval_path=None,
        )
