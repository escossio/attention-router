import hashlib
import hmac
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy.orm import sessionmaker

from attention_router.config import settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import now_utc
from attention_router.infrastructure.artifact_models import ArtifactRow
from attention_router.infrastructure.media_store import MediaStore
from attention_router.infrastructure.models import MediaArtifactRow, TenantRow
from attention_router.observability.tracing import configure_test_tracing, reset_tracing
from attention_router.web.internal_ingress_app import (
    app,
    get_session,
    health_engine,
    internal_ingress_executor,
)


SECRET = "unit-test-internal-ingress-secret-32-bytes"
VALID_TRACEPARENT = "00-1234567890abcdef1234567890abcdef-1234567890abcdef-01"
SECOND_TRACEPARENT = "00-abcdef1234567890abcdef1234567890-fedcba0987654321-01"


@pytest.fixture()
def otel_exporter():
    exporter = InMemorySpanExporter()
    configure_test_tracing(exporter)
    try:
        yield exporter
    finally:
        reset_tracing()


def teardown_function():
    app.dependency_overrides.clear()


def payload(event_id: str | None = None, text: str = "Mensagem interna sintética.") -> dict:
    return {
        "schema_version": "1",
        "tenant_id": DEFAULT_TENANT_ID,
        "source": "wwebjs",
        "external_event_id": event_id or f"int-{uuid.uuid4()}",
        "event_type": "message",
        "external_actor_id": "wa_actor_5500000000029",
        "channel": "whatsapp",
        "message_type": "text",
        "content": text,
        "has_media": False,
        "metadata": {"fixture": "unit"},
    }


def owner_command_payload(event_id: str) -> dict:
    data = payload(event_id, "espera 60")
    data.update(
        {
            "event_origin": "OWNER_COMMAND",
            "owner_authenticated": True,
            "metadata": {
                "from_me": True,
                "owner_self_chat": True,
                "from_me_classification": "OWNER_COMMAND",
                "final_from_me_classification": "OWNER_COMMAND",
            },
        }
    )
    return data


def signed_headers(body: bytes, secret: str = SECRET, timestamp: int | None = None) -> dict[str, str]:
    timestamp = timestamp or int(time.time())
    signature = hmac.new(secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"X-Attention-Timestamp": str(timestamp), "X-Attention-Signature": f"sha256={signature}"}


def client(session, monkeypatch):
    monkeypatch.setattr(settings, "internal_ingress_hmac_secret", SECRET)
    # The executor creates its own session; a FastAPI dependency override alone
    # does not reach that boundary. Share only this disposable fixture engine.
    monkeypatch.setattr(
        "attention_router.web.internal_ingress_app.SessionLocal",
        sessionmaker(bind=session.get_bind(), expire_on_commit=False, future=True),
    )

    def override():
        yield session

    app.dependency_overrides[get_session] = override
    return TestClient(app)


def post(
    client: TestClient,
    data: dict,
    headers: dict[str, str] | None = None,
    traceparent: str | None = None,
):
    body = json.dumps(data, separators=(",", ":")).encode()
    request_headers = dict(headers or signed_headers(body))
    if traceparent is not None:
        request_headers["traceparent"] = traceparent
    return client.post(
        "/api/v1/ingress/internal/events",
        content=body,
        headers=request_headers,
    )


def test_internal_ingress_valid_hmac_accepts_payload(session, monkeypatch):
    c = client(session, monkeypatch)
    response = post(c, payload())
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert response.json()["interaction_id"]


def test_internal_ingress_valid_traceparent_links_canonical_message_root(
    session,
    monkeypatch,
    otel_exporter,
):
    c = client(session, monkeypatch)
    response = post(c, payload(), traceparent=VALID_TRACEPARENT)

    assert response.status_code == 200
    trace_id = int(VALID_TRACEPARENT.split("-")[1], 16)
    span_id = int(VALID_TRACEPARENT.split("-")[2], 16)
    spans = otel_exporter.get_finished_spans()
    admission = next(span for span in spans if span.name == "ingress.accept")
    message = next(
        span for span in spans
        if span.name == "attention.message" and span.parent is None
    )

    assert admission.context.trace_id == trace_id
    assert admission.parent is not None
    assert admission.parent.span_id == span_id
    assert admission.parent.is_remote
    assert admission.attributes["roc.result"] == "ACCEPTED"
    assert admission.attributes["roc.trace_source"] == "native"
    assert admission.attributes["roc.synthetic"] is False

    assert message.context.trace_id != trace_id
    assert len(message.links) == 1
    assert message.links[0].context.trace_id == trace_id
    assert message.links[0].context.span_id == span_id
    assert message.attributes["roc.correlation_id"] == response.json()["correlation_id"]
    assert message.attributes["roc.result"] == "ACCEPTED"
    assert message.attributes["roc.trace_source"] == "native"
    assert message.attributes["roc.synthetic"] is False


