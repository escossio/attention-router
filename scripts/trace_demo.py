"""Print two sanitized in-memory Live Flow trace trees."""

from __future__ import annotations

import argparse

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from attention_router.observability.tracing import (
    configure_test_tracing,
    configure_tracing,
    flush_tracing,
    reset_tracing,
    safe_record_exception,
    safe_set_attribute,
    set_outcome,
    shutdown_tracing,
    start_span,
)


def emit_happy() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    configure_test_tracing(exporter)
    with start_span("attention.message") as root:
        set_outcome(root, "DELIVERED")
        for name in (
            "inbound.receive", "actor.resolve", "policy.resolve", "decision.evaluate",
            "behavior.generate", "repetition_guard.evaluate", "memory.archive",
            "autonomy.evaluate", "execution.intent", "outbox.enqueue", "transport.send",
        ):
            with start_span(name) as span:
                set_outcome(span, "OK")
    return exporter


def emit_suppressed() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    configure_test_tracing(exporter)
    with start_span("attention.message") as root:
        for name in ("inbound.receive", "actor.resolve", "decision.evaluate", "behavior.generate"):
            with start_span(name) as span:
                set_outcome(span, "OK")
        with start_span("repetition_guard.evaluate") as guard:
            safe_set_attribute(guard, "attention.repetition.semantic_repeat", True)
            safe_set_attribute(guard, "attention.repetition.state_changed", False)
            safe_set_attribute(guard, "attention.repetition.suppressed", True)
            set_outcome(guard, "SUPPRESSED")
        set_outcome(root, "SUPPRESSED")
    return exporter


def print_tree(exporter: InMemorySpanExporter) -> None:
    spans = exporter.get_finished_spans()
    by_parent = {}
    for span in spans:
        parent_id = span.parent.span_id if span.parent else None
        by_parent.setdefault(parent_id, []).append(span)
    root = next(span for span in spans if span.name == "attention.message")

    def visit(span, prefix=""):
        outcome = span.attributes.get("attention.outcome", "UNSET")
        print(f"{prefix}{span.name} [status={span.status.status_code.name}, outcome={outcome}]")
        for child in by_parent.get(span.context.span_id, []):
            visit(child, prefix + "├── ")

    print(f"DEMO_TRACE_ID={root.context.trace_id:032x}")
    visit(root)
    names = {span.name for span in spans}
    if "outbox.enqueue" not in names:
        print("outbox.enqueue = NOT_REACHED")
    if "transport.send" not in names:
        print("transport.send = NOT_REACHED")


def emit_otlp(path: str, endpoint: str) -> str:
    from attention_router.config import settings

    settings.otel_tracing_enabled = True
    settings.otel_exporter_otlp_endpoint = endpoint
    reset_tracing()
    configure_tracing(service_name="attention-router-trace-demo")
    with start_span("attention.message") as root:
        trace_id = f"{root.get_span_context().trace_id:032x}"
        if path in {"happy", "legacy"}:
            if path == "legacy":
                safe_set_attribute(root, "attention.response_source", "LEGACY")
            else:
                safe_set_attribute(root, "attention.response_source", "ANDY_BEHAVIOR")
            set_outcome(root, "DELIVERED")
            names = (
                "inbound.receive", "actor.resolve", "policy.resolve", "decision.evaluate",
                "behavior.generate", "repetition_guard.evaluate", "memory.archive",
                "autonomy.evaluate", "execution.intent", "outbox.enqueue", "transport.send",
            )
            for name in names:
                with start_span(name) as span:
                    set_outcome(span, "OK")
        elif path == "suppressed":
            for name in ("inbound.receive", "actor.resolve", "decision.evaluate", "behavior.generate"):
                with start_span(name) as span:
                    set_outcome(span, "OK")
            with start_span("repetition_guard.evaluate") as guard:
                safe_set_attribute(guard, "attention.repetition.semantic_repeat", True)
                safe_set_attribute(guard, "attention.repetition.state_changed", False)
                safe_set_attribute(guard, "attention.repetition.suppressed", True)
                safe_set_attribute(guard, "attention.repetition.reason", "SEMANTIC_REPEAT_NO_STATE_CHANGE")
                set_outcome(guard, "SUPPRESSED")
            set_outcome(root, "SUPPRESSED")
        elif path == "failed":
            safe_set_attribute(root, "attention.response_source", "NONE")
            for name in ("inbound.receive", "actor.resolve"):
                with start_span(name) as span:
                    set_outcome(span, "OK")
            with start_span("decision.evaluate") as decision:
                safe_record_exception(decision, RuntimeError("synthetic stage3 failure"))
                set_outcome(decision, "FAILED", error=True)
            set_outcome(root, "FAILED", error=True)
        else:
            # Deliberately attempt unsafe attributes; the tracing abstraction must drop them.
            safe_set_attribute(root, "attention.phone", "+5500000000029")
            safe_set_attribute(root, "attention.otp", "123456")
            safe_set_attribute(root, "attention.api_key", "sk-fake-stage2")
            safe_set_attribute(root, "attention.message_text", "synthetic sensitive text")
            safe_set_attribute(root, "attention.message_text_sha256", "0" * 64)
            safe_set_attribute(root, "attention.response_source", "NONE")
            set_outcome(root, "NO_RESPONSE")
    flush_tracing()
    shutdown_tracing()
    return trace_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--otlp", action="store_true")
    parser.add_argument("--endpoint", default="http://127.0.0.1:4318/v1/traces")
    parser.add_argument(
        "--path", choices=("happy", "suppressed", "privacy", "legacy", "failed"), default="happy"
    )
    args = parser.parse_args()
    if args.otlp:
        print(f"TRACE_ID={emit_otlp(args.path, args.endpoint)}")
    else:
        print("HAPPY_PATH")
        print_tree(emit_happy())
        reset_tracing()
        print("SUPPRESSION_PATH")
        print_tree(emit_suppressed())
        reset_tracing()
