"""Fail-open, allowlisted OpenTelemetry primitives for the existing Python flow."""

from __future__ import annotations

import hashlib
import inspect
import math
import re
from contextlib import contextmanager
from functools import wraps
from threading import RLock
from typing import Any, Callable, Iterator, TypeVar
from urllib.parse import urlsplit

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Link, Span, SpanContext, Status, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from attention_router.config import settings


_T = TypeVar("_T")
_tracer_provider: Any = None
_tracer: Tracer | None = None
_configured = False
_configuration_lock = RLock()
_NOOP_TRACER = trace.NoOpTracer()
_PROPAGATOR = TraceContextTextMapPropagator()
_MAX_ATTRIBUTES = 32
_MAX_LINKS = 8
_SPAN_NAMES = frozenset({
    "attention.message", "attention.operation", "inbound.receive", "actor.resolve",
    "policy.resolve", "decision.evaluate", "behavior.generate", "repetition_guard.evaluate",
    "memory.archive", "memory.extract", "autonomy.evaluate", "execution.intent",
    "outbox.enqueue", "transport.send", "worker.dispatch", "grace.release", "queue.enqueue", "andy.agent.context_build",
    "andy.agent.run", "andy.agent.validate", "authority.evaluate", "capability.execute",
    "capability.resolve", "event.normalize", "provider.resolve", "relationship.resolve",
    "ingress.accept", "scheduler.fire", "lab.delivery.consume", "lab.delivery.reserve",
    "lab.delivery.validate",
    "lab.session.claim_inbound",
})
_OUTCOMES = frozenset({
    "OK", "DELIVERED", "BOUND", "UNKNOWN", "SELECTED", "EVALUATED", "GENERATED",
    "ALLOWED", "NOT_APPLICABLE", "BLOCKED", "ENQUEUED", "SENT", "SUPPRESSED", "FAILED",
    "PROMOTED", "NO_RESPONSE", "RESPONSE_GENERATED", "CREATED", "ACCEPTED", "ARCHIVED",
    "DUPLICATE", "PROCESSED", "NO_JOBS", "NO_OUTBOX", "FALLBACK_UNAVAILABLE", "BUILT",
    "COMPLETED", "HOLD", "ALREADY_CLAIMED", "NOT_ELIGIBLE", "CLAIMED", "IDEMPOTENT",
    "RESERVED", "CONSUMED",
})
_MODES = frozenset({"OBSERVE", "REQUIRES_APPROVAL", "AUTO_ALLOWED"})
_REPETITION_REASONS = frozenset({
    "SEMANTIC_REPEAT_NO_STATE_CHANGE", "SEMANTIC_REPEAT_OBJECTIVE_ALREADY_SATISFIED",
})
_ENUM_ATTRIBUTES = {
    "attention.outcome": _OUTCOMES,
    "attention.final_outcome": _OUTCOMES,
    "roc.result": _OUTCOMES,
    "roc.stage": _SPAN_NAMES,
    "roc.operation": _SPAN_NAMES,
    "roc.trace_source": frozenset({"native"}),
    "attention.policy_mode": _MODES,
    "attention.effective_mode": _MODES,
    "attention.actor_resolution": frozenset({"BOUND", "UNKNOWN"}),
    "attention.binding.resolution_reason": frozenset({
        "EXACT_SOURCE_IDENTIFIER", "NO_ACTIVE_EXACT_MATCH",
    }),
    "attention.response_source": frozenset({
        "OPENAI_AGENTS_SDK", "ANDY_BEHAVIOR", "LEGACY", "NONE", "agent_failure",
        "andy_behavior", "legacy", "none",
    }),
    "attention.semantic_source": frozenset({"OPENAI_AGENTS_SDK"}),
    "attention.delivery_type": frozenset({"wwebjs", "local_transport"}),
    "attention.repetition.reason": _REPETITION_REASONS,
    "attention.repetition.suppression_reason": _REPETITION_REASONS,
    "attention.authorization_source": frozenset({"POLICY_AUTONOMY", "HUMAN_APPROVAL"}),
    "error.type": frozenset({
        "Exception", "BaseException", "RuntimeError", "ValueError", "TypeError", "KeyError",
        "LookupError", "AssertionError", "OSError", "TimeoutError", "ConnectionError",
        "PermissionError", "NotImplementedError", "KeyboardInterrupt", "SystemExit",
    }),
}
_BOOL_ATTRIBUTES = frozenset({
    "attention.message_from_me", "attention.response_text_present", "attention.response_generated",
    "attention.external_send", "attention.binding_resolved", "attention.binding.match_found",
    "attention.action_allowed", "attention.automatic_execution_allowed", "attention.intent_created",
    "attention.repetition.state_changed", "attention.repetition.semantic_repeat",
    "attention.repetition.suppressed", "attention.repetition.objective_already_satisfied",
    "attention.repetition.previous_useful_response_generated",
    "attention.repetition.previous_useful_response_delivered", "attention.memory.created",
    "attention.message_reference_present", "attention.decision.self_contained",
    "attention.decision.context_sufficient", "attention.agent_output_valid",
    "attention.behavior_enabled", "attention.agent.response_style_adaptation_applied",
    "attention.context.interaction_actor_resolved", "attention.context.represented_subject_resolved",
    "attention.context.presence_effective", "roc.execution_allowed", "roc.external_delivery_allowed",
})
_COUNT_ATTRIBUTES = frozenset({
    "attention.message_length", "attention.response_text_length", "attention.candidate_count",
    "attention.memory.candidate_count", "attention.policy_candidate_count", "attention.outbox_count",
    "attention.missing_information_count", "attention.agent.context_turn_count",
    "attention.agent.known_required_information_count", "attention.agent.missing_information_count",
    "attention.agent.available_action_count", "attention.agent.requested_action_count",
    "attention.agent.requested_capability_count", "attention.agent.turn_count",
    "attention.agent.response_style_explicit_count", "attention.agent.response_style_observed_count",
    "attention.context.represented_state_count", "attention.lab.inbound_ordinal",
    "attention.lab.budget_remaining", "attention.lab.send_permit_ordinal",
})
_UUID_ATTRIBUTES = frozenset({
    "attention.inbound_event_id", "attention.interaction_id", "attention.decision_id",
    "attention.execution_intent_id", "attention.outbox_id", "attention.correlation_id",
    "attention.selected_policy_version_id", "attention.lab_session_id", "roc.correlation_id",
})
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_RESOURCE_SERVICES = frozenset({
    "attention-router-api", "attention-router-worker", "attention-router-ingress",
    "attention-router-test", "attention-router-trace-demo",
    "attention-router-platform-matrix-candidate",
})
_RESOURCE_ENVIRONMENTS = frozenset({"test", "development", "production", "private"})
_VERSION = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}")
_TRACEPARENT = re.compile(r"00-[0-9a-f]{32}-[0-9a-f]{16}-0[01]")


