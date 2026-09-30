from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from opentelemetry import context as otel_context, trace
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Link, Status, StatusCode

from attention_router.application import services
from attention_router.application.decision_pipeline import process_agent_decision
from attention_router.adapters.inbound import NormalizedInboundEvent
from attention_router.config import Settings, settings
from attention_router.domain.models import new_id
from attention_router.observability import tracing
from attention_router.observability.tracing import (
    configure_test_tracing,
    current_span,
    extract_trace_context,
    inject_trace_context,
    reset_tracing,
    safe_record_exception,
    safe_set_attribute,
    set_outcome,
    start_span,
)


@pytest.fixture(autouse=True)
def isolated_tracing(monkeypatch):
    reset_tracing()
    monkeypatch.setattr(settings, "otel_tracing_enabled", False)
    monkeypatch.setattr(settings, "otel_exporter_otlp_endpoint", None)
    monkeypatch.setattr(settings, "otel_resource_attributes", None)
    monkeypatch.setattr(settings, "otel_service_version", "0.1.0")
    monkeypatch.setattr(settings, "otel_traces_sampler", "always_on")
    yield
    reset_tracing()


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
    assert not failed.events
    assert failed.attributes["error.type"] == "RuntimeError"
    assert failed.status.description is None


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
    assert "attention.message_text_sha256" not in attrs
    assert attrs["attention.message_length"] == len(fake_secret)


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


PRIVATE = "synthetic-private-content phone:+5500000000000 token:not-a-real-token"
VALID_PARENT = "00-1234567890abcdef1234567890abcdef-1234567890abcdef-01"


def _assert_no_private_export(exporter):
    # Include resources, instrumentation scope, events, links, status and tracestate.
    for span in exporter.get_finished_spans():
        assert PRIVATE not in span.to_json()
        assert PRIVATE not in repr(span.instrumentation_scope)
        assert not span.events
        assert span.status.description is None
        assert not span.context.trace_state
        assert not span.parent or not span.parent.trace_state
        assert all(not link.context.trace_state and not link.attributes for link in span.links)


@pytest.mark.parametrize("key", [
    "arbitrary", "attention.agent.objective", "attention.agent.failure_reason",
    "attention.decision.reason", "attention.response_objective", "attention.binding.actor_id",
    "attention.message_text_sha256", "attention.binding.lookup_identifier_hash",
    "attention.actor_id", "attention.text_length", "attention.payload_present",
    "phone", "token", "prompt", "response", "payload", "exception.message", "exception.stacktrace",
])
def test_attributes_require_explicit_allowlist(exporter, key):
    with start_span("attention.message", attributes={key: PRIVATE}) as span:
        safe_set_attribute(span, key, PRIVATE)
    assert key not in exporter.get_finished_spans()[0].attributes
    _assert_no_private_export(exporter)


@pytest.mark.parametrize(("key", "value"), [
    ("attention.outcome", PRIVATE),
    ("attention.response_source", PRIVATE),
    ("attention.message_length", PRIVATE),
    ("attention.message_length", True),
    ("attention.message_length", -1),
    ("attention.message_length", 10**30),
    ("attention.external_send", PRIVATE),
    ("attention.external_send", 1),
    ("attention.confidence", float("nan")),
    ("attention.confidence", float("inf")),
    ("attention.correlation_id", PRIVATE),
    ("attention.correlation_id", "a" * 65),
    ("attention.outcome", ["OK", PRIVATE]),
    ("attention.message_length", {"payload": PRIVATE}),
    ("error.type", PRIVATE),
])
def test_allowlisted_keys_reject_unsafe_types_values_and_lengths(exporter, key, value):
    with start_span("attention.message") as span:
        safe_set_attribute(span, key, value)
    assert key not in exporter.get_finished_spans()[0].attributes
    _assert_no_private_export(exporter)


