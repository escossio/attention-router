#!/usr/bin/env python3
"""Bounded, read-only host CPU and Tempo signals for Zabbix ROC.

Runs from AGT01; never restarts services, writes logs, or sends user messages.
All metric items are Zabbix trapper items provisioned by provision.py.
"""
import json
import re
import subprocess
import time
from pathlib import Path

NODES = {
    "agt01": ("Andy Engine - AGT01", None),
    "ci01": ("Andy CPU - CI01", "ci01"),
    "ci02": ("Andy CPU - CI02", "ci02"),
    "ci03": ("Andy CPU - CI03", "ci03"),
}
INTERVAL = 2
MAX_REMOTE_SECONDS = 12
PROC_LINE = re.compile(r"^cpu(?P<core>\d+)\s+(?P<values>(?:\d+\s*)+)$")


def decode_cpu(text):
    result = {}
    for line in text.splitlines():
        match = PROC_LINE.fullmatch(line.strip())
        if match:
            values = [int(n) for n in match.group("values").split()]
            result[int(match.group("core"))] = (sum(values), values[3] + values[4])
    return result


def percent(first, second):
    percentages = {}
    for core, (total, idle) in first.items():
        if core not in second:
            continue
        end_total, end_idle = second[core]
        delta_total = end_total - total
        delta_idle = end_idle - idle
        if delta_total > 0 and 0 <= delta_idle <= delta_total:
            percentages[core] = round(100 * (1 - delta_idle / delta_total), 3)
    return percentages


def cpu_sample(alias=None):
    if alias:
        cmd = [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
            "-o", "StrictHostKeyChecking=yes", "-o", "ControlMaster=no",
            "-o", "ControlPath=none", alias,
            "cat /proc/stat; echo __CPU_SECOND__; sleep 2; cat /proc/stat",
        ]
        output = subprocess.run(cmd, text=True, capture_output=True,
                                timeout=MAX_REMOTE_SECONDS, check=True).stdout
        start, finish = output.split("__CPU_SECOND__", 1)
        first, second = decode_cpu(start), decode_cpu(finish)
    else:
        first = decode_cpu(Path("/proc/stat").read_text())
        time.sleep(INTERVAL)
        second = decode_cpu(Path("/proc/stat").read_text())
    values = percent(first, second)
    if not values or len(values) != len(first):
        raise RuntimeError("CPU_SAMPLE_INCOMPLETE")
    return values


def cpu_metrics(data):
    peak_core = max(data, key=data.get)
    peak = data[peak_core]
    average = sum(data.values()) / len(data)
    return {
        "andy.cpu.max_pct": round(peak, 3),
        "andy.cpu.mean_pct": round(average, 3),
        "andy.cpu.hot_id": peak_core,
        "andy.cpu.hot_idle": int(peak >= 90 and average <= 40),
        "andy.cpu.probe_ok": 1,
        **{f"andy.cpu.core_pct[{core}]": value for core, value in data.items()},
    }


def tempo_metrics():
    try:
        logs = subprocess.run(
            ["docker", "logs", "--since", "75s", "andy-roc-tempo"],
            text=True, capture_output=True, timeout=7, check=True,
        )
        count = sum("no jobs found" in line and "backendworker.go:529" in line
                    for line in (logs.stdout + logs.stderr).splitlines())
        log_ok = 1
    except (OSError, subprocess.SubprocessError):
        count, log_ok = 0, 0
    code = (
        "import urllib.request;"
        "r=urllib.request.urlopen('http://roc-tempo:3200/ready',timeout=3);"
        "assert r.status==200;"
        "r=urllib.request.urlopen('http://roc-tempo:3200/api/search?limit=1',timeout=4);"
        "assert r.status==200"
    )
    try:
        subprocess.run(
            ["docker", "exec", "andy-roc-trace-bridge", "python", "-c", code],
            capture_output=True, check=True, timeout=10,
        )
        query_ok = 1
    except (OSError, subprocess.SubprocessError):
        query_ok = 0
    return {
        "andy.tempo.scheduler_empty.count": count,
        "andy.tempo.log_probe_ok": log_ok,
        "andy.tempo.query_ok": query_ok,
    }


def sender_lines(metrics):
    for host, entries in metrics.items():
        for key, value in entries.items():
            yield f"{json.dumps(host)} {key} {value}"


def publish(metrics):
    payload = "\n".join(sender_lines(metrics)) + "\n"
    result = subprocess.run(
        ["docker", "exec", "-i", "andy-roc-agent2", "zabbix_sender",
         "-z", "roc-zabbix-server", "-p", "10051", "-i", "-"],
        input=payload, capture_output=True, text=True, timeout=15,
    )
    combined = (result.stdout + " " + result.stderr)
    if result.returncode != 0 or "failed: 0" not in combined:
        raise RuntimeError("ZABBIX_SENDER_UNCONFIRMED")
    return combined


def main():
    metrics = {}
    for node, (host, alias) in NODES.items():
        try:
            data = cpu_sample(alias)
            metrics[host] = cpu_metrics(data)
            print(f"CPU {node}: cores={len(data)} max={max(data.values()):.1f}% "
                  f"mean={sum(data.values())/len(data):.1f}%")
        except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as error:
            metrics[host] = {"andy.cpu.probe_ok": 0}
            print(f"CPU {node}: PROBE_FAILED {type(error).__name__}")
    metrics[NODES["agt01"][0]].update(tempo_metrics())
    print("TEMPO scheduler_empty_count=",
          metrics[NODES["agt01"][0]]["andy.tempo.scheduler_empty.count"],
          "query_ok=", metrics[NODES["agt01"][0]]["andy.tempo.query_ok"])
    reply = publish(metrics)
    print("ZABBIX_SENDER_ACK", reply.strip()[-160:])


if __name__ == "__main__":
    main()
