"""Fail-safe OpenTelemetry primitives for the Attention Router flow.

Telemetry is deliberately kept behind this module.  Application code can use
the helpers without knowing whether an SDK provider or the API no-op tracer is
active, and exporter failures never become application failures.
"""

from __future__ import annotations

import hashlib
import inspect
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Iterator, TypeVar

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.trace import Link, Span, Status, StatusCode, Tracer

from attention_router.config import settings


_T = TypeVar("_T")
_tracer_provider: Any = None
_tracer: Tracer | None = None
_configured = False
_BLOCKED_KEY_PARTS = ("phone", "token", "secret", "password", "cookie", "otp", "api_key", "spoken_text")
_SAFE_TEXT_KEY_SUFFIXES = ("_sha256", "_length", "_present")


def _resource(service_name: str | None = None) -> Any:
    from opentelemetry.sdk.resources import DEPLOYMENT_ENVIRONMENT, SERVICE_NAME, SERVICE_VERSION, Resource

    attributes = {
        SERVICE_NAME: service_name or settings.otel_service_name,
        SERVICE_VERSION: settings.otel_service_version,
        DEPLOYMENT_ENVIRONMENT: settings.app_env,
    }
    for item in (settings.otel_resource_attributes or "").split(","):
        if "=" in item:
            key, value = item.split("=", 1)
            if key.strip() and value.strip() and not any(part in key.casefold() for part in _BLOCKED_KEY_PARTS):
                attributes[key.strip()] = value.strip()
    return Resource.create(attributes)


def _sampler() -> Any:
    from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON, ParentBased, TraceIdRatioBased

    if settings.otel_traces_sampler == "always_on":
        return ALWAYS_ON
    if settings.otel_traces_sampler == "always_off":
        return ALWAYS_OFF
    ratio = max(0.0, min(1.0, settings.otel_traces_sampler_arg))
    return ParentBased(TraceIdRatioBased(ratio))


def configure_tracing(service_name: str | None = None, exporter: Any = None) -> Tracer:
    """Configure one local provider; setup is best-effort and never required."""
    global _configured, _tracer_provider, _tracer
    if not settings.otel_tracing_enabled and exporter is None:
        _configured = True
        _tracer = trace.get_tracer("attention-router")
        return _tracer
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor

        provider = TracerProvider(resource=_resource(service_name), sampler=_sampler())
        selected_exporter = exporter
        if selected_exporter is None and settings.otel_exporter_otlp_endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            selected_exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)
        if selected_exporter is not None:
            if exporter is not None:
                provider.add_span_processor(SimpleSpanProcessor(selected_exporter))
            else:
                from opentelemetry.sdk.trace.export import BatchSpanProcessor

                provider.add_span_processor(BatchSpanProcessor(
                    selected_exporter,
                    max_queue_size=settings.otel_batch_max_queue_size,
                    max_export_batch_size=settings.otel_batch_max_export_batch_size,
                    schedule_delay_millis=settings.otel_batch_schedule_delay_millis,
                    export_timeout_millis=settings.otel_export_timeout_millis,
                ))
        _tracer_provider = provider
        _tracer = provider.get_tracer("attention-router", settings.otel_service_version)
        _configured = True
    except Exception:
        # A broken SDK/exporter must degrade to the API's no-op tracer.
        _tracer_provider = None
        _tracer = trace.get_tracer("attention-router")
        _configured = True
    return _tracer


def get_tracer(service_name: str | None = None) -> Tracer:
    if not _configured:
        configure_tracing(service_name=service_name)
    return _tracer or trace.get_tracer("attention-router")


def safe_set_attribute(span: Span, key: str, value: Any) -> None:
    """Set only scalar, already-sanitized telemetry values."""
    lowered = key.casefold()
    if value is None or not span.is_recording():
        return
    if any(part in lowered for part in _BLOCKED_KEY_PARTS) or (
        "text" in lowered and not lowered.endswith(_SAFE_TEXT_KEY_SUFFIXES)
    ):
        return
    try:
        if isinstance(value, (str, bool, int, float)):
            span.set_attribute(key, value)
        elif isinstance(value, (list, tuple)) and all(isinstance(item, (str, bool, int, float)) for item in value):
            span.set_attribute(key, list(value))
    except Exception:
        pass


def safe_record_exception(span: Span, exc: BaseException) -> None:
    try:
        if span.is_recording():
            span.record_exception(exc)
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
    """Return a sanitized subset of functional identifiers for span attrs."""
    allowed = {
        "attention.inbound_event_id", "attention.interaction_id", "attention.actor_id",
        "attention.binding_id", "attention.decision_id", "attention.execution_intent_id",
        "attention.outbox_id", "attention.correlation_id",
    }
    return {key: value for key, value in values.items() if key in allowed and value is not None}


@contextmanager
def start_span(
    name: str,
    *,
    attributes: dict[str, Any] | None = None,
    context: Any = None,
    links: list[Link] | None = None,
) -> Iterator[Span]:
    """Start a span and preserve the original exception semantics."""
    tracer = get_tracer()
    with tracer.start_as_current_span(name, context=context, links=links) as span:
        for key, value in (attributes or {}).items():
            safe_set_attribute(span, key, value)
        try:
            yield span
        except Exception as exc:
            safe_record_exception(span, exc)
            set_outcome(span, "FAILED", error=True)
            raise


@contextmanager
def ensure_message_trace(attributes: dict[str, Any] | None = None) -> Iterator[Span]:
    current = trace.get_current_span()
    span_context = current.get_span_context()
    if span_context.is_valid:
        yield current
        return
    with start_span("attention.message", attributes=attributes) as span:
        yield span


def current_span() -> Span:
    return trace.get_current_span()


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
                result = function(*args, **kwargs)
                return result

        return wrapped

    return decorator


def inject_trace_context(carrier: dict[str, str]) -> dict[str, str]:
    """Inject W3C trace context into a non-functional job metadata carrier."""
    try:
        propagate.inject(carrier)
    except Exception:
        pass
    return carrier


def extract_trace_context(carrier: dict[str, str] | None) -> Any:
    try:
        return propagate.extract(carrier or {})
    except Exception:
        return otel_context.get_current()


def link_from_carrier(carrier: dict[str, str] | None) -> list[Link]:
    context = extract_trace_context(carrier)
    span_context = trace.get_current_span(context).get_span_context()
    return [Link(span_context)] if span_context.is_valid else []


def configure_test_tracing(exporter: Any) -> None:
    """Install an in-memory/test provider without changing global SDK state."""
    configure_tracing(service_name="attention-router-test", exporter=exporter)


def flush_tracing(timeout_millis: int | None = None) -> bool:
    try:
        return bool(_tracer_provider and _tracer_provider.force_flush(timeout_millis or settings.otel_export_timeout_millis))
    except Exception:
        return False


def shutdown_tracing() -> None:
    try:
        if _tracer_provider:
            _tracer_provider.shutdown()
    except Exception:
        pass


def reset_tracing() -> None:
    global _configured, _tracer_provider, _tracer
    _configured = False
    _tracer_provider = None
    _tracer = None
