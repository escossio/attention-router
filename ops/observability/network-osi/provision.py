#!/usr/bin/env python3
"""Provision only the NETWORK / OSI template and host through the ROC API.

Settings and secrets are supplied in a root-only JSON file outside Git.
No password bootstrap, database access, container or network mutation occurs here.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import urllib.request

ROOT = Path(__file__).resolve().parent
TEMPLATE = "Template Andy Runtime Network - MikroTik SNMPv3"
GROUP = "Andy Runtime / Network"
IF = "1.3.6.1.2.1.2.2.1."
IFX = "1.3.6.1.2.1.31.1.1.1."
ARP = "1.3.6.1.2.1.4.22.1."
PARSER = (ROOT / "snmp.js").read_text()


def js(code):
    return [{"type": 21, "params": code, "error_handler": 0}]


def extract(oid, missing=None):
    step = {"type": 28, "params": oid + "\n0", "error_handler": 0}
    if missing is not None:
        step.update(error_handler=2, error_handler_params=str(missing))
    return [step]


def private_json(path):
    path = Path(path)
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("Settings must be owned by the operator and mode 0600")
    return json.loads(path.read_text())


class Api:
    def __init__(self, config):
        self.url = config["api_url"]
        self.auth = None
        if config.get("token_file"):
            token = Path(config["token_file"])
            if token.stat().st_mode & 0o077:
                raise ValueError("Token file must be private")
            self.auth = token.read_text().strip()
        else:
            self.auth = self.call("user.login", {
                "username": config["username"], "password": config["password"],
            })

    def call(self, method, params):
        headers = {"Content-Type": "application/json-rpc"}
        if self.auth:
            headers["Authorization"] = "Bearer " + self.auth
        request = urllib.request.Request(self.url, data=json.dumps({
            "jsonrpc": "2.0", "method": method, "params": params, "id": 1,
        }).encode(), headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
        if "error" in result:
            # Never echo API payloads, which can contain host secret macros.
            raise RuntimeError(method + ": " + result["error"]["message"])
        return result["result"]


def provision(api, settings):
    def group(kind):
        rows = api.call(kind + ".get", {"output": ["groupid"], "filter": {"name": GROUP}})
        return rows[0]["groupid"] if rows else api.call(kind + ".create", {"name": GROUP})["groupids"][0]

    templates = api.call("template.get", {"output": ["templateid"], "filter": {"host": TEMPLATE}})
    tid = templates[0]["templateid"] if templates else api.call("template.create", {
        "host": TEMPLATE, "groups": [{"groupid": group("templategroup")}],
        "description": "Read-only SNMPv3 native master/dependent collection; runtime remains untouched.",
    })["templateids"][0]
    ids = {}

    def item(key, name, typ=18, value_type=3, master=None, pre=None, **extra):
        body = {"hostid": tid, "key_": key, "name": name, "type": typ,
                "value_type": value_type, "delay": "0", "history": "7d",
                "trends": "0" if value_type in (1, 2, 4) else "30d",
                "tags": [{"tag": "component", "value": "network-osi"}]}
        if master:
            body["master_itemid"] = ids[master]
        if pre:
            body["preprocessing"] = pre
        body.update(extra)
        old = api.call("item.get", {"hostids": tid, "output": ["itemid"], "filter": {"key_": key}})
        if old:
            body["itemid"] = old[0]["itemid"]
            body.pop("hostid")
            api.call("item.update", body)
            ids[key] = body["itemid"]
        else:
            ids[key] = api.call("item.create", body)["itemids"][0]

    item("andy.discovery.raw", "NETWORK: Interface and address discovery", 20, 4,
         delay="5m", history="1d", snmp_oid="walk[" + ",".join([
             IF + "1", IF + "2", IF + "3", IFX + "1", IFX + "18",
             "1.3.6.1.2.1.4.20.1.2", "1.3.6.1.2.1.4.20.1.3"]) + "]")
    item("andy.metrics.raw", "NETWORK: Interface metrics master", 20, 4,
         delay="30s", history="1d", snmp_oid="walk[" + ",".join([
             IFX + "1", IF + "7", IF + "8", IFX + "15", IFX + "6", IFX + "10",
             IF + "14", IF + "20", IF + "13", IF + "19"]) + "]")
    item("andy.neighbors.raw", "NETWORK: IPv4 neighbors master", 20, 4,
         delay="30s", history="1d", snmp_oid="walk[" + ",".join(ARP + str(i) for i in (2, 4)) + "]")
    item("andy.topology.evidence", "NETWORK: RouterOS topology evidence", 2, 4,
         description="Read-only RouterOS snapshot; nodata after one hour makes topology UNKNOWN.")
    item("andy.topology.consistent", "NETWORK: Trunk / VLAN parent association", master="andy.topology.evidence",
         pre=js("return JSON.parse(value).consistent ? 1 : 0;"))
    item("andy.topology.note", "NETWORK: Parent / trunk evidence", value_type=4, master="andy.topology.evidence",
         pre=js("return JSON.parse(value).note;"))
    item("andy.parent.duplex", "L1 PHYSICAL: duplex", value_type=4, master="andy.topology.evidence",
         pre=js("var p=JSON.parse(value).physical; return p && p['full-duplex']!==undefined ? (p['full-duplex'] ? 'FULL' : 'HALF') : 'UNKNOWN';"))

    metrics = {
        "admin": (IF + "7", 3, ""), "oper": (IF + "8", 3, ""),
        "speed": (IFX + "15", 3, "Mbps"),
        "rx": (IFX + "6", 0, "Bps"), "tx": (IFX + "10", 0, "Bps"),
        "rx_errors": (IF + "14", 0, "eps"), "tx_errors": (IF + "20", 0, "eps"),
        "rx_discards": (IF + "13", 0, "eps"), "tx_discards": (IF + "19", 0, "eps"),
    }
    for metric, (oid, vt, units) in metrics.items():
        pre = extract(oid + ".{$ANDY.PARENT.INDEX}")
        if metric in ("rx", "tx", "rx_errors", "tx_errors", "rx_discards", "tx_discards"):
            pre += [{"type": 10, "params": "", "error_handler": 0}]
        item("andy.parent." + metric, "L1 PHYSICAL: " + metric, value_type=vt,
             master="andy.metrics.raw", pre=pre, units=units)
    item("andy.parent.name", "L1 PHYSICAL: parent/trunk", value_type=4,
         master="andy.metrics.raw", pre=extract(IFX + "1.{$ANDY.PARENT.INDEX}"))

    dkey = "andy.domains.discovery"
    rule = {"hostid": tid, "name": "ANDY210–217 interface discovery", "key_": dkey,
            "type": 18, "master_itemid": ids["andy.discovery.raw"], "delay": "0",
            "lifetime_type": 0, "lifetime": "1h", "enabled_lifetime_type": 0,
            "enabled_lifetime": "0", "preprocessing": js(PARSER + (ROOT / "discovery.js").read_text())}
    # An empty simple-check target would ping the monitoring host instead.
    rule["filter"] = {"evaltype": 0, "conditions": [{"macro": "{#ENDPOINT}",
        "value": r"^([0-9]{1,3}\.){3}[0-9]{1,3}$", "operator": 8}]}
    rows = api.call("discoveryrule.get", {"hostids": tid, "output": ["itemid"], "filter": {"key_": dkey}})
    if rows:
        rule["itemid"] = rows[0]["itemid"]
        rule.pop("hostid")
        api.call("discoveryrule.update", rule)
        did = rule["itemid"]
    else:
        did = api.call("discoveryrule.create", rule)["itemids"][0]

    def prototype(key, label, typ=18, vt=3, master="andy.metrics.raw", pre=None,
                  discovery_id=None, prefix="VLAN {#VLAN} / ", **extra):
        discovery_id = discovery_id or did
        spec = {"hostid": tid, "ruleid": discovery_id, "key_": key, "name": prefix + label,
                "type": typ, "value_type": vt, "delay": "0", "history": "7d",
                "trends": "0" if vt in (1, 2, 4) else "30d",
                "tags": [{"tag": "component", "value": "network-osi"}]}
        if prefix.startswith("VLAN"):
            spec["tags"].append({"tag": "vlan", "value": "{#VLAN}"})
        if master:
            spec["master_itemid"] = ids[master]
        if pre:
            spec["preprocessing"] = pre
        spec.update(extra)
        old = api.call("itemprototype.get", {"discoveryids": discovery_id, "output": ["itemid"], "filter": {"key_": key}})
        if old:
            spec["itemid"] = old[0]["itemid"]
            spec.pop("hostid")
            spec.pop("ruleid")
            api.call("itemprototype.update", spec)
            ids[key] = spec["itemid"]
        else:
            ids[key] = api.call("itemprototype.create", spec)["itemids"][0]

    for metric, (oid, vt, units) in metrics.items():
        pre = extract(oid + ".{#SNMPINDEX}")
        if metric in ("rx", "tx", "rx_errors", "tx_errors", "rx_discards", "tx_discards"):
            pre += [{"type": 10, "params": "", "error_handler": 0}]
        prototype("andy.domain." + metric + "[{#VLAN}]", "OPER raw" if metric == "oper" else metric,
                  vt=vt, pre=pre, units=units)
    prototype("andy.domain.interface[{#VLAN}]", "INTERFACE", vt=4, pre=extract(IFX + "1.{#SNMPINDEX}"))
    for key, oid in [("description", IF + "2"), ("alias", IFX + "18")]:
        prototype("andy.domain." + key + "[{#VLAN}]", key, vt=4,
                  master="andy.discovery.raw", pre=extract(oid + ".{#SNMPINDEX}", "UNKNOWN"))
    prototype("andy.domain.actual_vlan[{#VLAN}]", "VLAN ID", master="andy.topology.evidence",
              pre=js("var v=JSON.parse(value).vlans.filter(function(v){return v.name==='{#IFNAME}';}); if(v.length!==1)throw 'Missing VLAN'; return v[0]['vlan-id'];"))
    prototype("andy.domain.parent[{#VLAN}]", "PARENT", vt=4, master="andy.topology.evidence",
              pre=js("var v=JSON.parse(value).vlans.filter(function(v){return v.name==='{#IFNAME}';}); return v.length===1 ? v[0].interface : 'UNKNOWN';"))
    for key, macro, vt, label in [
        ("index", "{#SNMPINDEX}", 3, "ifIndex"), ("vlan", "{#VLAN}", 3, "VLAN"),
        ("network", "{#NETWORK}", 4, "NETWORK"), ("gateway", "{#GATEWAY}", 4, "GATEWAY"),
        ("endpoint", "{#ENDPOINT}", 4, "ENDPOINT"),
    ]:
        prototype("andy.domain." + key + "[{#VLAN}]", label, vt=vt, master="andy.discovery.raw",
                  pre=js("return " + json.dumps(macro) + ";"))
    prototype("andy.domain.present[{#VLAN}]", "PRESENT", pre=js(PARSER +
        "return snmp(value)['" + IFX + "1.{#SNMPINDEX}'] === '{#IFNAME}' ? 1 : 0;"))
    # RouterOS lacks ipNetToPhysicalState. Legacy empty MAC is INCOMPLETE;
    # a missing row in an exported table is ABSENT; unavailable/malformed data is UNKNOWN.
    neighbor = PARSER + (ROOT / "neighbor.js").read_text() + "var n=neighbor(snmp(value),'{#SNMPINDEX}','{#ENDPOINT}');"
    prototype("andy.domain.arp[{#VLAN}]", "ARP raw", master="andy.neighbors.raw", pre=js(neighbor + "return n.state;"))
    prototype("andy.domain.mac[{#VLAN}]", "MAC", vt=4, master="andy.neighbors.raw", pre=js(neighbor + "return n.mac;"))
    for key, native, label, vt, unit in [
        ("icmp", "icmpping", "ICMP raw", 3, ""),
        ("loss", "icmppingloss", "LOSS", 0, "%"),
        ("rtt", "icmppingsec", "RTT", 0, "s"),
    ]:
        prototype(native + "[{#ENDPOINT},3,100,56,300]", label, 3, vt, None,
                  delay="10s", units=unit, description="Native ICMP from the ROC Zabbix server; 3 probes per cycle.")

    # Local calculations ensure stale successful measurements cannot render UP.
    for key, label, raw, ttl, extra in [
        ("oper_state", "OPER", "andy.domain.oper[{#VLAN}]", "90s", ""),
        ("arp_state", "ARP/NEIGHBOR", "andy.domain.arp[{#VLAN}]", "90s", ""),
        ("icmp_state", "ICMP", "icmpping[{#ENDPOINT},3,100,56,300]", "40s", "+1"),
    ]:
        prototype("andy.domain." + key + "[{#VLAN}]", label, 15, 3, None,
                  delay="10s", params="(nodata(//" + raw + "," + ttl + ")=0)*(last(//" + raw + ")" + extra + ")")

    # The real VLAN parent can have several Ethernet members. Monitor them through
    # the same master; do not assert that the declared trunk is the effective parent.
    physical_rule = {"hostid": tid, "name": "Ethernet physical interfaces", "key_": "andy.physical.discovery",
        "type": 18, "master_itemid": ids["andy.discovery.raw"], "delay": "0",
        "lifetime_type": 0, "lifetime": "1h", "enabled_lifetime_type": 0, "enabled_lifetime": "0",
        "preprocessing": js(PARSER + "var o=snmp(value), rows=[]; Object.keys(o).forEach(function(k){"
            "var m=k.match(/^1\\.3\\.6\\.1\\.2\\.1\\.2\\.2\\.1\\.3\\.([0-9]+)$/);"
            "if(m && o[k]==='6'){var name=o['" + IFX + "1.'+m[1]]; if(name)rows.push({'{#SNMPINDEX}':m[1],'{#IFNAME}':name});}}); return JSON.stringify(rows);")}
    old = api.call("discoveryrule.get", {"hostids": tid, "output": ["itemid"], "filter": {"key_": physical_rule["key_"]}})
    if old:
        pid = old[0]["itemid"]
        physical_rule.pop("hostid")
        api.call("discoveryrule.update", dict(physical_rule, itemid=pid))
    else:
        pid = api.call("discoveryrule.create", physical_rule)["itemids"][0]
    for metric, (oid, vt, units) in metrics.items():
        pre = extract(oid + ".{#SNMPINDEX}")
        if metric not in ("admin", "oper", "speed"):
            pre += [{"type": 10, "params": "", "error_handler": 0}]
        prototype("andy.physical." + metric + "[{#SNMPINDEX}]", metric.upper(), vt=vt,
                  pre=pre, units=units, discovery_id=pid, prefix="PHYSICAL {#SNMPINDEX} / ")
    prototype("andy.physical.name[{#SNMPINDEX}]", "INTERFACE", vt=4,
              pre=extract(IFX + "1.{#SNMPINDEX}"), discovery_id=pid, prefix="PHYSICAL {#SNMPINDEX} / ")
    prototype("andy.physical.role[{#SNMPINDEX}]", "ROLE", vt=4, master="andy.topology.evidence",
              pre=js("var s=JSON.parse(value), name='{#IFNAME}', parents=s.vlans.map(function(v){return v.interface;});"
                     "var member=s.ports.some(function(p){return p.interface===name && parents.indexOf(p.bridge)>=0;});"
                     "return member ? 'VLAN parent member' : (s.physical.name===name ? 'Declared trunk' : 'Other / not asserted');"),
              discovery_id=pid, prefix="PHYSICAL {#SNMPINDEX} / ")

    def calc(key, label, expression):
        item(key, label, 15, params=expression, delay="10s")

    calc("andy.summary.domains", "NETWORK: domains", "sum(last_foreach(//andy.domain.present[*]))")
    calc("andy.summary.complete", "NETWORK: complete", 'count(last_foreach(//andy.domain.arp[*]),"eq",1)')
    calc("andy.summary.incomplete", "NETWORK: incomplete", 'count(last_foreach(//andy.domain.arp[*]),"eq",2)')
    calc("andy.summary.reachable", "NETWORK: reachable", 'count(last_foreach(//icmpping[*,3,100,56,300]),"eq",1)')
    calc("andy.health.l1", "NETWORK HEALTH: L1 PHYSICAL",
         '(nodata(//andy.parent.oper,90s)=0 and nodata(//andy.parent.admin,90s)=0 and nodata(//andy.topology.evidence,1h)=0 and last(//andy.topology.consistent)=1)*(1+2*(last(//andy.parent.oper)<>1 or last(//andy.parent.admin)<>1)+'
         '(last(//andy.parent.oper)=1 and last(//andy.parent.admin)=1 and '
         '(last(//andy.parent.rx_errors)>0 or last(//andy.parent.tx_errors)>0 or last(//andy.parent.rx_discards)>0 or last(//andy.parent.tx_discards)>0)))')
    calc("andy.health.l2", "NETWORK HEALTH: L2 DATA LINK",
         '(count(count_foreach(//andy.domain.oper[*],90s),"gt",0)=8 and count(count_foreach(//andy.domain.arp[*],90s),"gt",0)=8 and count(last_foreach(//andy.domain.arp[*]),"eq",0)=0 and nodata(//andy.topology.evidence,1h)=0)*'
         '(1+(last(//andy.summary.domains)<>8 or last(//andy.topology.consistent)=0 or last(//andy.summary.complete)<>8 or count(last_foreach(//andy.domain.oper[*]),"eq",1)<>8)+'
         '(last(//andy.summary.domains)<>8 or count(last_foreach(//andy.domain.oper[*]),"eq",1)<>8))')
    calc("andy.health.l3", "NETWORK HEALTH: L3 NETWORK",
         '(count(count_foreach(//andy.domain.arp[*],90s),"gt",0)=8 and count(count_foreach(//icmpping[*,3,100,56,300],40s),"gt",0)=8)*'
         '(1+(last(//andy.summary.reachable)<>8 or last(//andy.summary.complete)<>8)+(last(//andy.summary.reachable)=0))')

    def trigger(name, expression, dependencies=(), prototype=False, priority=3):
        kind = "triggerprototype" if prototype else "trigger"
        body = {"description": name, "expression": expression, "priority": priority,
                "dependencies": [{"triggerid": x} for x in dependencies],
                "tags": [{"tag": "component", "value": "network-osi"}]}
        old = api.call(kind + ".get", {"hostids": tid, "output": ["triggerid"], "filter": {"description": name}})
        if old:
            body["triggerid"] = old[0]["triggerid"]
            api.call(kind + ".update", body)
            return body["triggerid"]
        return api.call(kind + ".create", body)["triggerids"][0]

    path = "/" + TEMPLATE + "/"
    stale = trigger("NETWORK_TELEMETRY_UNKNOWN", f"nodata({path}andy.parent.oper,90s)=1 or nodata({path}andy.topology.evidence,1h)=1", priority=2)
    parent = trigger("L1_PARENT_DOWN", f"min({path}andy.parent.oper,#3)<>1", [stale], priority=4)
    parent_up = f"max({path}andy.parent.oper,#3)=1 and nodata({path}andy.metrics.raw,90s)=0"
    topology = trigger("L2_TOPOLOGY_MISMATCH", f"last({path}andy.topology.consistent)=0 and {parent_up}", [stale, parent], priority=3)
    verified_parent = parent_up + f" and last({path}andy.topology.consistent)=1"
    domain = trigger("L2_DOMAIN_DOWN VLAN {#VLAN}", f"(min({path}andy.domain.oper[{{#VLAN}}],#3)<>1 or max({path}andy.domain.present[{{#VLAN}}],#3)=0) and {verified_parent}", [stale, parent, topology], True)
    neighbor_trigger = trigger("L3_NEIGHBOR_INCOMPLETE VLAN {#VLAN}", f"min({path}andy.domain.arp[{{#VLAN}}],#3)>1 and {verified_parent}", [stale, parent, topology, domain], True)
    trigger("L3_ENDPOINT_UNREACHABLE VLAN {#VLAN}", f"max({path}icmpping[{{#ENDPOINT}},3,100,56,300],#3)=0 and {verified_parent}",
            [stale, parent, topology, domain, neighbor_trigger], True)

    # Host secret macros keep SNMP passphrases out of interface/API display and exports.
    secret = settings["snmp"]
    macros = [
        {"macro": "{$ANDY.SNMP.USER}", "value": secret["security_name"], "type": 1},
        {"macro": "{$ANDY.SNMP.AUTH}", "value": secret["auth_password"], "type": 1},
        {"macro": "{$ANDY.SNMP.PRIV}", "value": secret["privacy_password"], "type": 1},
        {"macro": "{$ANDY.PARENT.INDEX}", "value": str(settings["parent_index"])},
    ]
    host = settings["host_name"]
    interface = {"type": 2, "main": 1, "useip": 1, "ip": secret["target"], "dns": "", "port": "161",
        "details": {"version": 3, "securitylevel": 2, "securityname": "{$ANDY.SNMP.USER}",
                    "authpassphrase": "{$ANDY.SNMP.AUTH}", "privpassphrase": "{$ANDY.SNMP.PRIV}",
                    "authprotocol": settings.get("auth_protocol", 1), "privprotocol": 1,
                    "max_repetitions": 30}}
    hosts = api.call("host.get", {"output": ["hostid"], "filter": {"host": host}})
    if hosts:
        hid = hosts[0]["hostid"]
        api.call("host.update", {"hostid": hid, "macros": macros, "templates": [{"templateid": tid}]})
        old = api.call("hostinterface.get", {"hostids": [hid], "output": ["interfaceid"], "filter": {"type": 2, "main": 1}})
        if old:
            api.call("hostinterface.update", dict(interface, interfaceid=old[0]["interfaceid"]))
        else:
            api.call("hostinterface.create", dict(interface, hostid=hid))
    else:
        hid = api.call("host.create", {"host": host, "groups": [{"groupid": group("hostgroup")}],
            "templates": [{"templateid": tid}], "macros": macros,
            "interfaces": [interface]})["hostids"][0]
    return {"templateid": tid, "hostid": hid}


def managed_export(raw):
    """Exclude superseded partial definitions and private template macros."""
    result = json.loads(raw)
    template = result["zabbix_export"]["templates"][0]
    template.pop("macros", None)
    template["items"] = [i for i in template.get("items", [])
                         if i["key"].startswith("andy.") and not i["key"].startswith("andy.net.")]
    template["discovery_rules"] = [d for d in template.get("discovery_rules", [])
                                   if d["key"] in ("andy.domains.discovery", "andy.physical.discovery")]
    for rule in template["discovery_rules"]:
        rule["item_prototypes"] = [i for i in rule.get("item_prototypes", [])
                                   if i["key"] != "andy.domain.raw[{#VLAN}]"]
        rule["trigger_prototypes"] = [t for t in rule.get("trigger_prototypes", [])
                                      if " VLAN {#VLAN}" in t["name"]]
    root_names = {"NETWORK_TELEMETRY_UNKNOWN", "L1_PARENT_DOWN", "L2_TOPOLOGY_MISMATCH"}
    exported = result["zabbix_export"]
    exported["triggers"] = [t for t in exported.get("triggers", []) if t["name"] in root_names]
    return json.dumps(result, indent=2) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", required=True)
    parser.add_argument("--export-template")
    args = parser.parse_args()
    settings = private_json(args.settings)
    api = Api(settings)
    try:
        result = provision(api, settings)
        if args.export_template:
            export = api.call("configuration.export", {"format": "json", "options": {"templates": [result["templateid"]]}})
            Path(args.export_template).write_text(managed_export(export))
        print(json.dumps(result))
    finally:
        if not settings.get("token_file"):
            api.call("user.logout", {})


if __name__ == "__main__":
    main()