def test_safe_operational_attributes_and_limits_remain_available(exporter):
    correlation = new_id()
    with start_span("attention.message") as span:
        for key in tracing._BOOL_ATTRIBUTES:
            safe_set_attribute(span, key, True)
        safe_set_attribute(span, "attention.outcome", "OK")
        safe_set_attribute(span, "attention.message_length", 12)
        safe_set_attribute(span, "attention.correlation_id", correlation)
        safe_set_attribute(span, "attention.confidence", 0.5)
    attrs = exporter.get_finished_spans()[0].attributes
    assert len(attrs) <= 32
    assert attrs["attention.outcome"] == "OK"
    assert attrs["attention.message_length"] == 12
    assert attrs["attention.correlation_id"] == correlation
    assert attrs["attention.confidence"] == 0.5


def test_resource_allowlist_cannot_be_bypassed_by_sdk_environment(monkeypatch):
    monkeypatch.setattr(settings, "otel_resource_attributes", (
        f"arbitrary={PRIVATE},service.instance.id={PRIVATE},host.name={PRIVATE},"
        "service.name=attention-router-worker,service.version=1.2.3,deployment.environment=test"
    ))
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", f"arbitrary={PRIVATE},host.name={PRIVATE}")
    monkeypatch.setenv("OTEL_SERVICE_NAME", PRIVATE)
    exporter = InMemorySpanExporter()
    configure_test_tracing(exporter)
    with start_span("attention.message"):
        pass
    assert dict(exporter.get_finished_spans()[0].resource.attributes) == {
        "service.name": "attention-router-worker", "service.version": "1.2.3",
        "deployment.environment": "test",
    }
    _assert_no_private_export(exporter)


@pytest.mark.parametrize("key", ["service.name", "service.version", "deployment.environment"])
def test_resource_allowlisted_keys_reject_sensitive_values(monkeypatch, key):
    monkeypatch.setattr(settings, "otel_resource_attributes", f"{key}={PRIVATE}")
    monkeypatch.setattr(settings, "otel_service_version", PRIVATE)
    exporter = InMemorySpanExporter()
    configure_test_tracing(exporter)
    with start_span("attention.message"):
        pass
    assert key not in exporter.get_finished_spans()[0].resource.attributes
    _assert_no_private_export(exporter)


def test_direct_span_calls_names_events_status_and_links_cannot_export_content(exporter):
    parent = trace.SpanContext(123, 456, True, trace.TraceFlags(1), trace.TraceState([("vendor", PRIVATE)]))
    with start_span(PRIVATE, links=[Link(parent, {"payload": PRIVATE})] * 20) as span:
        span.set_attribute("payload", PRIVATE)
        span.set_attributes({"attention.outcome": PRIVATE, "attention.external_send": False})
        span.add_event(PRIVATE, {"payload": PRIVATE})
        span.set_status(Status(StatusCode.ERROR, PRIVATE))
        span.update_name(PRIVATE)
        span.record_exception(ValueError(PRIVATE))
    finished = exporter.get_finished_spans()[0]
    assert finished.name == "attention.operation"
    assert len(finished.links) == 8
    assert finished.attributes["attention.external_send"] is False
    assert finished.attributes["error.type"] == "ValueError"
    assert "payload" not in finished.attributes
    _assert_no_private_export(exporter)


def test_original_exception_identity_chain_and_traceback_preserved_without_duplicate_capture(exporter):
    original = ValueError(PRIVATE)
    cause = RuntimeError(PRIVATE)
    effects = []

    def functional_operation():
        effects.append("executed")
        raise original from cause

    with pytest.raises(ValueError) as caught:
        with start_span("attention.message"):
            with start_span("decision.evaluate") as span:
                safe_record_exception(span, original)
                safe_record_exception(span, original)
                functional_operation()
    assert caught.value is original
    assert caught.value.__cause__ is cause
    assert effects == ["executed"]
    assert list(caught.traceback)[-1].name == "functional_operation"
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    assert all(span.attributes["error.type"] == "ValueError" for span in spans)
    assert all(span.attributes["attention.outcome"] == "FAILED" for span in spans)
    assert all(span.status.status_code is StatusCode.ERROR for span in spans)
    assert all(not span.events for span in spans)
    _assert_no_private_export(exporter)


def test_unknown_exception_class_name_and_stringification_are_never_exported(exporter):
    def forbidden_str(self):
        raise AssertionError("Exception must not be stringified")

    error_class = type(PRIVATE, (Exception,), {"__str__": forbidden_str})
    error = error_class(PRIVATE)
    with pytest.raises(error_class) as caught:
        with start_span("decision.evaluate"):
            raise error
    assert caught.value is error
    assert exporter.get_finished_spans()[0].attributes["error.type"] == "Exception"
    _assert_no_private_export(exporter)


