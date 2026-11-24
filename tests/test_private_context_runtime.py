import asyncio
import json
from pathlib import Path
import runpy
import subprocess
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
import yaml

from attention_router.api.v1.client_context_staged import StagedContextEndpoint
from attention_router.observability.private_context import query_trace
from attention_router.web import private_context as runtime


def test_default_off_has_no_data_plane(monkeypatch):
    monkeypatch.setattr(runtime.settings, "staged_context_private_canary_enabled", False)
    monkeypatch.setattr(runtime, "_prepare", lambda: pytest.fail("opened authority while OFF"))
    app = runtime.create_app()

    async def probe():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            assert (await client.get("/health/live")).status_code == 200
            ready = await client.get("/health/ready")
            assert ready.status_code == 503 and ready.json()["status"] == "disabled"
            assert (await client.post(runtime.QUERY_PATH)).status_code == 404
            assert (await client.get("/openapi.json")).status_code == 404
    asyncio.run(probe())


def test_private_process_runs_in_production_configuration_only(monkeypatch):
    monkeypatch.setattr(runtime.settings, "app_env", "test")
    monkeypatch.setattr(runtime.settings, "staged_context_private_canary_enabled", True)
    with pytest.raises(ValueError, match="GATE_INCOMPLETE"):
        runtime.create_app()


def test_only_socket_and_expected_reader_are_accepted():
    role = "andy_ctx_rd_" + "a"*12
    raw = f"postgresql+psycopg://{role}@/test?host=/run/postgresql"
    assert runtime._reader_url(raw.encode(), role).username == role
    for value in (raw.replace(role, "other"), raw.replace("@/", "@127.0.0.1/"),
                  raw.replace("/run/postgresql", "/elsewhere"), raw + "&options=arbitrary"):
        with pytest.raises(ValueError):
            runtime._reader_url(value.encode(), role)


def test_grounded_excerpts_and_abstention():
    def item(source, excerpt, heuristic=None):
        return SimpleNamespace(attributes={"excerpts": [{"source_ref": source, "char_start": 0,
            "char_end": len(excerpt), "observed_at": "synthetic", "text": excerpt}]},
            provenance={"single_excerpt_heuristic": heuristic})
    first, second = item("one", "quarta-feira"), item("two", "quinta-feira")
    with query_trace() as trace:
        packet = SimpleNamespace(items=[first, second, first, item("three", "fora do tópico", "QUERY_COVERAGE_INSUFFICIENT")])
        body = runtime.owner_answer(packet)
        assert body["correlation_id"] == trace and body["conclusion"] is None
        assert [row["excerpt"] for row in body["evidence"]] == ["quarta-feira", "quinta-feira"]
        assert runtime.owner_answer(SimpleNamespace(items=[]))["kind"] == "INSUFFICIENT_EVIDENCE"


def test_private_correlation_logs_contain_only_decision(caplog, monkeypatch):
    caplog.set_level("INFO", logger="attention_router.observability.private_context")
    monkeypatch.setattr(runtime.settings, "app_env", "test")
    def refused(**kwargs):
        raise ValueError(kwargs["question"] + kwargs["bearer"] + kwargs["scope_ref"])
    reader = StagedContextEndpoint(SimpleNamespace(query_and_deliver=refused))
    async def probe():
        from starlette.applications import Starlette
        from starlette.routing import Route
        transport = httpx.ASGITransport(app=Starlette(routes=[Route("/query", reader, methods=["POST"])]))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            response = await client.post("/query", json={"question": "segredo sintético", "scope_ref": "ctx_"+"b"*64},
                                         headers={"Authorization": "Bearer synthetic-bearer", "x-correlation-id":"incoming-private"})
            assert response.status_code == 503 and not response.content
            trace = response.headers["x-correlation-id"]
            assert UUID(trace).version == 4 and trace in caplog.text
    asyncio.run(probe())
    assert "DELIVERY_BLOCKED" in caplog.text and "QUERY_REFUSED" in caplog.text
    for value in ("segredo sintético", "synthetic-bearer", "ctx_", "incoming-private"):
        assert value not in caplog.text


def test_bounded_single_owner_encoding(monkeypatch):
    now = [100.]
    monkeypatch.setattr(runtime, "monotonic", lambda: now[0])
    budget = runtime.QueryBudget()
    assert all(budget.reserve(None) for _ in range(32))
    assert not budget.reserve(None)
    now[0] += 60
    assert budget.reserve(None)


def test_oci_boundaries_and_release_refuses_before_execution(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    ops = root / "ops/context-canary"
    compose = yaml.safe_load((ops / "compose.private.yaml").read_text())
    for service in compose["services"].values():
        assert service["network_mode"] == "none" and service["read_only"]
        assert service["user"] == "65532:65532" and service["cap_drop"] == ["ALL"] and "ports" not in service
    assert all(vol["read_only"] and vol["bind"]["create_host_path"] is False for vol in compose["services"]["owner"]["volumes"])
    release = runpy.run_path(str(ops / "private_runtime_release.py"))["run"]
    build = runpy.run_path(str(ops / "build_private_oci.py"))["build"]
    with pytest.raises(ValueError, match="immutable"):
        build(root=root, sha="a"*40, base="image:tag", output=tmp_path/"image.tar")
    with pytest.raises(ValueError, match="immutable"):
        release("canary_enable", 1, "image:latest", root=root)
    operations = []
    def invoke(args, **kw):
        operations.append(args)
        if len(operations) == 1:
            return "PREFLIGHT_APROVADO canary_enable #428 PR #1 " + "a"*40
        return json.dumps([{"RepoDigests": [], "Config": {"Labels": {}}}])
    monkeypatch.setattr(subprocess, "check_output", invoke)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("unverified OCI executed"))
    with pytest.raises(ValueError, match="provenance"):
        release("canary_enable", 1, "ghcr.io/escossio/attention-router@sha256:"+"a"*64, root=root)
    assert len(operations) == 2


@pytest.mark.parametrize(("rows", "expected"), [
    ([("a", "A inspeção do moinho foi aprovada."),
      ("b", "A inspeção do moinho foi recusada.")], ["a", "b"]),
    ([("a", "A inspeção do moinho foi aprovada.")], []),
    ([("a", "A inspeção do moinho foi aprovada."),
      ("a", "A inspeção do moinho foi recusada.")], []),
    ([("a", "A inspeção do moinho foi aprovada."),
      ("b", "A inspeção da caldeira do moinho foi recusada.")], []),
    ([("a", "A inspeção do moinho foi aprovada."),
      ("b", "A inspeção do moinho foi recusada?")], []),
    ([("a", "A inspeção do moinho foi aprovada."),
      ("b", "A inspeção do moinho não foi recusada.")], []),
])
def test_inspection_contrast_requires_two_explicit_scoped_witnesses(rows, expected):
    from attention_router.application.conversation_relevance import CONTRAST_NEEDS_PAIR

    packet = SimpleNamespace(
        query="Existe referência à verificação contraditória do moinho?",
        items=[
            SimpleNamespace(
                attributes={"excerpts": [{
                    "source_ref": source, "char_start": 0, "char_end": len(excerpt),
                    "observed_at": "synthetic", "text": excerpt,
                }]},
                provenance={"single_excerpt_heuristic": CONTRAST_NEEDS_PAIR},
            )
            for source, excerpt in rows
        ],
    )
    with query_trace():
        response = runtime.owner_answer(packet)
    assert [item["source_ref"] for item in response["evidence"]] == expected
    assert response["conclusion"] is None
    assert response["kind"] == ("OBSERVATION" if expected else "INSUFFICIENT_EVIDENCE")
