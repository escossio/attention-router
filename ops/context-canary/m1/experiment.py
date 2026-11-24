"""M1-side telemetry, kept outside the application and its authorization path."""

import hashlib
import json
import logging
import os
from pathlib import Path
import resource
import socket
from threading import Event, Thread
from time import time_ns
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from attention_router.config import settings
from attention_router.observability.tracing import configure_tracing, flush_tracing, reset_tracing

VERSION = "2"


def digest(value):
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class Inactive:
    def offer(self, headers, _workload):
        return None, headers

    def complete(self, *_):
        pass

    def resources(self):
        pass

    def finish(self):
        pass


class EventFile(logging.FileHandler):
    def emit(self, record):
        if type(record.msg) is str and record.msg.startswith("g7_event "):
            super().emit(record)


class Experiment:
    def __init__(self, directory, instrumented):
        self.application = logging.getLogger("attention_router.observability.private_context")
        self.application.setLevel(logging.INFO)
        self.client = logging.getLogger("andy.m1.client")
        self.sink = EventFile(directory / "events.log", encoding="utf-8")
        self.sink.setFormatter(logging.Formatter("%(message)s"))
        self.client.propagate = False
        self.client.setLevel(logging.INFO)
        for logger in (self.application, self.client):
            logger.addHandler(self.sink)
        self.pool_sampler = None
        if instrumented:
            self.stop_pool_sampler = Event()
            self.pool_sampler = Thread(target=self.sample_pool, daemon=True)
            self.pool_sampler.start()

    def sample_pool(self):
        import psycopg
        from sqlalchemy.engine import make_url
        from attention_router.observability.pg_pool_lifecycle import (
            active_leases, report_driver_wait, report_progress,
        )

        target = make_url(os.environ["DATABASE_URL"])
        if target.host != "127.0.0.1" or target.database != "attention_router_test":
            self.event("pool_sampler", sampler_state="unexpected_database")
            return
        try:
            with psycopg.connect(
                dbname=target.database, user=target.username, password=target.password,
                host=target.host, port=target.port, connect_timeout=2, autocommit=True,
                options="-c statement_timeout=350",
            ) as observer:
                self.event("pool_sampler", sampler_state="available")
                while not self.stop_pool_sampler.wait(0.2):
                    leases = active_leases()
                    for lease in leases:
                        report_progress(lease)
                    active = [(lease, lease.sql_started) for lease in leases
                              if lease.backend_pid and lease.sql_started is not None]
                    if active:
                        rows = dict(observer.execute(
                            "SELECT pid, wait_event_type FROM pg_stat_activity WHERE pid = ANY(%s)",
                            ([lease.backend_pid for lease, _ in active],),
                        ).fetchall())
                        for lease, started in active:
                            report_driver_wait(lease, started, rows.get(lease.backend_pid))
        except psycopg.Error:
            self.event("pool_sampler", sampler_state="unavailable")

    def event(self, event, **fields):
        self.client.info("g7_event %s", json.dumps({
            "event": event, "timestamp_ns": time_ns(), **fields,
        }, separators=(",", ":")))

    def offer(self, headers, workload):
        trace = uuid4().hex
        parent = uuid4().hex[:16]
        self.event("client_offer", trace=trace, workload=workload)
        return trace, {**headers, "traceparent": f"00-{trace}-{parent}-01"}

    def complete(self, trace, workload, answer, elapsed):
        self.event("client_result", trace=trace, workload=workload,
                   outcome=answer, duration_ns=round(elapsed * 1_000_000_000))

    def resources(self):
        usage = resource.getrusage(resource.RUSAGE_SELF)
        self.event("resources", cpu_seconds=round(usage.ru_utime + usage.ru_stime, 3),
                   rss_peak_kib=usage.ru_maxrss, input_blocks=usage.ru_inblock,
                   output_blocks=usage.ru_oublock)

    def finish(self):
        if self.pool_sampler is not None:
            self.stop_pool_sampler.set()
            self.pool_sampler.join(timeout=2)
        self.event("load_complete")
        flush_tracing()
        # Keep recording through the enclosing test's re-open/revocation.
        self.sink.flush()


def experiment(bodies, controls, order, cases, segment_count):
    configured = os.environ.get("M1_TELEMETRY_DIR")
    if not configured:
        return Inactive()
    if os.environ["M1_TELEMETRY_MODE"] == "instrumented":
        endpoint = os.environ["M1_TRACE_ENDPOINT"]
        target = urlsplit(endpoint)
        if (target.scheme != "http" or target.hostname != "127.0.0.1" or not target.port
                or target.path != "/v1/traces" or target.username or target.password
                or target.query or target.fragment):
            raise ValueError("M1 loopback telemetry endpoint required")
        # The generic pytest fixture intentionally disables telemetry for all tests.
        # Re-enable only after this synthetic, opted-in trial has reached M1.
        settings.otel_tracing_enabled = True
        settings.otel_exporter_otlp_endpoint = endpoint
        settings.otel_service_name = "attention-router-private-context"
        reset_tracing()
        readiness = configure_tracing().start_span("context.request")
        try:
            assert readiness.is_recording(), "M1 trace recording unavailable"
        finally:
            readiness.end()
    root = Path(configured)
    identity = os.environ.get("M1_RUN_ID")
    run_id = str(UUID(identity)) if identity else str(uuid4())
    if identity is not None and (identity != run_id or UUID(run_id).version != 4):
        raise ValueError("M1 supervised run ID must be a canonical UUIDv4")
    folder = root / run_id
    folder.mkdir(mode=0o700, parents=True)
    config = {
        "waves": int(os.environ["M1_WAVES"]), "offers_per_minute": 20,
        "burst": int(os.environ.get("M1_BURST", "4")),
        "client_connections": 4, "client_timeout_seconds": 10, "db_pool_size": 2,
        "db_pool_overflow": 0, "db_pool_timeout_seconds": 2, "db_statement_timeout_ms": 8000,
        "db_lock_timeout_ms": 4000, "uvicorn_limit": 6, "cpu_affinity_count": len(os.sched_getaffinity(0)),
    }
    metadata = {
        "id": run_id, "sha": os.environ["M1_SHA"], "tree": os.environ["M1_TREE"],
        "db_image": os.environ["M1_DB_IMAGE"], "app_image": "native_worker_python",
        "worker": socket.gethostname().split(".")[0], "config": config,
        "segments": segment_count,
        "corpus_digest": digest({"sources": bodies, "controls": controls,
                                  "owner_fixture_blob": os.environ["M1_OWNER_FIXTURE_BLOB"]}),
        "queries_digest": digest([(case, cases[case][2]) for case in order]),
        "condition": os.environ["M1_CONDITION"], "started_ns": time_ns(),
        "instrumentation": VERSION, "mode": os.environ["M1_TELEMETRY_MODE"],
    }
    (folder / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(f"M1_EXPERIMENT_ID={run_id}", flush=True)
    return Experiment(folder, metadata["mode"] == "instrumented")