def test_error_after_provisional_success_finishes_as_error(exporter):
    original = ValueError(PRIVATE)
    with pytest.raises(ValueError) as caught:
        with start_span("decision.evaluate"):
            set_outcome(current_span(), "OK")
            raise original
    assert caught.value is original
    span = exporter.get_finished_spans()[0]
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["attention.outcome"] == "FAILED"
    _assert_no_private_export(exporter)


@pytest.mark.parametrize("method", ["is_recording", "set_attribute", "set_status", "get_span_context", "end"])
@pytest.mark.parametrize("functional_error", [False, True])
def test_span_component_failure_never_changes_functional_result(exporter, monkeypatch, method, functional_error):
    real = tracing.get_tracer().start_span("decision.evaluate")
    broken = Mock(wraps=real)
    getattr(broken, method).side_effect = RuntimeError(PRIVATE)
    monkeypatch.setattr(tracing, "get_tracer", lambda: SimpleNamespace(start_span=lambda *a, **kw: broken))
    original = ValueError("synthetic functional error")
    effects = []

    def operation():
        with start_span("decision.evaluate") as span:
            safe_set_attribute(span, "attention.outcome", "OK")
            safe_record_exception(span, original)
            set_outcome(span, "OK")
            current_span().get_span_context()
            effects.append("once")
            if functional_error:
                raise original
            return 42

    try:
        if functional_error:
            with pytest.raises(ValueError) as caught:
                operation()
            assert caught.value is original
        else:
            assert operation() == 42
        assert effects == ["once"]
        assert not broken.record_exception.called
    finally:
        real.end()


@pytest.mark.parametrize("boundary", ["get_tracer", "start_span", "attach", "detach", "detach_after"])
@pytest.mark.parametrize("functional_error", [False, True])
def test_setup_and_cleanup_failure_are_noop(monkeypatch, exporter, boundary, functional_error):
    original = ValueError("synthetic functional error")
    failing = Mock(side_effect=RuntimeError(PRIVATE))
    if boundary == "get_tracer":
        monkeypatch.setattr(tracing, "get_tracer", failing)
    elif boundary == "start_span":
        monkeypatch.setattr(tracing.get_tracer(), "start_span", failing)
    elif boundary == "detach_after":
        detach = otel_context.detach

        def detach_then_fail(token):
            detach(token)
            failing()

        monkeypatch.setattr(otel_context, "detach", detach_then_fail)
    else:
        monkeypatch.setattr(otel_context, boundary, failing)
    effects = []

    def operation():
        with start_span("decision.evaluate"):
            effects.append("once")
            if functional_error:
                raise original
            return 42

    if functional_error:
        with pytest.raises(ValueError) as caught:
            operation()
        assert caught.value is original
    else:
        assert operation() == 42
    assert effects == ["once"]
    assert not trace.get_current_span().get_span_context().is_valid


@pytest.mark.parametrize("hook", ["on_start", "on_end"])
def test_broken_processor_preserves_success_and_original_error(exporter, hook):
    class BrokenProcessor(SpanProcessor):
        def on_start(self, span, parent_context=None):
            if hook == "on_start":
                raise RuntimeError(PRIVATE)

        def on_end(self, span):
            if hook == "on_end":
                raise RuntimeError(PRIVATE)

    tracing._tracer_provider.add_span_processor(BrokenProcessor())
    with start_span("decision.evaluate"):
        result = 42
    assert result == 42
    original = ValueError("synthetic functional error")
    with pytest.raises(ValueError) as caught:
        with start_span("decision.evaluate"):
            raise original
    assert caught.value is original


def test_broken_exporter_does_not_log_or_mask_functional_exception(caplog):
    configure_test_tracing(FailingExporter())
    original = ValueError(PRIVATE)
    with start_span("attention.message"):
        result = 42
    with pytest.raises(ValueError) as caught:
        with start_span("attention.message"):
            raise original
    assert result == 42
    assert caught.value is original
    assert PRIVATE not in caplog.text
    assert "Traceback" not in caplog.text


