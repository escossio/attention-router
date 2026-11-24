import importlib.util
import json
import logging
from pathlib import Path
import sqlite3

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeout

from attention_router.config import settings
from attention_router.observability.context_performance import (
    Outcome, Stage, classify, observe_database, observe_query, phase, result,
)
from attention_router.observability.tracing import configure_test_tracing, reset_tracing


def test_private_phases_have_trace_ids_sql_counts_and_no_payload(monkeypatch, caplog):
    monkeypatch.setattr(settings, "context_performance_enabled", True)
    exporter = InMemorySpanExporter()
    configure_test_tracing(exporter)
    engine = create_engine("sqlite://")
    try:
        observe_database(engine)
        observe_database(engine)
        with caplog.at_level(logging.INFO, logger="attention_router.observability.private_context"):
            with observe_query("26faf9d5-c4d0-452f-9078-139dcf332a0c", "00-" + "a"*32 + "-" + "b"*16 + "-01"):
                with phase(Stage.CONSENT), engine.connect() as db:
                    db.execute(text("SELECT 'private-marker'"))
                result(Outcome.AUTHORITY)
        events = [json.loads(record.getMessage().removeprefix("g7_event "))
                  for record in caplog.records if record.getMessage().startswith("g7_event ")]
        assert {event["trace"] for event in events} == {"a"*32}
        assert events[-1]["outcome"] == "authority_refusal"
        assert events[-2]["sql_count"] == 1
        assert all(event.get("duration_ns", 0) >= 0 for event in events)
        assert "private-marker" not in caplog.text
        spans = exporter.get_finished_spans()
        assert [span.name for span in spans] == ["context.consent", "context.request"]
        assert spans[0].parent.span_id == spans[1].context.span_id
        assert spans[1].attributes["g7.outcome"] == "authority_refusal"
    finally:
        engine.dispose()
        reset_tracing()


def test_fault_classification_preserves_driver_ambiguity_and_anchor_cause(tmp_path):
    class LockTimeout(Exception):
        sqlstate = "55P03"

    class CancelledStatement(Exception):
        sqlstate = "57014"

    assert classify(OperationalError("private", {}, LockTimeout())) is Outcome.LOCK
    assert classify(OperationalError("private", {}, CancelledStatement())) is Outcome.SQL_CANCEL
    assert classify(PoolTimeout("pool")) is Outcome.POOL
    assert classify(TimeoutError()) is Outcome.APP_TIMEOUT
    assert classify(ValueError("STAGED_COMPUTE_EXPIRED")) is Outcome.DEADLINE
    assert classify(ValueError("CONTEXT_CONSENT_REVISION_STALE")) is Outcome.REVOKED
    with sqlite3.connect(tmp_path / "anchor.sqlite") as writer, sqlite3.connect(
        tmp_path / "anchor.sqlite", timeout=0,
    ) as reader:
        writer.execute("CREATE TABLE epoch(i INTEGER)")
        writer.commit()
        writer.execute("BEGIN EXCLUSIVE")
        try:
            try:
                reader.execute("SELECT * FROM epoch")
            except sqlite3.OperationalError as underlying:
                raise OSError("protected") from underlying
        except OSError as error:
            assert classify(error) is Outcome.ANCHOR_LOCK
        else:
            raise AssertionError("expected exclusive SQLite lock")


def test_projection_uses_interval_union_and_flags_different_environment():
    path = Path(__file__).resolve().parents[1] / "ops/observability/grafana/g7/reader.py"
    spec = importlib.util.spec_from_file_location("g7_projection", path)
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    assert reader.union_duration([(10, 30), (20, 40), (50, 60)]) == 40
    sample = [{"event":"end", "stage":"context.worker", "duration_ns":50, "monotonic_ns":60},
              {"event":"end", "stage":"context.capture", "duration_ns":20, "monotonic_ns":35},
              {"event":"end", "stage":"context.revalidate", "duration_ns":18, "monotonic_ns":45}]
    assert reader.trace_timeline(sample)[0]["exclusive_observed_ns"] == 20
    selected = {"p95_ms": 200, "abstained": 1}
    baseline = {"p95_ms": 100, "abstained": 0}
    cfg = {"config": {"pool": 2}, "worker": "ci02"}
    values, flags = reader.compare(selected, baseline, cfg, cfg | {"worker": "ci01"})
    assert values[0] == {"metric": "p95_ms", "selected": 200, "baseline": 100,
                         "delta": 100, "delta_pct": 100}
    assert values[1]["delta_pct"] is None
    assert flags == [{"comparable": False, "differences": "worker"}]

    traces = ("grounded", "abstention", "refused", "disconnected", "pending")
    events = [{"event": "client_offer", "trace": trace, "timestamp_ns": i, "workload": "load"}
              for i, trace in enumerate(traces)]
    events.extend({"event": "client_result", "trace": trace, "outcome": outcome, "timestamp_ns": 10 + i}
                  for i, (trace, outcome) in enumerate(zip(
                      traces, ("grounded", "abstention", "empty_response", "client_timeout"),
                  )))
    events.extend({"event": "end", "trace": trace, "stage": "context.request",
                   "outcome": "authority_refusal", "timestamp_ns": 20, "duration_ns": 4}
                  for trace in ("refused", "disconnected"))
    counts = reader.summarize(events)["summary"][0]
    assert (counts["offered"], counts["completed"], counts["legitimate_refusals"]) == (5, 4, 2)
    assert (counts["grounded"], counts["abstained"], counts["grounding_pct"]) == (1, 1, 100)
    assert counts["availability_pct"] == 66.67