def _safe_attribute(key: str, value: Any) -> Any:
    # Exact built-in types only: no __str__, arbitrary sequences or coercion of content.
    if type(key) is not str:
        return None
    if key in _ENUM_ATTRIBUTES:
        return value if type(value) is str and value in _ENUM_ATTRIBUTES[key] else None
    if key in _BOOL_ATTRIBUTES:
        return value if type(value) is bool else None
    if key == "roc.synthetic":
        return False if value is False else None
    if key in _COUNT_ATTRIBUTES:
        return value if type(value) is int and 0 <= value <= 1_000_000 else None
    if key in {"attention.confidence", "attention.agent.confidence"}:
        return value if type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 1 else None
    if key in _UUID_ATTRIBUTES:
        return value if type(value) is str and _UUID.fullmatch(value) else None
    if key == "http.response.status_code":
        return value if type(value) is int and 100 <= value <= 599 else None
    return None


def _resource(service_name: str | None = None) -> Any:
    from opentelemetry.sdk.resources import Resource

    attributes = {
        "service.name": service_name or settings.otel_service_name,
        "service.version": settings.otel_service_version,
        "deployment.environment": settings.app_env,
    }
    raw = settings.otel_resource_attributes
    if type(raw) is str and len(raw) <= 4096:
        for item in raw.split(",")[:32]:
            key, separator, value = item.partition("=")
            if separator and key.strip() in attributes:
                attributes[key.strip()] = value.strip()
    allowed = {}
    for key, value in attributes.items():
        if type(value) is not str:
            continue
        if (
            (key == "service.name" and value in _RESOURCE_SERVICES)
            or (key == "service.version" and _VERSION.fullmatch(value))
            or (key == "deployment.environment" and value in _RESOURCE_ENVIRONMENTS)
        ):
            allowed[key] = value
    # Resource.create() merges env/detector attributes, bypassing the allowlist.
    return Resource(allowed)


def _sampler() -> Any:
    from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON, ParentBased, TraceIdRatioBased

    if settings.otel_traces_sampler == "always_on":
        return ALWAYS_ON
    if settings.otel_traces_sampler == "always_off":
        return ALWAYS_OFF
    if settings.otel_traces_sampler != "parentbased_traceidratio":
        raise ValueError("Unsupported telemetry sampler")
    ratio = settings.otel_traces_sampler_arg
    if type(ratio) not in {float, int} or not math.isfinite(ratio) or not 0 <= ratio <= 1:
        raise ValueError("Invalid telemetry sampling ratio")
    return ParentBased(TraceIdRatioBased(ratio))