@pytest.mark.parametrize("carrier", [
    None, {}, {"traceparent": "invalid"}, {"traceparent": PRIVATE},
    {"traceparent": [VALID_PARENT]}, {"traceparent": 123}, {"traceparent": "a" * 8192},
    {"traceparent": "00-" + "0" * 32 + "-1234567890abcdef-01"},
    {"traceparent": "00-1234567890abcdef1234567890abcdef-0000000000000000-01"},
    {"traceparent": VALID_PARENT + "\n"}, {"traceparent": VALID_PARENT.upper()},
    {"tracestate": PRIVATE, "baggage": PRIVATE}, [], "invalid",
])
def test_invalid_carrier_never_inherits_previous_operation(exporter, carrier):
    with start_span("attention.message") as previous:
        previous_context = previous.get_span_context()
        clean = extract_trace_context(carrier)
        assert not trace.get_current_span(clean).get_span_context().is_valid
        assert tracing.link_from_carrier(carrier) == []
        with start_span("worker.dispatch", context=clean) as current:
            assert current.get_span_context().trace_id != previous_context.trace_id
        assert current_span().get_span_context() == previous_context
    worker = next(span for span in exporter.get_finished_spans() if span.name == "worker.dispatch")
    assert worker.parent is None
    assert not current_span().get_span_context().is_valid


def test_extraction_exception_returns_clean_context_and_link(monkeypatch, exporter):
    monkeypatch.setattr(tracing._PROPAGATOR, "extract", Mock(side_effect=RuntimeError(PRIVATE)))
    with start_span("attention.message") as previous:
        clean = extract_trace_context({"traceparent": VALID_PARENT})
        assert not trace.get_current_span(clean).get_span_context().is_valid
        assert tracing.link_from_carrier({"traceparent": VALID_PARENT}) == []
        with start_span("worker.dispatch", context=clean) as span:
            assert span.get_span_context().trace_id != previous.get_span_context().trace_id


@pytest.mark.parametrize("result", [None, "invalid", "ambient"])
def test_broken_extractor_result_never_reuses_ambient_context(monkeypatch, exporter, result):
    with start_span("attention.message") as previous:
        extracted = otel_context.get_current() if result == "ambient" else result
        monkeypatch.setattr(tracing._PROPAGATOR, "extract", Mock(return_value=extracted))
        clean = extract_trace_context({"traceparent": VALID_PARENT})
        assert clean is not None
        assert not trace.get_current_span(clean).get_span_context().is_valid
        with start_span("worker.dispatch", context=clean) as span:
            assert span.get_span_context().trace_id != previous.get_span_context().trace_id


@pytest.mark.parametrize("context", ["invalid", {}, object()])
def test_invalid_explicit_context_is_also_isolated(exporter, context):
    with start_span("attention.message") as previous:
        with start_span("worker.dispatch", context=context) as span:
            assert span.get_span_context().trace_id != previous.get_span_context().trace_id
    assert exporter.get_finished_spans()[0].parent is None


def test_valid_carrier_overrides_ambient_trace_without_baggage_or_tracestate(exporter):
    carrier = {"traceparent": VALID_PARENT, "tracestate": f"vendor={PRIVATE}", "baggage": PRIVATE}
    with start_span("attention.message") as unrelated:
        with start_span("worker.dispatch", context=extract_trace_context(carrier)) as span:
            context = span.get_span_context()
            assert context.trace_id == int(VALID_PARENT.split("-")[1], 16)
            assert context.trace_id != unrelated.get_span_context().trace_id
            injected = {"traceparent": "stale", "tracestate": PRIVATE, "baggage": PRIVATE}
            inject_trace_context(injected)
            assert set(injected) == {"traceparent"}
            assert injected["traceparent"].split("-")[1] == VALID_PARENT.split("-")[1]
        assert current_span().get_span_context() == unrelated.get_span_context()
    worker = exporter.get_finished_spans()[0]
    assert worker.parent.span_id == int(VALID_PARENT.split("-")[2], 16)
    assert worker.parent.is_remote
    assert tracing.link_from_carrier(carrier)[0].context.trace_id == context.trace_id
    _assert_no_private_export(exporter)