def test_internal_ingress_invalid_traceparent_starts_clean_local_traces(
    session,
    monkeypatch,
    otel_exporter,
):
    c = client(session, monkeypatch)
    response = post(c, payload(), traceparent="invalid")

    assert response.status_code == 200
    spans = otel_exporter.get_finished_spans()
    admission = next(span for span in spans if span.name == "ingress.accept")
    message = next(
        span for span in spans
        if span.name == "attention.message" and span.parent is None
    )

    assert admission.parent is None
    assert message.parent is None
    assert not message.links
    assert message.context.trace_id != admission.context.trace_id
    assert message.attributes["roc.correlation_id"] == response.json()["correlation_id"]


def test_internal_ingress_replay_keeps_andy_correlation_across_distinct_trace_attempts(
    session,
    monkeypatch,
    otel_exporter,
):
    c = client(session, monkeypatch)
    data = payload("otel-replay-correlation")
    first = post(c, data, traceparent=VALID_TRACEPARENT)
    second = post(c, data, traceparent=SECOND_TRACEPARENT)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "duplicate"
    assert first.json()["correlation_id"] == second.json()["correlation_id"]

    message_roots = [
        span for span in otel_exporter.get_finished_spans()
        if span.name == "attention.message" and span.parent is None
    ]
    assert len(message_roots) == 2
    assert {
        span.attributes["roc.result"] for span in message_roots
    } == {"ACCEPTED", "DUPLICATE"}
    assert {
        span.attributes["roc.correlation_id"] for span in message_roots
    } == {first.json()["correlation_id"]}
    assert {
        span.links[0].context.trace_id for span in message_roots
    } == {
        int(VALID_TRACEPARENT.split("-")[1], 16),
        int(SECOND_TRACEPARENT.split("-")[1], 16),
    }


def test_internal_ingress_requires_explicit_tenant(session, monkeypatch):
    c = client(session, monkeypatch)
    data = payload()
    data.pop("tenant_id")
    assert post(c, data).status_code == 422


def test_internal_ingress_rejects_unknown_tenant(session, monkeypatch):
    c = client(session, monkeypatch)
    data = payload()
    data["tenant_id"] = f"missing-{uuid.uuid4()}"
    response = post(c, data)
    assert response.status_code == 403
    assert response.json()["detail"] == "tenant unavailable"


def test_internal_ingress_rejects_inactive_tenant(session, monkeypatch):
    tenant_id = f"inactive-{uuid.uuid4()}"
    stamp = now_utc()
    session.add(TenantRow(
        id=tenant_id,
        slug=tenant_id,
        name="Inactive synthetic tenant",
        status="SUSPENDED",
        created_at=stamp,
        updated_at=stamp,
    ))
    session.commit()
    c = client(session, monkeypatch)
    data = payload()
    data["tenant_id"] = tenant_id
    assert post(c, data).status_code == 403


def test_internal_ingress_invalid_hmac_rejected(session, monkeypatch):
    c = client(session, monkeypatch)
    body = json.dumps(payload()).encode()
    response = c.post("/api/v1/ingress/internal/events", content=body, headers=signed_headers(body, "wrong-secret"))
    assert response.status_code == 401


def test_internal_ingress_rejects_trace_context_before_authentication(
    session,
    monkeypatch,
    otel_exporter,
):
    c = client(session, monkeypatch)
    body = json.dumps(payload(), separators=(",", ":")).encode()
    headers = signed_headers(body, "wrong-secret")
    headers["traceparent"] = VALID_TRACEPARENT

    response = c.post(
        "/api/v1/ingress/internal/events",
        content=body,
        headers=headers,
    )

    assert response.status_code == 401
    assert otel_exporter.get_finished_spans() == ()


