#!/usr/bin/env python3
"""Send a read-only RouterOS topology snapshot to the ROC Zabbix trapper.

Run separately from Andy Ops and browsers. Settings are private, outside Git.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess

from provision import private_json


def evidence(vlans, ports, physical, parent):
    domains = [v for v in vlans if re.fullmatch(r"ANDY21[0-7]-GW", v.get("name", ""))]
    parents = sorted({v["interface"] for v in domains})
    trunk = [p for p in ports if p.get("interface") == parent]
    trunk_bridge = trunk[0].get("bridge") if len(trunk) == 1 else None
    valid = len(domains) == 8 and all(
        v.get("vlan-id") == int(v["name"][4:7]) and not v.get("disabled") for v in domains)
    consistent = valid and len(parents) == 1 and parents[0] == trunk_bridge
    members = sorted(p["interface"] for p in ports if p.get("bridge") in parents)
    note = ("VLAN parent: " + (", ".join(parents) or "UNKNOWN") +
            "; members: " + (", ".join(members) or "UNKNOWN") +
            ". Declared trunk: " + parent + " in " + (trunk_bridge or "UNKNOWN") +
            " (" + physical.get("status", "UNKNOWN") + "). " +
            ("Parent association verified." if consistent else
             "TOPOLOGY MISMATCH: physical association is unproven."))
    return {"observed_at": datetime.now(timezone.utc).isoformat(),
            "consistent": consistent, "note": note, "vlans": domains,
            "ports": ports, "physical": physical}


def collect(settings):
    alias, parent = settings["ssh_alias"], settings["parent_name"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", alias) or alias.startswith("-"):
        raise ValueError("Invalid SSH alias")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", parent):
        raise ValueError("Invalid interface name")

    def read(command):
        raw = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                              "-o", "ControlMaster=no", "-o", "ControlPath=none",
                              "-o", "StrictHostKeyChecking=yes",
                              alias, command], check=True, capture_output=True,
                             text=True, timeout=20).stdout
        return json.loads(raw)

    vlans = read(":put [:serialize to=json value=[/interface/vlan/print as-value]]")
    ports = read(":put [:serialize to=json value=[/interface/bridge/port/print as-value]]")
    physical = read(":put [:serialize to=json value=[/interface/ethernet/monitor " + parent + " once as-value]]")
    return evidence(vlans, ports, physical, parent)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", required=True)
    args = parser.parse_args()
    settings = private_json(args.settings)
    value = collect(settings)
    payload = json.dumps(settings["host_name"]) + " andy.topology.evidence " + json.dumps(value, separators=(",", ":")) + "\n"
    # Explicit ROC destination; never fall back to the host's legacy Zabbix.
    result = subprocess.run(["zabbix_sender", "-z", settings["sender_address"],
                             "-p", str(settings.get("sender_port", 10051)), "-i", "-"],
                            input=payload, capture_output=True, text=True, timeout=15)
    if result.returncode or "failed: 0" not in result.stdout:
        raise RuntimeError("ROC topology snapshot was not accepted")
    if settings.get("evidence_file"):
        dest = Path(settings["evidence_file"])
        dest.write_text(json.dumps(value, indent=2) + "\n")
        dest.chmod(0o600)
    print("ROC topology snapshot accepted")


if __name__ == "__main__":
    main()