def test_real_pool_exhaustion_uses_non_authority_outcome(tmp_path):
    pool = create_engine(f"sqlite:///{tmp_path / 'pool.sqlite'}", pool_size=1,
                         max_overflow=0, pool_timeout=.01)
    try:
        with pool.connect():
            try:
                pool.connect()
            except PoolTimeout as error:
                assert classify(error) is Outcome.POOL
            else:
                raise AssertionError("expected exhaustion of the only slot")
    finally:
        pool.dispose()


def test_pool_acquisition_and_return_are_observed_before_request_completes(tmp_path, monkeypatch, caplog):
    from time import monotonic_ns
    from attention_router.observability.context_performance import request_sample
    from attention_router.observability.pg_pool_lifecycle import active_leases, report_progress

    monkeypatch.setattr(settings, "context_performance_enabled", True)
    engine = create_engine(f"sqlite:///{tmp_path / 'lease.sqlite'}", pool_size=1,
                           max_overflow=0, pool_timeout=.01)
    path = Path(__file__).resolve().parents[1] / "ops/observability/grafana/g7/reader.py"
    spec = importlib.util.spec_from_file_location("g7_projection", path)
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)

    def observed():
        return [json.loads(record.getMessage().removeprefix("g7_event ")) for record in caplog.records
                if record.getMessage().startswith("g7_event ")]

    trace = "a" * 32
    try:
        observe_database(engine)
        with caplog.at_level(logging.INFO, logger="attention_router.observability.private_context"):
            with observe_query("26faf9d5-c4d0-452f-9078-139dcf332a0c", f"00-{trace}-{'b' * 16}-01"):
                with phase(Stage.CAPTURE), engine.begin() as connection:
                    assert connection.execute(text("SELECT 'outside-telemetry'")).scalar() == "outside-telemetry"
                    hold, = active_leases()
                    report_progress(hold)
                    during = reader.project_pool(observed(), trace)
                    [row] = during["pool_open"]
                    assert (row["phase"], row["state"], row["driver_active"], row["sql_count"]) == (
                        Stage.CAPTURE.value, "ocupada", False, 1,
                    )
                    try:
                        engine.connect()
                    except PoolTimeout:
                        pass
                    else:
                        raise AssertionError("expected real queue timeout")
                    request_sample.get().disconnected = monotonic_ns()
                finished = reader.project_pool(observed(), trace)
                assert not finished["pool_open"]
                [summary] = finished["pool_summary"]
                assert (summary["free"], summary["waiting"], summary["timeouts"]) == (1, 0, 1)
                returned, = [row for row in finished["pool_trace"] if row["event"] == "pool_checkin"]
                assert returned["pool_begins"] == returned["pool_commits"] == 1
                assert returned["pool_after_disconnect_ns"] > 0
                assert returned["pool_sql_ns"] + returned["pool_outside_cursor_ns"] == returned["duration_ns"]
                assert "outside-telemetry" not in caplog.text
    finally:
        engine.dispose()


def test_inference_wait_and_work_are_measured_separately(monkeypatch, caplog):
    from threading import Event, Lock, Thread
    from time import sleep
    from attention_router.observability.context_performance import inference_lock

    monkeypatch.setattr(settings, "context_performance_enabled", True)
    exporter = InMemorySpanExporter()
    configure_test_tracing(exporter)
    lock = Lock()
    acquired = Event()

    def hold_lock():
        with lock:
            acquired.set()
            sleep(.04)

    worker = Thread(target=hold_lock)
    worker.start()
    assert acquired.wait(.5)
    try:
        with caplog.at_level(logging.INFO, logger="attention_router.observability.private_context"):
            with observe_query("26faf9d5-c4d0-452f-9078-139dcf332a0c", None):
                with inference_lock(lock), phase(Stage.INFERENCE):
                    sleep(.01)
        spans = {span.name: span for span in exporter.get_finished_spans()}
        wait, run = (spans[name] for name in ("context.inference_wait", "context.inference"))
        assert wait.end_time <= run.start_time
        assert wait.attributes["g7.duration_ms"] > run.attributes["g7.duration_ms"] >= 8
    finally:
        worker.join(.5)
        reset_tracing()