def test_internal_ingress_missing_signature_rejected(session, monkeypatch):
    c = client(session, monkeypatch)
    response = c.post("/api/v1/ingress/internal/events", json=payload())
    assert response.status_code == 401


def test_internal_ingress_expired_timestamp_rejected(session, monkeypatch):
    c = client(session, monkeypatch)
    body = json.dumps(payload()).encode()
    response = c.post(
        "/api/v1/ingress/internal/events",
        content=body,
        headers=signed_headers(body, timestamp=int(time.time()) - settings.internal_ingress_max_skew_seconds - 10),
    )
    assert response.status_code == 401


def test_internal_ingress_future_timestamp_rejected(session, monkeypatch):
    c = client(session, monkeypatch)
    body = json.dumps(payload()).encode()
    response = c.post(
        "/api/v1/ingress/internal/events",
        content=body,
        headers=signed_headers(body, timestamp=int(time.time()) + settings.internal_ingress_max_skew_seconds + 10),
    )
    assert response.status_code == 401


def test_internal_ingress_schema_invalid(session, monkeypatch):
    c = client(session, monkeypatch)
    data = payload()
    data["unexpected"] = True
    assert post(c, data).status_code == 422


def test_internal_ingress_rejects_owner_command_without_final_classification(
    session,
    monkeypatch,
):
    c = client(session, monkeypatch)
    data = owner_command_payload("owner-command-no-final")
    data["metadata"].pop("final_from_me_classification")
    assert post(c, data).status_code == 422


def test_internal_ingress_rejects_owner_command_without_initial_classification(
    session,
    monkeypatch,
):
    c = client(session, monkeypatch)
    data = owner_command_payload("owner-command-no-initial")
    data["metadata"].pop("from_me_classification")
    assert post(c, data).status_code == 422


def test_internal_ingress_rejects_other_source(session, monkeypatch):
    c = client(session, monkeypatch)
    data = payload()
    data["source"] = "meta_whatsapp"
    assert post(c, data).status_code == 422


def test_internal_ingress_replay_is_idempotent(session, monkeypatch):
    c = client(session, monkeypatch)
    data = payload()
    first = post(c, data)
    session.commit()
    second = post(c, data)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert first.json()["interaction_id"] == second.json()["interaction_id"]


def test_internal_ingress_same_id_different_payload_conflicts(session, monkeypatch):
    c = client(session, monkeypatch)
    event_id = f"conflict-{uuid.uuid4()}"
    assert post(c, payload(event_id)).status_code == 200
    session.commit()
    conflict = post(c, payload(event_id, "Mensagem alterada."))
    assert conflict.status_code == 409


def test_internal_ingress_admin_docs_meta_and_synthetic_are_404(session, monkeypatch):
    c = client(session, monkeypatch)
    assert c.get("/api/v1/admin/policies").status_code == 404
    assert c.get("/docs").status_code == 404
    assert c.get("/openapi.json").status_code == 404
    assert c.get("/api/v1/ingress/meta/whatsapp/webhook").status_code == 404
    assert c.post("/api/v1/ingress/synthetic/events").status_code == 404


def test_internal_ingress_uses_dedicated_executor_for_blocking_work():
    route = next(route for route in app.routes if getattr(route, "path", None) == "/api/v1/ingress/internal/events")
    assert "run_in_executor" in route.endpoint.__code__.co_names
    assert getattr(internal_ingress_executor, "_max_workers") == 16


def test_internal_ingress_ready_uses_separate_unpooled_health_engine():
    assert health_engine.pool.__class__.__name__ == "NullPool"

def _post_media(client: TestClient, data: dict):
    body = json.dumps(data, separators=(",", ":")).encode()
    return client.post(
        "/internal/whatsapp/media",
        content=body,
        headers=signed_headers(body),
    )


