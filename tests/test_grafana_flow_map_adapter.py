"""Flow Map bridge contracts: faked DB, no credentials, no live services."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from unittest.mock import patch

BASE = Path(__file__).parents[1]
FOLDER = BASE / "ops/observability/grafana/flow"
sys.path.insert(0, str(FOLDER))
PATH = FOLDER / "map/adapter.py"
SPEC = importlib.util.spec_from_file_location("flow_map_adapter", PATH)
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.query = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query):
        self.query = query

    def fetchmany(self, count):
        assert self.query == bridge.QUERY
        assert count == 33
        return self.rows[:count]


class FakeConnection:
    def __init__(self, rows):
        self.cur = FakeCursor(rows)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self.cur


class FakeTempo:
    def __init__(self):
        self.calls = []

    def view(self, tid, start, end):
        self.calls.append((tid,start,end))
        return {
            "metadata": [{"trace_id": "a"*32, "relationship": "NO_LINK_OBSERVED"}],
            "latency": [
                {"service": "attention-router-transport", "name": "transport.receive",
                 "trace_id": "a"*32, "span_id": "1"*16, "parent_span_id": "",
                 "duration_ms": 5, "status": "OK"},
                {"service": "attention-router-ingress", "name": "attention.message",
                 "trace_id": "a"*32, "span_id": "2"*16, "parent_span_id": "1"*16,
                 "duration_ms": 7, "status": "UNSET"},
            ],
        }


def sample_rows(now):
    return [
        ("container", now - 35, {
            "entity_key": "andy-whatsapp-runtime/transport/1", "state": "running",
            "health": "healthy", "name": "runtime-transport", "host_name": "host-a"
        }),
        ("container", now - 20, {
            "entity_key": "attention-router/ingress/1", "state": "running",
            "health": "healthy", "name": "runtime-ingress", "host_name": "host-a",
        }),
        ("container", now - 300, {
            "entity_key": "attention-router-live-flow-stage4/worker/1",
            "state": "running", "health": "healthy", "name": "runtime-worker"
        }),
        ("attachment", now - 35, {
            "entity_key": "andy-whatsapp-runtime/transport/1",
            "ip": "10.30.20.2", "network": "test-net"
        }),
    ]


def fake_connector(rows, connection_calls):
    def connector(**kwargs):
        connection_calls.append(kwargs)
        return FakeConnection(rows)
    return connector


def test_read_only_view_and_selected_metadata_columns():
    calls=[]
    result=bridge.ZabbixView(fake_connector(sample_rows(1700000000), calls)).read()
    assert len(result[0])==3 and len(result[1])==1
    assert calls[0]["user"]==bridge.DB_ROLE
    assert calls[0]["options"].find("default_transaction_read_only=on")>=0
    assert calls[0]["passfile"]=="/run/secrets/zabbix_pgpass"
    assert bridge.QUERY.startswith("SELECT kind,clock,value FROM public.roc_flow_map_v1_inventory")
    assert " " not in calls[0]["user"]


def test_projection_uses_fresh_history_and_never_optimistic_stale_health():
    now=1700000000
    calls=[]
    zbx=bridge.ZabbixView(fake_connector(sample_rows(now), calls))
    t=FakeTempo()
    m=bridge.MapProjection(zabbix=zbx, tempo=t)
    with patch.object(bridge.time, "time", return_value=now):
        result=m.view("latest",(now-300)*1000,now*1000)
    assert result["schema"]=="roc.flow-map.v1"
    assert len(result["nodes"])==3
    assert [x["mainStat"] for x in result["nodes"]] == [
        "HEALTHY", "HEALTHY", "UNKNOWN"
    ]
    assert [x["secondaryStat"] for x in result["nodes"]] == [
        "REACHED", "REACHED", "NOT_REACHED"
    ]
    assert result["network_evidence"]==[]
    assert result["nodes"][0]["interfaces"] == [{"ip":"10.30.20.2","network":"test-net"}]
    assert not any(x.get("trace_correlation")=="VERIFIED" for x in result["network_evidence"])
    assert len([e for e in result["edges"] if e["relationship"]=="PARENT_CHILD"])==1
    assert t.calls[0][0]=="latest"


def test_unknown_or_extra_db_entity_rejected():
    calls=[]
    zbx=bridge.ZabbixView(fake_connector([("container",1700000000,{
        "entity_key":"unlisted/svc/1","health":"healthy"})], calls))
    try:
        zbx.read()
    except ValueError as exc:
        assert "scope" in str(exc)
    else:
        raise AssertionError("unauthorized entity accepted")


def test_bounded_rows_fail_closed():
    rows=[("container",1,{"entity_key":"andy-whatsapp-runtime/transport/1"})]*34
    try:
        bridge.ZabbixView(fake_connector(rows,[])).read()
    except ValueError as exc:
        assert "size" in str(exc)
    else:
        raise AssertionError("row limit bypassed")


def test_bad_trace_and_time_cannot_query_db():
    calls=[]
    zbx=bridge.ZabbixView(fake_connector(sample_rows(1700000000), calls))
    t=FakeTempo()
    m=bridge.MapProjection(zabbix=zbx,tempo=t)
    now=1700000000
    with patch.object(bridge.time,"time",return_value=now):
        for tid,start,end in [
            ("../",None,None),("b"*33,None,None),
            ("latest",now*1000,now*1000),
            ("latest",(now-90000)*1000,now*1000),
        ]:
            try:
                m.view(tid,start,end)
            except ValueError:
                pass
            else:
                raise AssertionError("unbounded query accepted")
    assert calls==[] and t.calls==[]


def test_auth_secret_never_in_http_or_projected_data():
    text=(FOLDER / "map/adapter.py").read_text()
    assert "os.environ.get(\"ZABBIX_ADMIN_PASSWORD\"" not in text
    assert "PGPASSFILE" in text
    assert "password=" not in text
    assert 'def log_message(self, *_args):' in text


def test_view_ddl_scope_explicit_and_does_not_grant_admin():
    sql=(FOLDER / "map/zabbix_view.sql").read_text()
    assert "security_barrier=true" in sql
    assert "SELECT *" not in sql
    assert "REVOKE ALL" in sql
    assert all(ent in sql for ent in bridge.SERVICE_ENTITIES.values())
    assert "DROP TABLE" not in sql and "GRANT ALL" not in sql


def test_short_traces_only_one_missing_nybble_allowed():
    now = 1700000000
    t = FakeTempo()
    m = bridge.MapProjection(zabbix=bridge.ZabbixView(fake_connector(sample_rows(now), [])), tempo=t)
    with patch.object(bridge.time, "time", return_value=now):
        for bad in ("f", "f"*30, "0"*33):
            try:
                m.view(bad, None, None)
            except bridge.InvalidSelection:
                pass
            else:
                raise AssertionError("invalid id accepted")
    assert not t.calls


def test_unavailable_inventory_never_returns_stale_cached_green():
    class FailedZabbix:
        def read(self):
            raise RuntimeError("Zabbix polling unavailable")
    m = bridge.MapProjection(zabbix=FailedZabbix(), tempo=FakeTempo())
    with patch.object(bridge.time, "time", return_value=1700000000):
        try:
            m.view("latest", None, None)
        except RuntimeError as exc:
            assert "unavailable" in str(exc)
        else:
            raise AssertionError("missing evidence accepted")


def test_tempo_outage_keeps_zabbix_topology_without_fake_reached():
    class FailedTempo:
        def view(self, *args):
            raise RuntimeError("Tempo temporarily unavailable")
    now = 1700000000
    m = bridge.MapProjection(
        zabbix=bridge.ZabbixView(fake_connector(sample_rows(now),[])),
        tempo=FailedTempo(),
    )
    with patch.object(bridge.time, "time", return_value=now):
        data = m.view("latest",None,None)
    assert len(data["nodes"]) == 3
    assert [n["mainStat"] for n in data["nodes"]] == [
        "HEALTHY", "HEALTHY", "UNKNOWN"
    ]
    assert all(n["participation"] == "UNSELECTED" for n in data["nodes"])
    assert all(e["relationship"] == "EXPECTED_TOPOLOGY" for e in data["edges"])
    assert data["source_status"]["tempo"] == "UNAVAILABLE"
    assert data["evidence_completeness"] == "PARTIAL"