def _mock_otlp(monkeypatch):
    import opentelemetry.exporter.otlp.proto.http.trace_exporter as otlp

    monkeypatch.setattr(settings, "otel_tracing_enabled", True)
    monkeypatch.setattr(settings, "otel_exporter_otlp_endpoint", "http://collector.invalid/v1/traces")
    exporter = InMemorySpanExporter()
    factory = Mock(return_value=exporter)
    monkeypatch.setattr(otlp, "OTLPSpanExporter", factory)
    return factory, exporter


@pytest.mark.parametrize("component", [
    "provider", "resource", "sampler", "exporter", "processor", "add_processor", "get_tracer",
])
def test_configuration_component_failure_falls_back_to_real_noop(monkeypatch, component):
    import opentelemetry.sdk.trace as sdk
    import opentelemetry.sdk.trace.export as sdk_export

    factory, _ = _mock_otlp(monkeypatch)
    failing = Mock(side_effect=RuntimeError(PRIVATE))
    if component == "provider":
        monkeypatch.setattr(sdk, "TracerProvider", failing)
    elif component in {"resource", "sampler"}:
        monkeypatch.setattr(tracing, "_" + component, failing)
    elif component == "exporter":
        factory.side_effect = RuntimeError(PRIVATE)
    elif component == "processor":
        monkeypatch.setattr(sdk_export, "BatchSpanProcessor", failing)
    else:
        monkeypatch.setattr(sdk.TracerProvider, "add_span_processor" if component == "add_processor" else "get_tracer", failing)
    # A configured global tracer must not be used as the fallback.
    monkeypatch.setattr(trace, "get_tracer", Mock(side_effect=AssertionError("Global tracer used")))
    assert isinstance(tracing.configure_tracing(), trace.NoOpTracer)
    assert isinstance(tracing.get_tracer(), trace.NoOpTracer)
    with start_span("attention.message") as span:
        assert not span.is_recording()
    original = ValueError("synthetic functional error")
    with pytest.raises(ValueError) as caught:
        with start_span("attention.message"):
            raise original
    assert caught.value is original
    assert tracing._tracer_provider is None


@pytest.mark.parametrize(("key", "value"), [
    ("otel_tracing_enabled", "invalid"),
    ("otel_traces_sampler_arg", "invalid"),
    ("otel_traces_sampler_arg", "nan"),
    ("otel_traces_sampler_arg", "inf"),
    ("otel_export_timeout_millis", "invalid"),
    ("otel_export_timeout_millis", -1),
    ("otel_flush_timeout_millis", "invalid"),
    ("otel_flush_timeout_millis", 10001),
    ("otel_batch_max_queue_size", 0),
    ("otel_batch_max_export_batch_size", 999999),
    ("otel_batch_schedule_delay_millis", "invalid"),
    ("otel_exporter_otlp_protocol", "invalid"),
    ("otel_traces_sampler", "invalid"),
])
def test_invalid_otel_config_does_not_break_settings_startup(key, value):
    options = {"otel_tracing_enabled": True, key: value}
    configured = Settings(_env_file=None, **options)
    assert configured.otel_tracing_enabled is False
    assert getattr(configured, key) == Settings.model_fields[key].default


def test_invalid_otel_environment_disables_tracing_without_breaking_settings(monkeypatch):
    monkeypatch.setenv("OTEL_TRACING_ENABLED", "true")
    monkeypatch.setenv("OTEL_FLUSH_TIMEOUT_MILLIS", "invalid")
    configured = Settings(_env_file=None)
    assert configured.otel_tracing_enabled is False
    assert configured.otel_flush_timeout_millis == 1000


@pytest.mark.parametrize(("key", "value"), [
    ("otel_batch_max_queue_size", 0),
    ("otel_batch_max_export_batch_size", 8192),
    ("otel_export_timeout_millis", "invalid"),
    ("otel_flush_timeout_millis", -1),
    ("otel_exporter_otlp_endpoint", "invalid"),
    ("otel_exporter_otlp_protocol", "invalid"),
    ("otel_traces_sampler_arg", float("nan")),
])
def test_invalid_runtime_telemetry_config_is_noop(monkeypatch, key, value):
    _mock_otlp(monkeypatch)
    monkeypatch.setattr(settings, "otel_traces_sampler", "parentbased_traceidratio")
    monkeypatch.setattr(settings, key, value)
    assert isinstance(tracing.configure_tracing(), trace.NoOpTracer)
    with start_span("attention.message") as span:
        assert not span.is_recording()


