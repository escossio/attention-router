"""Private production-mode context gate tests. No real identity or data."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route

from attention_router.api.v1.client_context_staged import StagedContextEndpoint
from attention_router.application.context_canary_gate import canary_configured, canary_scope
from attention_router.config import settings


def enable(monkeypatch):
    for field, value in {
        "app_env": "production",
        "staged_context_private_canary_enabled": True,
        "client_session_enabled": True,
        "context_epoch_authority_canary_enabled": True,
        "cognitive_runtime_canary_tenant_id": "synthetic-tenant",
        "persistent_memory_canary_binding_id": "synthetic-binding",
        "staged_context_canary_owner_id": "synthetic-owner",
        "staged_context_canary_source_account": "synthetic-account",
    }.items():
        monkeypatch.setattr(settings, field, value)


def test_gate_is_off_by_default_and_requires_exact_scope(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "staged_context_private_canary_enabled", False)
    assert not canary_configured()
    enable(monkeypatch)
    assert canary_configured()
    assert canary_scope(
        tenant_id="synthetic-tenant", binding_id="synthetic-binding",
        owner_id="synthetic-owner", source_account="synthetic-account",
    )
    for field in ("tenant_id", "binding_id", "owner_id", "source_account"):
        values = dict(
            tenant_id="synthetic-tenant", binding_id="synthetic-binding",
            owner_id="synthetic-owner", source_account="synthetic-account",
        )
        values[field] = "other"
        assert not canary_scope(**values)
    monkeypatch.setattr(settings, "staged_context_canary_source_account", None)
    assert not canary_configured()


def test_production_endpoint_refuses_remote_network_even_when_flagged(monkeypatch):
    enable(monkeypatch)
    fake = SimpleNamespace(query_and_deliver=lambda **kwargs: pytest.fail("must_not_disclose"))
    app = Starlette(routes=[Route(
        "/api/v1/client/context/staged-evidence",
        StagedContextEndpoint(fake), methods=["POST"],
    )])

    async def exercise():
        transport = httpx.ASGITransport(app=app, client=("198.51.100.2", 33333))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:18099") as client:
            response = await client.post(
                "/api/v1/client/context/staged-evidence",
                json={"question": "No source", "scope_ref": "ctx_" + "0" * 64},
                headers={"Authorization": "Bearer synthetic"},
            )
        assert response.status_code == 404
        assert response.content == b""
    asyncio.run(exercise())
