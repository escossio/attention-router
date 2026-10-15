#!/usr/bin/env python3
"""Read-only WhatsApp automatic-reply health -> Zabbix; fail-closed recovery.

Only outbound rows of type agent_execution_text/voice with status DONE and
a direct inbound interaction qualify as positive recovery evidence.
No messages, private actor IDs, payloads or credentials are exported.
"""
import json
import os
from pathlib import Path
import subprocess
import time

STATE = Path("/midia/agt01-storage/attention-router/private/andy-reply-health-state.json")
HOST = "Andy Engine - AGT01"
LOOKBACK = "6 hours"

SQL = r"""
BEGIN READ ONLY;
WITH direct AS (
    SELECT DISTINCT interaction_id, received_at
    FROM inbound_events
    WHERE source='wwebjs' AND received_at >= now() - interval '6 hours'
      AND (payload->>'actor_id' LIKE '%@lid'
           OR payload->>'actor_id' LIKE '%@c.us')
), failure AS (
    SELECT a.event_type, a.created_at, a.interaction_id
    FROM audit_events a
    JOIN direct d ON d.interaction_id=a.interaction_id
    WHERE a.created_at >= now() - interval '6 hours'
      AND (a.event_type IN ('behavior.unavailable','conversation.blocked',
                           'execution.dispatch_failed','outbox.failed',
                           'decision.error','execution.error')
           OR a.event_type LIKE '%.failed')
), success AS (
    SELECT max(o.completed_at) AS last_delivery
    FROM outbox_messages o
    JOIN inbound_events i ON i.interaction_id=o.interaction_id
    WHERE o.status='DONE' AND o.completed_at IS NOT NULL
      AND o.destination IN ('local_transport','wwebjs')
      AND o.action_type IN ('agent_execution_text','agent_execution_voice')
      AND i.source='wwebjs'
      AND (i.payload->>'actor_id' LIKE '%@lid'
           OR i.payload->>'actor_id' LIKE '%@c.us')
)
SELECT
    (SELECT count(distinct interaction_id) FROM direct),
    (SELECT count(distinct interaction_id) FROM failure WHERE event_type='behavior.unavailable'),
    (SELECT count(distinct interaction_id) FROM failure WHERE event_type='conversation.blocked'),
    (SELECT count(distinct interaction_id) FROM failure WHERE event_type NOT IN
        ('behavior.unavailable','conversation.blocked')),
    coalesce((SELECT extract(epoch FROM max(created_at))::bigint FROM failure),0),
    coalesce((SELECT extract(epoch FROM last_delivery)::bigint FROM success),0),
    coalesce((SELECT extract(epoch FROM max(received_at))::bigint FROM direct),0);
COMMIT;
"""


def probe():
    command = [
        "docker", "exec", "-i", "attention-router-live-flow-stage4-db-1",
        "sh", "-c",
        'exec psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
    ]
    result = subprocess.run(
        command, input=SQL, capture_output=True, text=True, timeout=25,
        check=True,
    )
    rows = [line for line in result.stdout.splitlines() if line]
    if len(rows) != 1:
        raise RuntimeError("UNEXPECTED_READONLY_SQL_ROWS")
    values = [int(value) for value in rows[0].split("|")]
    if len(values) != 7 or any(v < 0 for v in values):
        raise RuntimeError("UNEXPECTED_READONLY_SQL_VALUES")
    return dict(zip(
        ("direct_6h", "no_safe_6h", "blueprint_6h", "other_failed_6h",
         "latest_failure_ts", "last_agent_delivery_ts", "latest_direct_ts"), values
    ))


def next_state(previous, evidence, now):
    active = bool(previous.get("active", False))
    opened = int(previous.get("opened_at", 0))
    failures = sum(evidence[k] for k in ("no_safe_6h", "blueprint_6h", "other_failed_6h"))
    delivery = int(evidence["last_agent_delivery_ts"])
    latest_error = int(evidence["latest_failure_ts"])
    if active:
        # An old success, manual owner reply, or passage of time NEVER heals it.
        if delivery > max(opened, latest_error):
            active = False
            opened = 0
    elif (evidence["direct_6h"] >= 3 and failures >= 2
          and latest_error > 0 and delivery < latest_error):
        active = True
        opened = now
    return {"active": active, "opened_at": opened}


def sender_lines(metrics):
    return "".join(f'{json.dumps(HOST)} {key} {value}\n'
                   for key, value in metrics.items())


def send(metrics):
    result = subprocess.run(
        ["docker", "exec", "-i", "andy-roc-agent2", "zabbix_sender",
         "-z", "roc-zabbix-server", "-p", "10051", "-i", "-"],
        input=sender_lines(metrics), text=True,
        capture_output=True, timeout=15,
    )
    if result.returncode or "failed: 0" not in (result.stdout + result.stderr):
        raise RuntimeError("ZABBIX_SENDER_NOT_ACKNOWLEDGED")
    return result.stdout.strip()[-145:]


def load_state():
    if not STATE.exists():
        return {"active": False, "opened_at": 0}
    if (STATE.stat().st_mode & 0o077) != 0:
        raise RuntimeError("STATE_FILE_NOT_PRIVATE")
    obj = json.loads(STATE.read_text())
    if type(obj.get("active")) is not bool or type(obj.get("opened_at")) is not int:
        raise RuntimeError("STATE_FILE_INVALID")
    return obj


def save_state(state):
    STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = STATE.with_name(STATE.name + ".staged." + str(os.getpid()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, STATE)
    finally:
        if tmp.exists():
            tmp.unlink()


def main():
    os.umask(0o077)
    previous = load_state()
    now = int(time.time())
    try:
        evidence = probe()
        current = next_state(previous, evidence, now)
    except Exception as error:
        # No state change, no false green, no secret or message content.
        send({"andy.reply.probe_ok": 0})
        print("REPLY_PROBE_FAILED", type(error).__name__)
        return
    metrics = {
        "andy.reply.probe_ok": 1,
        "andy.reply.functional_stalled": int(current["active"]),
        "andy.reply.direct_6h": evidence["direct_6h"],
        "andy.reply.no_safe_6h": evidence["no_safe_6h"],
        "andy.reply.blueprint_6h": evidence["blueprint_6h"],
        "andy.reply.other_failed_6h": evidence["other_failed_6h"],
        "andy.reply.latest_failure_epoch": evidence["latest_failure_ts"],
        "andy.reply.last_agent_done_epoch": evidence["last_agent_delivery_ts"],
    }
    reply = send(metrics)
    save_state(current)
    print("REPLY_HEALTH", "INCIDENT" if current["active"] else "NO_ACTIVE_INCIDENT",
          "DIRECT_6H", evidence["direct_6h"],
          "NO_SAFE_6H", evidence["no_safe_6h"],
          "BLUEPRINT_6H", evidence["blueprint_6h"],
          "OTHER_FAILED_6H", evidence["other_failed_6h"],
          "LAST_AGENT_DONE_EPOCH", evidence["last_agent_delivery_ts"])
    print("SENDER_ACK", reply)


if __name__ == "__main__":
    main()
