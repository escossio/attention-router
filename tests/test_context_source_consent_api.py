"""Laboratory route registration and request-boundary checks."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from attention_router.api.v1.context_source_consent import mount_context_source_consent_for_test
from attention_router.application.client_context import ClientContextReader, ClientContextScope
from attention_router.application.context_pg_consent import PgSourceConsentWriter
from attention_router.config import Settings, settings

GRANT = "/api/v1/context/source-consent/grant"
REVOKE = "/api/v1/context/source-consent/revoke"


@pytest.fixture
def writer(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "test")
    scope = ClientContextScope("tenant", "owner", "binding", "account", datetime.now(UTC), "resource")
    reader = ClientContextReader(
        SimpleNamespace(begin=lambda: None), role="fixture",
        client_sessions=SimpleNamespace(authenticated_bootstrap=lambda: None),
        scope_registry={"ctx_" + "1" * 32: scope}, consent=None, epoch=None, read=None,
    )
    return PgSourceConsentWriter(reader)


@pytest.mark.parametrize(("env", "opt_in", "sessions"), [
    ("production", True, True), ("development", True, True),
    ("test", False, True), ("test", True, False),
])
def test_gate_denies_registration(writer, monkeypatch, env, opt_in, sessions):
    monkeypatch.setattr(settings, "app_env", env)
    monkeypatch.setattr(settings, "context_source_consent_api_enabled", opt_in)
    monkeypatch.setattr(settings, "client_session_enabled", sessions)
    app = FastAPI()
    assert not mount_context_source_consent_for_test(app, writer=writer)
    assert {GRANT, REVOKE}.isdisjoint(route.path for route in app.routes)


def test_gate_defaults_off():
    assert Settings.model_fields["context_source_consent_api_enabled"].default is False


def test_gate_requires_writer_and_rechecks_on_request(writer, monkeypatch):
    monkeypatch.setattr(settings, "context_source_consent_api_enabled", True)
    monkeypatch.setattr(settings, "client_session_enabled", True)
    app = FastAPI()
    assert not mount_context_source_consent_for_test(app, writer=None)
    assert mount_context_source_consent_for_test(app, writer=writer)
    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.post(REVOKE)).status_code == 401
            invalid = await client.post(
                REVOKE, headers={"Authorization": "Bearer synthetic"},
                json={"scope_ref": "ctx_" + "1" * 32, "message_id": "synthetic", "tenant_id": "forged"},
            )
            assert invalid.status_code == 422 and invalid.content == b""
            monkeypatch.setattr(settings, "app_env", "production")
            assert (await client.post(REVOKE)).status_code == 404

    asyncio.run(exercise())
