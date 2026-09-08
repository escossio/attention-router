from __future__ import annotations

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from attention_router.application import services
from attention_router.application.decision_pipeline import process_agent_decision
from attention_router.adapters.inbound import NormalizedInboundEvent
from attention_router.config import settings
from attention_router.domain.models import new_id
from attention_router.observability.tracing import (
    configure_test_tracing,
    current_span,
    extract_trace_context,
    inject_trace_context,
    reset_tracing,
    safe_set_attribute,
    set_outcome,
    start_span,
)


@pytest.fixture()
def exporter():
    value = InMemorySpanExporter()
    configure_test_tracing(value)
    yield value
    reset_tracing()


def _span_names(exporter):
    return [span.name for span in exporter.get_finished_spans()]


def _emit_happy_trace():
    with start_span("attention.message") as root:
        set_outcome(root, "DELIVERED")
        for name, outcome in (
            ("inbound.receive", "OK"),
            ("actor.resolve", "BOUND"),
            ("policy.resolve", "SELECTED"),
            ("decision.evaluate", "EVALUATED"),
            ("behavior.generate", "GENERATED"),
            ("repetition_guard.evaluate", "ALLOWED"),
            ("memory.archive", "NOT_APPLICABLE"),
            ("autonomy.evaluate", "BLOCKED"),
            ("execution.intent", "NOT_APPLICABLE"),
            ("outbox.enqueue", "ENQUEUED"),
            ("transport.send", "SENT"),
        ):
            with start_span(name) as span:
                safe_set_attribute(span, "attention.outcome", outcome)


def test_happy_path_has_one_correlated_trace_and_expected_tree(exporter):
    _emit_happy_trace()
    spans = exporter.get_finished_spans()
    assert {span.name for span in spans} == {
        "attention.message", "inbound.receive", "actor.resolve", "policy.resolve",
        "decision.evaluate", "behavior.generate", "repetition_guard.evaluate",
        "memory.archive", "autonomy.evaluate", "execution.intent", "outbox.enqueue",
        "transport.send",
    }
    assert len({span.context.trace_id for span in spans}) == 1
    root = next(span for span in spans if span.name == "attention.message")
    assert all(span.parent and span.parent.span_id == root.context.span_id for span in spans if span is not root)
    assert root.attributes["attention.outcome"] == "DELIVERED"


def test_suppression_path_has_no_outbox_or_transport(exporter):
    with start_span("attention.message") as root:
        with start_span("inbound.receive"):
            pass
        with start_span("actor.resolve"):
            pass
        with start_span("decision.evaluate"):
            pass
        with start_span("behavior.generate"):
            pass
        with start_span("repetition_guard.evaluate") as guard:
            safe_set_attribute(guard, "attention.repetition.state_changed", False)
            safe_set_attribute(guard, "attention.repetition.semantic_repeat", True)
            safe_set_attribute(guard, "attention.repetition.suppressed", True)
            safe_set_attribute(guard, "attention.repetition.reason", "SEMANTIC_REPEAT_NO_STATE_CHANGE")
            set_outcome(guard, "SUPPRESSED")
        set_outcome(root, "SUPPRESSED")
    names = _span_names(exporter)
    assert "outbox.enqueue" not in names
    assert "transport.send" not in names
    guard = next(span for span in exporter.get_finished_spans() if span.name == "repetition_guard.evaluate")
    assert guard.attributes["attention.repetition.suppressed"] is True
    assert guard.attributes["attention.outcome"] == "SUPPRESSED"


def test_blocked_and_failure_paths_preserve_trace(exporter):
    with start_span("attention.message") as root:
        with start_span("autonomy.evaluate") as autonomy:
            safe_set_attribute(autonomy, "attention.effective_mode", "REQUIRES_APPROVAL")
            set_outcome(autonomy, "BLOCKED")
        set_outcome(root, "BLOCKED")
    with pytest.raises(RuntimeError):
        with start_span("attention.message") as root:
            with start_span("decision.evaluate"):
                raise RuntimeError("synthetic functional failure")
            set_outcome(root, "FAILED", error=True)
    finished = exporter.get_finished_spans()
    assert any(span.name == "attention.message" and span.attributes.get("attention.outcome") == "BLOCKED" for span in finished)
    failed = next(span for span in finished if span.name == "decision.evaluate" and span.status.status_code.name == "ERROR")
    assert failed.events