def test_whatsapp_document_media_stages_canonical_artifact(
    session,
    monkeypatch,
    tmp_path,
):
    c = client(session, monkeypatch)
    monkeypatch.setattr(settings, "artifact_store_enabled", True)
    monkeypatch.setattr(settings, "artifact_store_root", str(tmp_path / "artifacts"))
    monkeypatch.setattr(settings, "artifact_store_max_bytes", 1024)
    monkeypatch.setattr(settings, "whatsapp_artifact_ingestion_enabled", True)
    monkeypatch.setattr(settings, "whatsapp_artifact_max_bytes", 1024)
    monkeypatch.setattr(settings, "whatsapp_media_root", str(tmp_path / "media"))
    monkeypatch.setattr(settings, "whatsapp_media_max_bytes", 1024)

    event_id = "wamid.document.endpoint"
    inbound = payload(event_id, "")
    inbound["message_type"] = "document"
    inbound["has_media"] = True
    inbound["metadata"] = {"source_account": "whatsapp-local"}

    assert post(c, inbound).status_code == 200

    staging = MediaStore(tmp_path / "media", 1024)
    reference, digest, size, _ = staging.put_opaque_bytes(b"endpoint pdf bytes")
    response = _post_media(
        c,
        {
            "tenant_id": DEFAULT_TENANT_ID,
            "source": "wwebjs",
            "external_event_id": event_id,
            "media_ref": reference,
            "content_sha256": digest,
            "mime_type": "application/pdf",
            "size_bytes": size,
            "media_kind": "document",
            "original_filename": "../../report.pdf",
            "capture_status": "READY",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["artifact_id"] is None
    assert body["canonical_artifact_id"]
    session.expire_all()
    artifact = session.get(ArtifactRow, body["canonical_artifact_id"])
    assert artifact is not None
    assert artifact.tenant_id == DEFAULT_TENANT_ID
    assert artifact.mime_type == "application/pdf"
    assert artifact.original_filename == "../../report.pdf"


def test_whatsapp_voice_media_keeps_legacy_and_adds_canonical_artifact(
    session,
    monkeypatch,
    tmp_path,
):
    c = client(session, monkeypatch)
    monkeypatch.setattr(settings, "artifact_store_enabled", True)
    monkeypatch.setattr(settings, "artifact_store_root", str(tmp_path / "artifacts"))
    monkeypatch.setattr(settings, "artifact_store_max_bytes", 1024)
    monkeypatch.setattr(settings, "whatsapp_artifact_ingestion_enabled", True)
    monkeypatch.setattr(settings, "whatsapp_artifact_max_bytes", 1024)
    monkeypatch.setattr(settings, "whatsapp_media_root", str(tmp_path / "media"))
    monkeypatch.setattr(settings, "whatsapp_media_max_bytes", 1024)

    event_id = "wamid.voice.endpoint"
    inbound = payload(event_id, "")
    inbound["message_type"] = "ptt"
    inbound["has_media"] = True
    inbound["metadata"] = {"source_account": "whatsapp-local"}
    assert post(c, inbound).status_code == 200

    staging = MediaStore(tmp_path / "media", 1024)
    reference, digest, size, _ = staging.put_bytes(
        b"OggS endpoint voice",
        mime_type="audio/ogg",
    )
    response = _post_media(
        c,
        {
            "tenant_id": DEFAULT_TENANT_ID,
            "source": "wwebjs",
            "external_event_id": event_id,
            "media_ref": reference,
            "content_sha256": digest,
            "mime_type": "audio/ogg",
            "size_bytes": size,
            "media_kind": "ptt",
            "capture_status": "READY",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["artifact_id"]
    assert body["canonical_artifact_id"]
    assert body["artifact_id"] != body["canonical_artifact_id"]
    session.expire_all()
    assert session.get(ArtifactRow, body["canonical_artifact_id"]) is not None
    assert session.get(MediaArtifactRow, body["artifact_id"]) is not None

def test_whatsapp_generic_media_retries_when_artifact_gate_is_off(
    session,
    monkeypatch,
):
    c = client(session, monkeypatch)
    event_id = "wamid.generic.disabled"
    inbound = payload(event_id, "")
    inbound["message_type"] = "image"
    inbound["has_media"] = True
    assert post(c, inbound).status_code == 200

    response = _post_media(
        c,
        {
            "tenant_id": DEFAULT_TENANT_ID,
            "source": "wwebjs",
            "external_event_id": event_id,
            "media_ref": "sha256:" + "0" * 64,
            "content_sha256": "0" * 64,
            "mime_type": "image/png",
            "size_bytes": 1,
            "media_kind": "image",
            "capture_status": "READY",
        },
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "whatsapp artifact ingestion disabled"
