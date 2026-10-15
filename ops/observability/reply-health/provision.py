#!/usr/bin/env python3
"""Install only readonly derived Zabbix indicators for Andy reply health.

Existing action 7 already sends ops_alert-tagged incidents via Telegram,
including recovery. Do not modify its destination or bot credentials.
"""
import sys

sys.path.insert(0, "/usr/local/lib/andy-ops-alerts")
from provision import client, ensure_item, ensure_trigger

HOST = "Andy Engine - AGT01"
HOSTID = "10785"
KEYS = (
    ("andy.reply.probe_ok", "Andy reply: audit query available"),
    ("andy.reply.functional_stalled", "Andy reply: functional response incident"),
    ("andy.reply.direct_6h", "Andy reply: direct interactions in 6 hours"),
    ("andy.reply.no_safe_6h", "Andy reply: behavior unavailable in 6 hours"),
    ("andy.reply.blueprint_6h", "Andy reply: blueprint blocked in 6 hours"),
    ("andy.reply.other_failed_6h", "Andy reply: other failures in 6 hours"),
    ("andy.reply.latest_failure_epoch", "Andy reply: most recent failure epoch"),
    ("andy.reply.last_agent_done_epoch", "Andy reply: last automatic outbox DONE epoch"),
)


def main():
    api = client()
    hosts = api.call(
        "host.get",
        {"hostids": [HOSTID], "output": ["hostid", "name", "status"]},
    )
    if len(hosts) != 1 or hosts[0]["name"] != HOST or hosts[0]["status"] != "0":
        raise RuntimeError("UNEXPECTED_ZABBIX_TARGET_HOST")
    for key, title in KEYS:
        ensure_item(api, HOSTID, key, title, 3)
    triggerid = ensure_trigger(
        api, HOSTID,
        "ANDY_WA_AUTO_REPLY_STALLED: real direct messages unanswered",
        f"last(/{HOST}/andy.reply.functional_stalled)=1",
        4, "andy_reply",
    )
    # All diagnostic dimensions are trapper items visible in Zabbix.
    # Avoid dynamic function macros in operational data during bootstrap;
    # the Zabbix API rejected them for this trigger.
    print("ROOT_INCIDENT_TRIGGER", triggerid)
    watchdog = ensure_trigger(
        api, HOSTID, "ANDY_WA_REPLY_OBSERVABILITY_LOST: read-only probe failed",
        f"last(/{HOST}/andy.reply.probe_ok)=0 or "
        f"nodata(/{HOST}/andy.reply.probe_ok,15m)=1",
        3, "andy_reply_monitor",
    )
    print("ZABBIX_REPLY_HEALTH_PROVISIONED",len(KEYS),
          "ROOT_TRIGGER",triggerid,"PROBE_TRIGGER",watchdog)
    print("ACTION7_SCOPE_UNCHANGED")


if __name__ == "__main__":
    main()
