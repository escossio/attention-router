#!/usr/bin/env python3
"""Generate the separate Grafana dashboard using the existing ROC datasource."""

import json
from pathlib import Path

DS = {"type": "alexanderzobnin-zabbix-datasource", "uid": "roc-zabbix"}


def target(item, ref="A", text=False):
    return {
        "refId": ref, "datasource": DS, "queryType": "2" if text else "0",
        "group": {"filter": "Andy Runtime / Network"},
        "host": {"filter": "MikroTik - Andy Runtime Network"},
        "item": {"filter": item}, "application": {"filter": ""},
        "itemTag": {"filter": ""}, "tags": {"filter": ""},
        "functions": [], "textFilter": "", "resultFormat": "time_series",
        "schema": 12, "options": {"useZabbixValueMapping": False,
        "disableDataAlignment": True, "useTrends": "false",
        "showDisabledItems": False, "skipEmptyValues": False},
    }


def mapping(values):
    return [{"type": "value", "options": {str(k): {"text": v[0], "color": v[1], "index": i}
            for i, (k, v) in enumerate(values.items())}}]


def dashboard():
    panels = []

    def add(kind, title, x, y, w, h, targets=None, **extra):
        panel = {"id": len(panels) + 1, "type": kind, "title": title,
                 "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": targets or [],
                 "datasource": DS, "fieldConfig": {"defaults": {"noValue": "UNKNOWN",
                     "thresholds": {"mode": "absolute", "steps": [{"color": "blue", "value": None}]}},
                     "overrides": []}}
        panel.update(extra)
        panels.append(panel)
        return panel

    def row(title, y):
        add("row", title, 0, y, 24, 1, collapsed=False, panels=[])

    def stat(title, item, x, y, w=8, text=False, values=None, unit="none", relative="90s"):
        p = add("stat", title, x, y, w, 4, [target(item, text=text)], timeFrom=relative,
                options={"colorMode": "background", "graphMode": "none", "justifyMode": "center",
                         "orientation": "auto", "textMode": "value",
                         "reduceOptions": {"calcs": ["lastNotNull"], "fields": "/^(?!Time$).+/" if text else "", "values": False}})
        p["fieldConfig"]["defaults"].update(unit=unit)
        if values:
            p["fieldConfig"]["defaults"]["mappings"] = mapping(values)
        return p

    row("NETWORK HEALTH", 0)
    health = {0: ("UNKNOWN", "gray"), 1: ("PASS", "green"),
              2: ("DEGRADED", "orange"), 3: ("FAIL", "red")}
    for i, name in enumerate(["L1 PHYSICAL", "L2 DATA LINK", "L3 NETWORK"]):
        stat(name, "NETWORK HEALTH: " + name, i * 8, 1, values=health)
    for i, label in enumerate(["domains", "complete", "incomplete", "reachable"]):
        stat(label.upper(), "NETWORK: " + label, i * 6, 5, w=6)

    row("L1 — PHYSICAL", 9)
    stat("Declared trunk · association below", "L1 PHYSICAL: parent/trunk", 0, 10, w=6, text=True)
    stat("Link / carrier", "L1 PHYSICAL: oper", 6, 10, w=3,
         values={1: ("UP", "green"), 2: ("DOWN", "red"), 0: ("UNKNOWN", "gray")})
    stat("Admin", "L1 PHYSICAL: admin", 9, 10, w=3,
         values={1: ("ENABLED", "green"), 2: ("DISABLED", "red"), 0: ("UNKNOWN", "gray")})
    stat("Speed · Mbps", "L1 PHYSICAL: speed", 12, 10, w=3,
         values={0: ("NO LINK", "red")})
    stat("Duplex", "L1 PHYSICAL: duplex", 15, 10, w=3, text=True, relative="1h")
    stat("RX errors / s", "L1 PHYSICAL: rx_errors", 18, 10, w=3)
    stat("RX discards / s", "L1 PHYSICAL: rx_discards", 21, 10, w=3)
    p = add("timeseries", "Parent RX/TX, errors and discards · 30s", 0, 14, 12, 5,
            [target("/^L1 PHYSICAL: (rx|tx|rx_errors|tx_errors|rx_discards|tx_discards)$/")],
            options={"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["lastNotNull"]},
                     "tooltip": {"mode": "multi", "sort": "none"}})
    p["fieldConfig"]["defaults"].update(custom={"drawStyle": "line", "lineWidth": 1, "fillOpacity": 5})
    p = stat("Parent association · read-only RouterOS evidence", "NETWORK: Parent / trunk evidence", 12, 14,
             w=12, text=True, relative="1h")
    p["gridPos"]["h"] = 5
    p["options"]["text"] = {"valueSize": 15}
    p["description"] = "Topology is UNKNOWN after one hour without a new RouterOS snapshot. ifStack and duplex were not exposed; neither is assumed."

    def domain_table(title, numeric, textual, x, y, height, order, overrides, prefix="VLAN", row_label="VLAN"):
        queries = []
        if numeric:
            queries.append(target("/^" + prefix + " .* \\/ (" + numeric + ")$/", "A"))
        if textual:
            queries.append(target("/^" + prefix + " .* \\/ (" + textual + ")$/", "B", text=True))
        p = add("table", title, x, y, 24, height, queries, timeFrom="10m",
                options={"showHeader": True, "cellHeight": "md", "sortBy": [{"displayName": "VLAN", "desc": False}]},
                transformations=[
                    {"id": "reduce", "filter": {"id": "byRefId", "options": "A"},
                     "options": {"reducers": ["last"], "includeTimeField": False, "mode": "seriesToRows"}},
                    {"id": "convertFieldType", "options": {"conversions": [{"targetField": "Last", "destinationType": "string"}]}},
                    {"id": "reduce", "filter": {"id": "byRefId", "options": "B"},
                     "options": {"reducers": ["last"], "includeTimeField": False, "mode": "seriesToRows"}},
                    {"id": "merge", "options": {}},
                    {"id": "extractFields", "options": {"source": "Field", "format": "regexp",
                        "regExp": "/^" + prefix + " (?<VLAN>[0-9]+) \\/ (?<COLUMN>.+)$/", "replace": False}},
                    {"id": "groupingToMatrix", "options": {"rowField": "VLAN", "columnField": "COLUMN", "valueField": "Last"}},
                    {"id": "convertFieldType", "options": {"conversions": [
                        {"targetField": name, "destinationType": "number"}
                        for name in ["VLAN\\COLUMN"] + numeric.split("|")]}},
                    {"id": "organize", "options": {"indexByName": {v: i for i, v in enumerate(order)},
                        "renameByName": {"VLAN\\COLUMN": row_label}}},
                ])
        p["fieldConfig"]["defaults"]["custom"] = {"align": "auto", "cellOptions": {"type": "auto"}}
        p["fieldConfig"]["overrides"] = overrides + [override(name, unit="string") for name in textual.split("|")]
        p["description"] = "Live Zabbix LLD values. Blank or missing information is UNKNOWN. Metadata is discovered every 5 minutes; live metrics and neighbors every 30 seconds."
        return p

    def override(name, maps=None, unit=None):
        properties = []
        if maps:
            properties += [{"id": "mappings", "value": mapping(maps)},
                           {"id": "custom.cellOptions", "value": {"type": "color-text"}}]
        if unit:
            properties.append({"id": "unit", "value": unit})
        return {"matcher": {"id": "byName", "options": name}, "properties": properties}

    arp = {0: ("UNKNOWN", "gray"), 1: ("COMPLETE", "green"),
           2: ("INCOMPLETE", "orange"), 3: ("ABSENT", "red")}
    oper = {1: ("UP", "green"), 2: ("DOWN", "red"), 0: ("UNKNOWN", "gray")}
    domain_table("Physical members of the actual VLAN parent and declared trunk",
                 "ADMIN|OPER|SPEED|RX_ERRORS|TX_ERRORS|RX_DISCARDS|TX_DISCARDS", "INTERFACE|ROLE", 0, 19, 12,
                 ["VLAN\\COLUMN", "INTERFACE", "ROLE", "ADMIN", "OPER", "SPEED", "RX_ERRORS", "TX_ERRORS", "RX_DISCARDS", "TX_DISCARDS"],
                 [override("OPER", oper), override("SPEED", unit="Mbps")], prefix="PHYSICAL", row_label="ifIndex")
    physical = panels[-1]
    physical["transformations"].insert(-1, {"id": "filterByValue", "options": {
        "type": "exclude", "match": "any", "filters": [{"fieldName": "ROLE", "config": {"id": "equal", "options": {"value": "Other / not asserted"}}}]}})
    row("L2 — DATA LINK", 31)
    domain_table("VLAN | INTERFACE | ifIndex | OPER | ARP/NEIGHBOR | MAC",
                 "ifIndex|OPER|ARP/NEIGHBOR", "INTERFACE|MAC", 0, 32, 12,
                 ["VLAN\\COLUMN", "INTERFACE", "ifIndex", "OPER", "ARP/NEIGHBOR", "MAC"],
                 [override("OPER", oper), override("ARP/NEIGHBOR", arp)])

    row("L3 — NETWORK", 44)
    domain_table("VLAN | NETWORK | GATEWAY | ENDPOINT | ARP | ICMP | LOSS | RTT",
                 "ARP/NEIGHBOR|ICMP|LOSS|RTT", "NETWORK|GATEWAY|ENDPOINT", 0, 45, 12,
                 ["VLAN\\COLUMN", "NETWORK", "GATEWAY", "ENDPOINT", "ARP/NEIGHBOR", "ICMP", "LOSS", "RTT"],
                 [override("ARP/NEIGHBOR", arp), override("ICMP", {0: ("UNKNOWN", "gray"), 1: ("UNREACHABLE", "red"), 2: ("REACHABLE", "green")}),
                  override("LOSS", unit="percent"),
                  override("RTT", {0: ("NO RESPONSE", "gray")}, "s")])
    add("text", "Evidence and causality", 0, 57, 24, 3,
        options={"mode": "markdown", "content":
        "MikroTik → SNMPv3 authPriv → Zabbix ROC → Grafana ROC → Andy Ops / MGMT. "
        "Native ICMP: 10s, 3 packets per cycle, unavailability after 3 consecutive cycles. "
        "Trigger dependencies: L1_PARENT_DOWN → L2_DOMAIN_DOWN → L3_NEIGHBOR_INCOMPLETE → L3_ENDPOINT_UNREACHABLE. "
        "Downstream measurements remain visible when alerts are suppressed. UNKNOWN never means PASS. "
        "Browser refresh does not trigger SNMP polling."})
    return {"uid": "roc-network-osi", "title": "ROC-NETWORK / OSI", "schemaVersion": 39,
            "version": 1, "editable": True, "refresh": "10s", "timezone": "browser",
            "time": {"from": "now-10m", "to": "now"}, "tags": ["roc", "network", "osi"],
            "templating": {"list": []}, "annotations": {"list": []}, "panels": panels}


if __name__ == "__main__":
    Path(__file__).with_name("roc-network-osi.json").write_text(json.dumps(dashboard(), indent=2) + "\n")
