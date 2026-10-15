#!/usr/bin/env python3
"""Idempotent Zabbix ROC provisioning for per-core CPU and Tempo signals.

Existing administrative credentials are read from root-only local config. The
Telegram webhook is deliberately NOT enabled until the private bot connection
has been tested and its receiver configured.
"""
import importlib.util
import json
from pathlib import Path

ROC_CONFIG = Path("/etc/andy-ops-network/provision.json")
NETWORK_PROVISION = Path("/midia/agt01-storage/attention-router/worktrees/"
                         "mgmt-unified-otel-20261015/ops/observability/network-osi/provision.py")
GROUP = "22"
NODES = {
    "agt01": {"name": "Andy Engine - AGT01", "cores": 8, "hostid": "10785"},
    "ci01": {"name": "Andy CPU - CI01", "cores": 4},
    "ci02": {"name": "Andy CPU - CI02", "cores": 4},
    "ci03": {"name": "Andy CPU - CI03", "cores": 8},
}


def client():
    config = json.loads(ROC_CONFIG.read_text())
    admin = json.loads(Path(config["admin_credentials_file"]).read_text())
    spec = importlib.util.spec_from_file_location("network_provision", NETWORK_PROVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Api({"api_url": config["api_url"], **admin})


def ensure_host(api, config):
    rows = api.call("host.get", {"filter": {"host": config["name"]},
                                 "output": ["hostid", "host", "status"]})
    if rows:
        hostid = rows[0]["hostid"]
    else:
        result = api.call("host.create", {
            "host": config["name"], "name": config["name"],
            "status": 0, "groups": [{"groupid": GROUP}],
            "description": "Read-only per-core CPU projection via AGT01 SSH trapper.",
        })
        hostid = result["hostids"][0]
    if "hostid" in config and str(config["hostid"]) != str(hostid):
        raise RuntimeError("EXISTING_HOST_ID_CHANGED")
    return str(hostid)


def ensure_item(api, hostid, key, name, value_type, history="7d", units=""):
    rows = api.call("item.get", {
        "hostids": [hostid], "filter": {"key_": key},
        "output": ["itemid", "type", "value_type", "status", "key_"],
    })
    if rows:
        item = rows[0]
        if str(item["type"]) != "2" or str(item["value_type"]) != str(value_type):
            raise RuntimeError("EXISTING_ITEM_CONFLICT_" + key)
        if str(item["status"]) != "0":
            api.call("item.update", {"itemid": item["itemid"], "status": 0})
        return item["itemid"]
    item = {
        "hostid": hostid, "name": name, "key_": key,
        "type": 2, "value_type": value_type,
        "history": history, "trends": "30d" if value_type in (0, 3) else "0",
        "status": 0, "description": "Readonly AGT telemetry; 60s collection.",
    }
    if units:
        item["units"] = units
    return api.call("item.create", item)["itemids"][0]


def ensure_trigger(api, hostid, desc, expression, severity, tag_value):
    rows = api.call("trigger.get", {
        "hostids": [hostid], "filter": {"description": desc},
        "output": ["triggerid", "expression", "status"],
    })
    data = {
        "description": desc, "expression": expression, "priority": severity,
        "status": 0, "type": 0,
        "tags": [{"tag": "ops_alert", "value": tag_value},
                 {"tag": "owner", "value": "andy-ops"}],
    }
    if rows:
        data["triggerid"] = rows[0]["triggerid"]
        api.call("trigger.update", data)
        return data["triggerid"]
    return api.call("trigger.create", data)["triggerids"][0]


def main():
    api = client()
    for label, node in NODES.items():
        hostid = ensure_host(api, node)
        for key, name, kind, units in [
            ("andy.cpu.max_pct", "CPU: hottest individual core", 0, "%"),
            ("andy.cpu.mean_pct", "CPU: average of all cores", 0, "%"),
            ("andy.cpu.hot_id", "CPU: hottest core index", 3, ""),
            ("andy.cpu.hot_idle", "CPU: hot core while aggregate idle", 3, ""),
            ("andy.cpu.probe_ok", "CPU: remote probe succeeded", 3, ""),
        ]:
            ensure_item(api, hostid, key, name, kind, units=units)
        for core in range(node["cores"]):
            ensure_item(api, hostid, f"andy.cpu.core_pct[{core}]",
                        f"CPU: core {core} utilization", 0, units="%")
        host = node["name"]
        expr = (f"min(/{host}/andy.cpu.hot_idle,5m)=1 and "
                f"count(/{host}/andy.cpu.hot_idle,5m)>=4")
        ensure_trigger(api, hostid, "OPS_CPU_CORE_HOT_IDLE: "+label.upper(),
                       expr, 3, "cpu_hot")
        ensure_trigger(
            api, hostid, "OPS_CPU_OBSERVABILITY_LOST: "+label.upper(),
            f"nodata(/{host}/andy.cpu.probe_ok,5m)=1", 2, "cpu_gap")
        print("PROVISIONED_CPU",label,"cores",node["cores"],"host",hostid)
    hostid = NODES["agt01"]["hostid"]
    host = NODES["agt01"]["name"]
    for key, name in [
        ("andy.tempo.scheduler_empty.count", "Tempo: idle scheduler log events / min"),
        ("andy.tempo.log_probe_ok", "Tempo: Docker log collector available"),
        ("andy.tempo.query_ok", "Tempo: Ready and search API reachable"),
        ("andy.alert.test_cpu", "[TEST ONLY] CPU incident notification"),
        ("andy.alert.test_tempo", "[TEST ONLY] Tempo incident notification"),
    ]:
        ensure_item(api, hostid, key, name, 3)
    ensure_trigger(
        api, hostid, "OPS_TEMPO_SCHEDULER_EMPTY: informational log event",
        f"sum(/{host}/andy.tempo.scheduler_empty.count,5m)>=4",
        1, "tempo_idle")
    ensure_trigger(
        api, hostid, "OPS_TEMPO_QUERY_UNAVAILABLE: real query failure",
        f"max(/{host}/andy.tempo.query_ok,3m)=0 and count(/{host}/andy.tempo.query_ok,3m)>=2",
        4, "tempo_failed")
    ensure_trigger(api, hostid, "[TEST] OPS_CPU_CORE_HOT_IDLE notification",
                   f"last(/{host}/andy.alert.test_cpu)=1", 1, "test_cpu")
    ensure_trigger(api, hostid, "[TEST] OPS_TEMPO notification",
                   f"last(/{host}/andy.alert.test_tempo)=1", 1, "test_tempo")
    print("PROVISIONED_TEMPO_AND_TEST_EVENTS")
    print("TELEGRAM_REMAINS_DISABLED_UNTIL_PRIVATE_CREDENTIALS_AND_CHAT_CONFIGURED")


if __name__ == "__main__":
    main()
