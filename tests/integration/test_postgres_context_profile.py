"""Synthetic comparison of the same graph reads with and without a source filter."""

from collections import Counter
import json
import re
from resource import RUSAGE_SELF, getrusage
from time import perf_counter, process_time

import pytest
from sqlalchemy import event, text

from attention_router.application.context_validity import ContextValidity
import test_postgres_context_transactions as context_fixtures
from test_cognitive_observation import _attachment


pytestmark = pytest.mark.postgres
world = context_fixtures.world
boundary = context_fixtures.boundary


def test_context_cost_at_supported_scales(Session, world):
    engine = Session.kw["bind"]
    total = len(world["ids"])
    for size in (3, 10, 25, 50, 100, 200):
        if size > total:
            with Session.begin() as session:
                ids = [_attachment(session, f"profile-{i}", 3)[0] for i in range(total, size)]
            for offset in range(0, len(ids), 50):
                with Session.begin() as session:
                    boundary(session).observe(ids[offset:offset + 50])
            total = size

        for with_source_filter in (False, True):
            samples = {}
            expected = None
            for sample in range(6):
                batch_sources = bool(sample % 2)
                count = 0
                by_table = Counter()
                patterns = Counter()

                def counted(_connection, _cursor, statement, _params, _context, _many):
                    nonlocal count
                    count += 1
                    table = re.search(r"\bFROM\s+([a-z_]+)", statement, re.IGNORECASE)
                    by_table[table.group(1) if table else statement.split(maxsplit=1)[0]] += 1
                    patterns[statement] += 1

                with Session() as session:
                    scope = dict(boundary(session).arguments)
                options = {"source_allowed": lambda message, **context: True} if with_source_filter else {}
                event.listen(engine, "before_cursor_execute", counted)
                elapsed, cpu = perf_counter(), process_time()
                try:
                    with Session() as session:
                        context = ContextValidity(session, **scope, **options)
                        token, graph = context._graph(batch_sources=batch_sources)
                        graph_queries = count
                        wall_ms = round((perf_counter() - elapsed) * 1000, 1)
                        cpu_ms = round((process_time() - cpu) * 1000, 1)
                        locked_relations = session.scalar(text(
                            "SELECT count(DISTINCT relation) FROM pg_locks "
                            "WHERE pid=pg_backend_pid() AND granted AND locktype='relation'"
                        ))
                finally:
                    event.remove(engine, "before_cursor_execute", counted)
                actual = sum(node.source_type == "TIMELINE_EVENT" for node in graph.nodes)
                assert actual == size
                projection = (token, graph)
                if expected is None:
                    expected = projection
                assert projection == expected
                samples[batch_sources] = graph_queries
                print("context_profile " + json.dumps({
                    "events": size, "source_filter": with_source_filter, "sql_queries": graph_queries,
                    "batch_sources": batch_sources, "sample": sample // 2,
                    "wall_ms": wall_ms, "runner_cpu_ms": cpu_ms, "locked_relations": locked_relations,
                    "queries_by_first_relation": dict(by_table), "statement_shapes": len(patterns),
                    "runner_peak_rss_kib": getrusage(RUSAGE_SELF).ru_maxrss,
                }, sort_keys=True))
            if size == 25:
                assert samples[True] <= samples[False] // 2


def test_batch_matches_scalar_after_source_withdrawal_and_consent_filter(Session, world):
    first, second, _ = world["ids"]
    with Session.begin() as session:
        boundary(session).revoke_message(second)
    with Session() as session:
        scope = dict(boundary(session).arguments)
        context = ContextValidity(
            session, **scope, source_allowed=lambda message, **_: message.id in {first, second},
        )
        token, graph = context.graph()
        assert (token, graph) == context._graph(batch_sources=False)
        observed = {node.source_id for node in graph.nodes if node.source_type == "TIMELINE_EVENT"}
        assert len(observed) == 1
        session.rollback()
    with Session() as session:
        context = ContextValidity(session, **scope, source_allowed=lambda message, **_: message.id == second)
        assert context.graph() == context._graph(batch_sources=False)
        assert context.compile("attachment observed")[1].items == ()