def _limit(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Invalid telemetry limit")
    return value


class _SafeExporter:
    """Prevent SDK processor logging from serializing a failing export's exception chain."""

    def __init__(self, exporter):
        self._exporter = exporter

    def export(self, spans):
        from opentelemetry.sdk.trace.export import SpanExportResult

        try:
            return self._exporter.export(spans)
        except Exception:
            return SpanExportResult.FAILURE

    def shutdown(self):
        try:
            self._exporter.shutdown()
        except Exception:
            pass


def configure_tracing(service_name: str | None = None, exporter: Any = None) -> Tracer:
    """Initialize once, including after failure; explicit exporters are offline test hooks."""
    global _configured, _tracer_provider, _tracer
    with _configuration_lock:
        if _configured:
            return _tracer or _NOOP_TRACER
        provider = None
        processor = None
        selected_exporter = None
        _tracer = _NOOP_TRACER
        try:
            if settings.otel_tracing_enabled or exporter is not None:
                from opentelemetry.sdk.trace import SpanLimits, TracerProvider
                from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

                resource = _resource(service_name)
                provider = TracerProvider(
                    resource=resource, sampler=_sampler(), shutdown_on_exit=False,
                    span_limits=SpanLimits(
                        max_attributes=_MAX_ATTRIBUTES, max_span_attributes=_MAX_ATTRIBUTES,
                        max_events=0, max_links=_MAX_LINKS,
                        max_attribute_length=128, max_span_attribute_length=128,
                        max_event_attributes=0, max_link_attributes=0,
                    ),
                )
                candidate_tracer = provider.get_tracer(
                    "attention-router", resource.attributes.get("service.version", ""),
                )
                selected_exporter = exporter
                if selected_exporter is None and settings.otel_exporter_otlp_endpoint:
                    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

                    endpoint = settings.otel_exporter_otlp_endpoint
                    parsed = urlsplit(endpoint)
                    if (settings.otel_exporter_otlp_protocol != "http/protobuf"
                            or parsed.scheme not in {"http", "https"} or not parsed.hostname
                            or parsed.username or parsed.password or parsed.query or parsed.fragment):
                        raise ValueError("Invalid telemetry endpoint/protocol")
                    export_timeout = _limit(settings.otel_export_timeout_millis, 1, 10000)
                    queue = _limit(settings.otel_batch_max_queue_size, 1, 8192)
                    batch = _limit(settings.otel_batch_max_export_batch_size, 1, min(queue, 1024))
                    delay = _limit(settings.otel_batch_schedule_delay_millis, 10, 60000)
                    _limit(settings.otel_flush_timeout_millis, 0, 10000)
                    selected_exporter = OTLPSpanExporter(endpoint=endpoint, timeout=export_timeout / 1000)
                    processor = BatchSpanProcessor(
                        _SafeExporter(selected_exporter), max_queue_size=queue,
                        max_export_batch_size=batch,
                        schedule_delay_millis=delay,
                        # SDK 1.27 uses this as the default wait budget for force_flush.
                        export_timeout_millis=settings.otel_flush_timeout_millis,
                    )
                elif selected_exporter is not None:
                    processor = SimpleSpanProcessor(_SafeExporter(selected_exporter))
                if processor is not None:
                    provider.add_span_processor(processor)
                _tracer = candidate_tracer
                _tracer_provider = provider
        except Exception:
            # Also close components that failed before being attached to the provider.
            try:
                if processor is not None:
                    processor.shutdown()
                elif selected_exporter is not None:
                    selected_exporter.shutdown()
            except Exception:
                pass
            if provider is not None:
                try:
                    provider.shutdown()
                except Exception:
                    pass
            _tracer_provider = None
            _tracer = _NOOP_TRACER
        finally:
            _configured = True
        return _tracer


def get_tracer(service_name: str | None = None) -> Tracer:
    try:
        return configure_tracing(service_name=service_name) if not _configured else (_tracer or _NOOP_TRACER)
    except Exception:
        return _NOOP_TRACER


def safe_set_attribute(span: Span, key: str, value: Any) -> None:
    try:
        value = _safe_attribute(key, value)
        if value is not None and span.is_recording():
            span.set_attribute(key, value)
    except Exception:
        pass


def safe_record_exception(span: Span, exc: BaseException) -> None:
    """Idempotent type metadata, never exception text, stack, chain or an event."""
    try:
        import builtins

        # Never trust a user-defined exception class name (it can contain content).
        error_type = next((name for name in _ENUM_ATTRIBUTES["error.type"]
                           if type(exc) is getattr(builtins, name)), "Exception")
        safe_set_attribute(span, "error.type", error_type)
    except Exception:
        pass


def set_outcome(span: Span, outcome: str, *, error: bool = False) -> None:
    safe_set_attribute(span, "attention.outcome", outcome)
    try:
        span.set_status(Status(StatusCode.ERROR if error else StatusCode.OK))
    except Exception:
        pass


def message_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def identifier_hash(value: str) -> str:
    return message_text_hash(value)[:16]


def domain_attributes(**values: Any) -> dict[str, Any]:
    return {key: clean for key, value in values.items()
            if key in _UUID_ATTRIBUTES and (clean := _safe_attribute(key, value)) is not None}


def _clean_span_context(value: SpanContext) -> SpanContext:
    if not isinstance(value, SpanContext) or not value.is_valid:
        return trace.INVALID_SPAN_CONTEXT
    # Vendor tracestate is free-form and is not authorized in this gate.
    return SpanContext(value.trace_id, value.span_id, value.is_remote, value.trace_flags)


def _parent_context(context: Any) -> otel_context.Context:
    empty = otel_context.Context()
    try:
        if context is not None and not isinstance(context, otel_context.Context):
            return empty
        parent = _clean_span_context(trace.get_current_span(context).get_span_context())
        return trace.set_span_in_context(trace.NonRecordingSpan(parent), empty)
    except Exception:
        return empty


def _safe_links(links: list[Link] | None) -> list[Link]:
    result = []
    try:
        for link in (links or [])[:_MAX_LINKS]:
            context = _clean_span_context(link.context)
            if context.is_valid:
                result.append(Link(context))
    except Exception:
        pass
    return result


class _SafeSpan(Span):
    """Keep direct Span API calls inside the same privacy/failure boundary."""

    def __init__(self, span: Span, *, defer_status: bool = False):
        self._span = span
        self._defer_status = defer_status
        self._status_code = None

    def _call(self, method, *args, **kwargs):
        try:
            return getattr(self._span, method)(*args, **kwargs)
        except Exception:
            return None

    def get_span_context(self):
        try:
            return _clean_span_context(self._call("get_span_context"))
        except Exception:
            return trace.INVALID_SPAN_CONTEXT

    def is_recording(self):
        try:
            return bool(self._call("is_recording"))
        except Exception:
            return False

    def set_attribute(self, key, value):
        safe_set_attribute(self._span, key, value)

    def set_attributes(self, attributes):
        try:
            for key, value in attributes.items():
                self.set_attribute(key, value)
        except Exception:
            pass

    def set_status(self, status, description=None):
        try:
            code = status.status_code if isinstance(status, Status) else status
            if isinstance(code, StatusCode):
                if self._defer_status:
                    # SDK OK is final: wait until the operation has really finished.
                    if self._status_code is not StatusCode.ERROR:
                        self._status_code = code
                else:
                    self._call("set_status", Status(code))
        except Exception:
            pass

    def update_name(self, name):
        self._call("update_name", name if type(name) is str and name in _SPAN_NAMES else "attention.operation")

    def add_event(self, name, attributes=None, timestamp=None):
        # No free-form events are authorized; errors use an attribute, once per span.
        pass

    def record_exception(self, exception, attributes=None, timestamp=None, escaped=False):
        safe_record_exception(self, exception)

    def end(self, end_time=None):
        try:
            if self._status_code is not None:
                self._call("set_status", Status(self._status_code))
        except Exception:
            pass
        self._call("end", end_time=end_time)


@contextmanager
def start_span(
    name: str,
    *,
    attributes: dict[str, Any] | None = None,
    context: Any = None,
    links: list[Link] | None = None,
) -> Iterator[Span]:
    """Keep setup/cleanup errors separate from the single functional yield."""
    span = _SafeSpan(trace.INVALID_SPAN)
    token = None
    previous_context = None
    try:
        previous_context = otel_context.get_current()
        name = name if type(name) is str and name in _SPAN_NAMES else "attention.operation"
        span = _SafeSpan(get_tracer().start_span(
            name, context=_parent_context(context), links=_safe_links(links),
            record_exception=False, set_status_on_exception=False,
        ), defer_status=True)
        span.set_attributes(attributes or {})
        token = otel_context.attach(trace.set_span_in_context(span, otel_context.Context()))
    except Exception:
        span.end()
        span = _SafeSpan(trace.INVALID_SPAN)
        try:
            token = otel_context.attach(trace.set_span_in_context(span, otel_context.Context()))
        except Exception:
            pass
    try:
        yield span
    except BaseException as exc:
        safe_record_exception(span, exc)
        set_outcome(span, "FAILED", error=True)
        raise
    finally:
        if token is not None:
            try:
                otel_context.detach(token)
            except Exception:
                pass
            # The API can swallow a detach failure. Restore the saved context if
            # it was left attached, without allowing cleanup to replace an error.
            try:
                if previous_context is not None and otel_context.get_current() is not previous_context:
                    otel_context.attach(previous_context)
            except Exception:
                pass
        span.end()


@contextmanager
def ensure_message_trace(attributes: dict[str, Any] | None = None) -> Iterator[Span]:
    current = current_span()
    if current.get_span_context().is_valid:
        yield current
        return
    with start_span("attention.message", attributes=attributes) as span:
        yield span


def current_span() -> Span:
    try:
        if get_tracer() is _NOOP_TRACER:
            return _SafeSpan(trace.INVALID_SPAN)
        span = trace.get_current_span()
        return span if isinstance(span, _SafeSpan) else _SafeSpan(span)
    except Exception:
        return _SafeSpan(trace.INVALID_SPAN)


def message_trace(function: Callable[..., _T]) -> Callable[..., _T]:
    """Create a root message span only when no upstream context is active."""
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> _T:
        with ensure_message_trace() as root:
            result = function(*args, **kwargs)
            if isinstance(result, dict):
                safe_set_attribute(root, "attention.response_generated", bool(result.get("lia_speech")))
                safe_set_attribute(root, "attention.external_send", False)
                set_outcome(root, "NO_RESPONSE" if not result.get("lia_speech") else "RESPONSE_GENERATED")
            return result

    return wrapped


def traced_span(name: str) -> Callable[[Callable[..., _T]], Callable[..., _T]]:
    def decorator(function: Callable[..., _T]) -> Callable[..., _T]:
        if inspect.iscoroutinefunction(function):
            raise TypeError("traced_span currently supports synchronous functions only")

        @wraps(function)
        def wrapped(*args: Any, **kwargs: Any) -> _T:
            with start_span(name):
                return function(*args, **kwargs)

        return wrapped

    return decorator


def inject_trace_context(carrier: dict[str, str]) -> dict[str, str]:
    """W3C traceparent only; never inherit baggage or vendor tracestate."""
    try:
        carrier.pop("traceparent", None)
        carrier.pop("tracestate", None)
        carrier.pop("baggage", None)
        if get_tracer() is not _NOOP_TRACER:
            _PROPAGATOR.inject(carrier, context=_parent_context(None))
    except Exception:
        pass
    return carrier


def extract_trace_context(carrier: dict[str, str] | None) -> Any:
    empty = otel_context.Context()
    try:
        if type(carrier) is not dict:
            return empty
        parent = carrier.get("traceparent")
        if type(parent) is not str or not _TRACEPARENT.fullmatch(parent):
            return empty
        extracted = _PROPAGATOR.extract({"traceparent": parent}, context=empty)
        if not isinstance(extracted, otel_context.Context):
            return empty
        span_context = trace.get_current_span(extracted).get_span_context()
        if (not span_context.is_valid or not span_context.is_remote
                or span_context.trace_id != int(parent[3:35], 16)
                or span_context.span_id != int(parent[36:52], 16)):
            return empty
        return _parent_context(extracted)
    except Exception:
        return empty


def link_from_carrier(carrier: dict[str, str] | None) -> list[Link]:
    try:
        context = extract_trace_context(carrier)
        span_context = trace.get_current_span(context).get_span_context()
        return _safe_links([Link(span_context)])
    except Exception:
        return []


def configure_test_tracing(exporter: Any) -> None:
    """Install an in-memory/test provider without changing global SDK state."""
    reset_tracing()
    configure_tracing(service_name="attention-router-test", exporter=exporter)


def flush_tracing(timeout_millis: int | None = None) -> bool:
    try:
        timeout = settings.otel_flush_timeout_millis if timeout_millis is None else timeout_millis
        timeout = _limit(timeout, 0, 10000)
        return bool(_tracer_provider and _tracer_provider.force_flush(timeout))
    except Exception:
        return False


def shutdown_tracing() -> None:
    try:
        if _tracer_provider is not None:
            _tracer_provider.shutdown()
    except Exception:
        pass


def reset_tracing() -> None:
    global _configured, _tracer_provider, _tracer
    with _configuration_lock:
        shutdown_tracing()
        _configured = False
        _tracer_provider = None
        _tracer = None