def test_async_memory_context_keeps_trace_correlation(exporter):
    carrier: dict[str, str] = {}
    with start_span("attention.message"):
        inject_trace_context(carrier)
        with start_span("memory.archive"):
            pass
    with start_span("memory.extract", context=extract_trace_context(carrier)) as extraction:
        safe_set_attribute(extraction, "attention.candidate_count", 1)
        set_outcome(extraction, "PROMOTED")
    spans = exporter.get_finished_spans()
    message = next(span for span in spans if span.name == "attention.message")
    extraction = next(span for span in spans if span.name == "memory.extract")
    assert extraction.context.trace_id == message.context.trace_id
    assert extraction.parent and extraction.parent.span_id == message.context.span_id


def test_privacy_filter_never_records_raw_content(exporter):
    fake_phone = "+5500000000029"
    fake_secret = "otp=123456 api_key=not-real"
    with start_span("attention.message") as span:
        safe_set_attribute(span, "attention.phone", fake_phone)
        safe_set_attribute(span, "attention.message_text", fake_secret)
        safe_set_attribute(span, "attention.message_text_sha256", "hash-only")
        safe_set_attribute(span, "attention.message_length", len(fake_secret))
    finished = exporter.get_finished_spans()
    attrs = dict(finished[0].attributes)
    assert fake_phone not in attrs.values()
    assert fake_secret not in attrs.values()
    assert "attention.phone" not in attrs
    assert "attention.message_text" not in attrs
    assert attrs["attention.message_text_sha256"] == "hash-only"


def test_tracing_disabled_is_noop_and_inbound_behavior_remains_available(session, monkeypatch):
    reset_tracing()
    monkeypatch.setattr(settings, "otel_tracing_enabled", False)
    result = services.receive_inbound_event(
        session, "synthetic", "trace-disabled-1", "message", "actor-trace", "Synthetic",
        "family_core", None, "Bom dia.",
    )
    assert result["id"]
    assert current_span().get_span_context().is_valid is False


def test_real_inbound_path_emits_sanitized_stage_spans(session, exporter, monkeypatch):
    monkeypatch.setattr(settings, "agent_decision_pipeline_enabled", False)
    result = services.receive_inbound_event(
        session, "synthetic", "trace-real-1", "message", "actor-trace", "Synthetic",
        "family_core", None, "Mensagem sintética para instrumentação.",
    )
    assert result["id"]
    names = _span_names(exporter)
    assert {"attention.message", "inbound.receive"}.issubset(names)
    assert not {"actor.resolve", "policy.resolve", "decision.evaluate", "behavior.generate",
                 "repetition_guard.evaluate"}.intersection(names)
    assert "transport.send" not in names
    spans = exporter.get_finished_spans()
    assert len({span.context.trace_id for span in spans}) == 1
    all_attributes = {key for span in spans for key in span.attributes}
    assert "attention.message_text" not in all_attributes
    assert "attention.response_text" not in all_attributes


def test_real_decision_path_emits_only_reached_runtime_spans(session, exporter):
    event = NormalizedInboundEvent(
        source="synthetic",
        external_event_id=f"trace-decision-{new_id()}",
        event_type="message",
        actor_id="trace-actor",
        actor_display_name="Synthetic",
        actor_category="unknown",
        channel="synthetic",
        content="Preciso falar com Alex.",
    )
    received = services.receive_normalized_inbound_event(session, event)
    process_agent_decision(session, received["inbound_event_id"])
    names = _span_names(exporter)
    assert {"attention.message", "actor.resolve", "policy.resolve", "decision.evaluate", "behavior.generate"} <= set(names)
    assert "memory.archive" not in names
    assert "memory.extract" not in names
    assert "transport.send" not in names


class FailingExporter:
    def export(self, spans):  # noqa: ANN001
        raise RuntimeError("synthetic exporter unavailable")

    def shutdown(self):
        return None

    def force_flush(self, timeout_millis=30000):  # noqa: ANN001
        return True


def test_exporter_failure_does_not_block_functional_span(exporter):
    configure_test_tracing(FailingExporter())
    with start_span("attention.message") as span:
        set_outcome(span, "OK")
    reset_tracing()