@pytest.mark.parametrize("explicit", [None, 0, 37])
def test_flush_uses_own_timeout_and_preserves_explicit_zero(monkeypatch, explicit):
    provider = Mock()
    provider.force_flush.return_value = True
    monkeypatch.setattr(tracing, "_tracer_provider", provider)
    monkeypatch.setattr(settings, "otel_export_timeout_millis", 9000)
    monkeypatch.setattr(settings, "otel_flush_timeout_millis", 123)
    assert tracing.flush_tracing(explicit) is True
    provider.force_flush.assert_called_once_with(123 if explicit is None else explicit)


def test_exporter_http_timeout_and_batch_flush_budget_are_separate(monkeypatch):
    factory, exporter = _mock_otlp(monkeypatch)
    monkeypatch.setattr(settings, "otel_export_timeout_millis", 2500)
    monkeypatch.setattr(settings, "otel_flush_timeout_millis", 73)
    tracing.configure_tracing()
    factory.assert_called_once_with(endpoint="http://collector.invalid/v1/traces", timeout=2.5)
    processor = tracing._tracer_provider._active_span_processor._span_processors[0]
    assert processor.export_timeout_millis == 73
    with start_span("attention.message"):
        pass
    assert tracing.flush_tracing(1000)
    assert len(exporter.get_finished_spans()) == 1


def test_batch_export_failure_is_off_functional_thread_and_flush_is_bounded(monkeypatch):
    factory, _ = _mock_otlp(monkeypatch)
    entered, release = Event(), Event()

    class BlockedExporter(FailingExporter):
        def export(self, spans):
            entered.set()
            assert release.wait(2), "Test export was not released"
            super().export(spans)

    factory.return_value = BlockedExporter()
    monkeypatch.setattr(settings, "otel_batch_max_export_batch_size", 1)
    monkeypatch.setattr(settings, "otel_batch_max_queue_size", 2)
    monkeypatch.setattr(settings, "otel_flush_timeout_millis", 1)
    tracing.configure_tracing()
    try:
        with start_span("attention.message"):
            result = 42
        assert result == 42
        assert entered.wait(1)
        assert tracing.flush_tracing() is False
        # Fill/drop a tiny synthetic batch queue while the exporter is blocked.
        for _ in range(4):
            with start_span("attention.message"):
                pass
        original = ValueError("synthetic functional error")
        with pytest.raises(ValueError) as caught:
            with start_span("attention.message"):
                raise original
        assert caught.value is original
    finally:
        release.set()
        tracing.shutdown_tracing()


def test_flush_and_shutdown_exceptions_are_best_effort(monkeypatch):
    provider = Mock()
    provider.force_flush.side_effect = RuntimeError(PRIVATE)
    provider.shutdown.side_effect = RuntimeError(PRIVATE)
    monkeypatch.setattr(tracing, "_tracer_provider", provider)
    assert tracing.flush_tracing() is False
    tracing.shutdown_tracing()


def test_initialization_is_once_across_threads(monkeypatch):
    factory, _ = _mock_otlp(monkeypatch)
    with ThreadPoolExecutor(max_workers=4) as pool:
        tracers = list(pool.map(lambda _: tracing.configure_tracing(), range(8)))
    assert all(tracer is tracers[0] for tracer in tracers)
    factory.assert_called_once()


def test_disabled_tracing_stays_noop_even_with_active_global_provider(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    foreign = provider.get_tracer("foreign-test")
    monkeypatch.setattr(trace, "get_tracer", lambda *a, **kw: foreign)
    try:
        with foreign.start_as_current_span("foreign.operation"):
            with tracing.ensure_message_trace() as span:
                safe_set_attribute(span, "attention.outcome", "OK")
                assert not span.is_recording()
                assert not current_span().get_span_context().is_valid
                assert inject_trace_context({}) == {}
        assert len(exporter.get_finished_spans()) == 1
        assert not exporter.get_finished_spans()[0].attributes
    finally:
        provider.shutdown()
