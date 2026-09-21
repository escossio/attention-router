#!/usr/bin/env python3
"""LAN-only live compute supervisor for AGT and distributed CI workers."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parent
LOG_ROOT = Path(os.environ.get("ANDY_OPS_CI_LOG_ROOT", "/var/log/andy-ci"))
TOOL_HISTORY = Path(os.environ.get("ANDY_OPS_TOOL_HISTORY", str(Path.home() / ".claude-server-commander" / "tool-history.jsonl")))
LISTEN_ADDRESS = os.environ.get("ANDY_OPS_LISTEN_ADDRESS", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("ANDY_OPS_LISTEN_PORT", "18121"))
POLL_SECONDS = float(os.environ.get("ANDY_OPS_POLL_SECONDS", "1.5"))
SSH_TIMEOUT = float(os.environ.get("ANDY_OPS_SSH_TIMEOUT", "1.2"))

NODES = {
    "agt": {"label": "AGT", "role": "CONTROL PLANE", "host": None},
    "ci01": {"label": "CI01", "role": "WORKER", "host": os.environ.get("ANDY_OPS_CI01_HOST", "ci01")},
    "ci02": {"label": "CI02", "role": "WORKER", "host": os.environ.get("ANDY_OPS_CI02_HOST", "ci02")},
    "ci03": {"label": "CI03", "role": "KVM Virtualized Worker", "host": os.environ.get("ANDY_OPS_CI03_HOST", "ci03")},
}

REMOTE_SNAPSHOT = r"""
printf '__PROC__\n'
grep -E '^cpu[0-9]* ' /proc/stat
printf '__TEMP__\n'
for f in /sys/class/hwmon/hwmon*/temp*_input; do
  [ -r "$f" ] || continue
  d="$(dirname "$f")"
  name="$(cat "$d/name" 2>/dev/null || basename "$d")"
  value="$(cat "$f" 2>/dev/null || true)"
  metric="$(basename "$f")"
  label="$(cat "${f%_input}_label" 2>/dev/null || true)"
  printf '%s|%s|%s|%s\n' "$name" "$metric" "$label" "$value"
done
printf '__TASK__\n'
ps -eo pid=,etimes=,args= | grep -E 'andy-ci-(run|distributed|reprofile)|python -m pytest -m postgres' | grep -v -E 'grep -E|andy-ops-panel' | head -20 || true
"""

_state_lock = threading.Lock()
_state: dict[str, Any] = {"generated_at": None, "nodes": {}, "dispatch": None, "recent": []}
_cpu_prev: dict[str, dict[str, tuple[int, int]]] = {}


def _run_snapshot(host: str | None) -> str:
    if host is None:
        cmd = ["bash", "-lc", REMOTE_SNAPSHOT]
    else:
        cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={max(1, int(SSH_TIMEOUT))}",
            host,
            REMOTE_SNAPSHOT,
        ]
    result = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=max(2.0, SSH_TIMEOUT + 0.8),
    )
    if result.returncode != 0:
        raise RuntimeError("snapshot unavailable")
    return result.stdout


def _sections(raw: str) -> dict[str, list[str]]:
    sections = {"PROC": [], "TEMP": [], "TASK": []}
    current: str | None = None
    for line in raw.splitlines():
        if line == "__PROC__":
            current = "PROC"
            continue
        if line == "__TEMP__":
            current = "TEMP"
            continue
        if line == "__TASK__":
            current = "TASK"
            continue
        if current:
            sections[current].append(line)
    return sections


def _parse_cpu(lines: list[str]) -> dict[str, tuple[int, int]]:
    parsed: dict[str, tuple[int, int]] = {}
    for line in lines:
        fields = line.split()
        if not fields or not fields[0].startswith("cpu"):
            continue
        try:
            values = [int(v) for v in fields[1:9]]
        except ValueError:
            continue
        while len(values) < 8:
            values.append(0)
        total = sum(values)
        idle = values[3] + values[4]
        parsed[fields[0]] = (total, idle)
    return parsed


def _usage(node_id: str, current: dict[str, tuple[int, int]]) -> tuple[float | None, list[float]]:
    previous = _cpu_prev.get(node_id)
    _cpu_prev[node_id] = current
    if not previous:
        return None, []

    values: dict[str, float] = {}
    for key, (total, idle) in current.items():
        old = previous.get(key)
        if not old:
            continue
        dt = total - old[0]
        di = idle - old[1]
        if dt <= 0:
            continue
        values[key] = round(max(0.0, min(100.0, ((dt - di) / dt) * 100.0)), 1)

    core_keys = sorted(
        (key for key in values if key != "cpu"),
        key=lambda key: int(key[3:]) if key[3:].isdigit() else 999,
    )
    return values.get("cpu"), [values[key] for key in core_keys]


def _temperature(lines: list[str]) -> tuple[float | None, str | None]:
    readings: list[tuple[int, float, str]] = []
    for line in lines:
        parts = line.split("|", 3)
        if len(parts) != 4:
            continue
        sensor, metric, label, raw = parts
        try:
            celsius = float(raw) / 1000.0
        except ValueError:
            continue
        if not 8.0 <= celsius <= 110.0:
            continue
        sensor_l = sensor.casefold()
        label_l = label.casefold()
        if sensor_l in {"radeon", "amdgpu", "nouveau"}:
            continue
        priority = 1
        if sensor_l == "acpitz":
            priority = 2
        if sensor_l in {"coretemp", "k10temp", "zenpower"}:
            priority = 3
        if any(key in label_l for key in ("package id", "tctl", "tdie", "cputin")):
            priority = 4
        source_label = label or metric
        readings.append((priority, celsius, f"{sensor}/{source_label}"))
    if not readings:
        return None, None
    priority = max(item[0] for item in readings)
    _, celsius, source = max(
        (item for item in readings if item[0] == priority),
        key=lambda item: item[1],
    )
    return round(celsius, 1), source


def _parse_task(lines: list[str], node_id: str) -> dict[str, Any] | None:
    candidates: list[tuple[int, int, str]] = []
    for line in lines:
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(.+)$", line)
        if match:
            candidates.append((int(match.group(1)), int(match.group(2)), match.group(3)))

    patterns: list[tuple[str, re.Pattern[str]]] = []
    if node_id == "agt":
        patterns.extend([
            ("DISPATCH", re.compile(r"andy-ci-distributed\s+([0-9a-f]{40})\s+(\w+)")),
            ("REPROFILE", re.compile(r"andy-ci-reprofile\s+([0-9a-f]{40})")),
        ])
    else:
        patterns.append(("RUNNER", re.compile(r"andy-ci-run\s+([0-9a-f]{40})\s+(\w+)")))

    for kind, pattern in patterns:
        for pid, elapsed, args in candidates:
            found = pattern.search(args)
            if not found:
                continue
            sha = found.group(1)
            suite = found.group(2) if found.lastindex and found.lastindex >= 2 else "postgres-profile"
            return {
                "kind": kind,
                "pid": pid,
                "elapsed_seconds": elapsed,
                "sha": sha,
                "sha_short": sha[:12],
                "suite": suite,
                "label": f"{suite} · {sha[:12]}",
            }

    for pid, elapsed, args in candidates:
        if "python -m pytest -m postgres" in args:
            return {
                "kind": "PYTEST",
                "pid": pid,
                "elapsed_seconds": elapsed,
                "sha": None,
                "sha_short": None,
                "suite": "postgres",
                "label": "PostgreSQL test shard",
            }
    return None


def _sample_node(node_id: str, cfg: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    try:
        parts = _sections(_run_snapshot(cfg["host"]))
        cpu, cores = _usage(node_id, _parse_cpu(parts["PROC"]))
        temp, temp_source = _temperature(parts["TEMP"])
        task = _parse_task(parts["TASK"], node_id)
        return {
            "id": node_id,
            "label": cfg["label"],
            "role": cfg["role"],
            "reachable": True,
            "state": "RUNNING" if task else "IDLE",
            "cpu_percent": cpu,
            "cores": cores,
            "temperature_c": temp,
            "temperature_source": temp_source,
            "task": task,
            "sample_ms": round((time.monotonic() - started) * 1000),
        }
    except Exception:
        return {
            "id": node_id,
            "label": cfg["label"],
            "role": cfg["role"],
            "reachable": False,
            "state": "OFFLINE",
            "cpu_percent": None,
            "cores": [],
            "temperature_c": None,
            "temperature_source": None,
            "task": None,
            "sample_ms": round((time.monotonic() - started) * 1000),
        }


def _recent_dispatches(limit: int = 7) -> list[dict[str, Any]]:
    if not LOG_ROOT.exists():
        return []
    paths = sorted(
        (p for p in LOG_ROOT.iterdir() if p.is_dir() and p.name.endswith("-postgres-distributed")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    result: list[dict[str, Any]] = []
    for path in paths[:limit]:
        bits = path.name.split("-")
        stamp = bits[0] if bits else ""
        sha_short = bits[1] if len(bits) > 1 else "unknown"
        summary_file = path / "summary.json"
        summary = None
        if summary_file.is_file():
            try:
                summary = json.loads(summary_file.read_text())
            except (OSError, json.JSONDecodeError):
                summary = None
        try:
            started_at = datetime.strptime(stamp, "%Y%m%dT%H%M%S").astimezone().isoformat()
        except ValueError:
            started_at = None
        age = int(time.time() - path.stat().st_mtime)
        result.append({
            "id": path.name,
            "sha_short": sha_short,
            "sha": summary.get("sha") if isinstance(summary, dict) else None,
            "suite": "postgres",
            "started_at": started_at,
            "status": summary.get("status") if isinstance(summary, dict) else (
                "RUNNING" if age < 180 else "INCOMPLETE"
            ),
            "wall_seconds": (
                summary.get("wall_seconds")
                if isinstance(summary, dict)
                else age
            ),
            "total_passed_tests": summary.get("total_passed_tests") if isinstance(summary, dict) else None,
            "workers": summary.get("workers", {}) if isinstance(summary, dict) else {},
        })
    return result


def _dispatch(nodes: list[dict[str, Any]], recent: list[dict[str, Any]]) -> dict[str, Any] | None:
    active = [node for node in nodes if node.get("task")]
    if active:
        sha = next((node["task"].get("sha") for node in active if node["task"].get("sha")), None)
        suite = next((node["task"].get("suite") for node in active if node["task"].get("suite")), "postgres")
        elapsed = max((node["task"].get("elapsed_seconds", 0) for node in active), default=0)
        return {
            "status": "RUNNING",
            "sha": sha,
            "sha_short": sha[:12] if sha else None,
            "suite": suite,
            "elapsed_seconds": elapsed,
            "source": "live-process",
        }
    if recent:
        latest = recent[0]
        return {
            "status": latest["status"],
            "sha": latest.get("sha"),
            "sha_short": latest.get("sha_short"),
            "suite": latest.get("suite"),
            "elapsed_seconds": latest.get("wall_seconds"),
            "source": "last-dispatch",
        }
    return None



def _safe_text(value: object, limit: int = 120) -> str:
    text_value = str(value or "").replace("\n", " ").replace("\r", " ").strip()
    text_value = re.sub(
        r"(?i)(token|secret|password|passwd|authorization|bearer|key)=([^\s&;]+)",
        r"\1=<redacted>",
        text_value,
    )
    text_value = re.sub(r"gh[ps]_[A-Za-z0-9_\-]{20,}", "<redacted-token>", text_value)
    return text_value[:limit] + ("…" if len(text_value) > limit else "")


def _tool_summary(tool_name: str, arguments: object) -> str:
    if not isinstance(arguments, dict):
        return "ação via plugin"
    if tool_name == "start_process":
        command = _safe_text(arguments.get("command"), 160)
        for label, pattern in (
            ("distributed CI", r"andy-ci-distributed\s+([0-9a-f]{12,40})\s+(\w+)"),
            ("CI reprofile", r"andy-ci-reprofile\s+([0-9a-f]{12,40})"),
            ("Codex", r"codex\s+exec"),
            ("Git", r"\bgit\s+(clone|fetch|push|commit|rebase|status)\b"),
            ("Docker", r"\bdocker\s+"),
            ("SSH", r"\bssh\s+"),
        ):
            match = re.search(pattern, command)
            if match:
                suffix = " · " + " · ".join(match.groups()) if match.groups() else ""
                return label + suffix
        return command or "console command"
    if tool_name in {"read_file", "write_file", "edit_block", "move_file", "list_directory"}:
        path = arguments.get("path") or arguments.get("source") or arguments.get("destination")
        return _safe_text(path, 110) if path else "filesystem"
    if tool_name in {"read_process_output", "interact_with_process", "kill_process", "force_terminate"}:
        pid = arguments.get("pid")
        return f"PID {pid}" if pid else "process session"
    if tool_name == "get_recent_tool_calls":
        return "histórico do plugin"
    return _safe_text(", ".join(str(key) for key in arguments.keys()), 100) or "plugin action"


def _tool_result_status(output: object) -> str:
    if isinstance(output, dict) and output.get("isError"):
        return "ERROR"
    return "DONE"


def _recent_tool_calls(limit: int = 24) -> list[dict[str, Any]]:
    if not TOOL_HISTORY.is_file():
        return []
    try:
        with TOOL_HISTORY.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            block = 65536
            data = b""
            position = end
            while position > 0 and data.count(b"\n") <= limit + 4:
                size = min(block, position)
                position -= size
                handle.seek(position)
                data = handle.read(size) + data
        lines = [line for line in data.splitlines() if line.strip()][-limit:]
    except OSError:
        return []

    calls: list[dict[str, Any]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        tool_name = str(record.get("toolName") or "unknown")
        calls.append({
            "timestamp": record.get("timestamp"),
            "tool": tool_name,
            "summary": _tool_summary(tool_name, record.get("arguments")),
            "duration_ms": record.get("duration"),
            "status": _tool_result_status(record.get("output")),
        })
    return calls


def _desktop_commander_pid() -> int | None:
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            timeout=0.5,
            check=True,
        )
        candidates = []
        for line in result.stdout.splitlines():
            match = re.match(r"\s*(\d+)\s+(.+)$", line)
            if not match:
                continue
            args = match.group(2)
            if "/opt/desktop-commander/node_modules/@wonderwhy-er/desktop-commander/dist/index.js" not in args:
                continue
            candidates.append((int(match.group(1)), args))
        for pid, args in candidates:
            if not args.rstrip().endswith(" remote"):
                return pid
        return candidates[0][0] if candidates else None
    except Exception:
        return None


def _active_console_sessions(root_pid: int | None) -> list[dict[str, Any]]:
    if root_pid is None:
        return []
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,etimes=,args="],
            capture_output=True,
            text=True,
            timeout=0.8,
            check=True,
        )
    except Exception:
        return []

    rows: dict[int, tuple[int, int, str]] = {}
    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(.+)$", line)
        if not match:
            continue
        pid, ppid, elapsed = map(int, match.group(1, 2, 3))
        args = match.group(4)
        rows[pid] = (ppid, elapsed, args)
        children.setdefault(ppid, []).append(pid)

    descendants: list[int] = []
    queue = list(children.get(root_pid, []))
    seen: set[int] = set()
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        descendants.append(pid)
        queue.extend(children.get(pid, []))

    ignored_roots = {
        pid
        for pid in descendants
        if any(skip in rows[pid][2] for skip in ("screenshot_watch.sh", "andy-ops-panel"))
        or rows[pid][1] > 21600
    }

    def ignored_by_ancestor(pid: int) -> bool:
        current = pid
        visited: set[int] = set()
        while current in rows and current not in visited:
            if current in ignored_roots:
                return True
            visited.add(current)
            current = rows[current][0]
            if current == root_pid:
                return False
        return False

    sessions: list[dict[str, Any]] = []
    for pid in descendants:
        ppid, elapsed, args = rows[pid]
        if ignored_by_ancestor(pid):
            continue
        if args.startswith(("[", "/usr/bin/ssh-agent")):
            continue
        sessions.append({
            "pid": pid,
            "ppid": ppid,
            "elapsed_seconds": elapsed,
            "summary": _safe_text(args, 150),
        })
    sessions.sort(key=lambda item: item["elapsed_seconds"])
    return sessions[:8]


def _chat_status() -> dict[str, Any]:
    calls = _recent_tool_calls()
    pid = _desktop_commander_pid()
    sessions = _active_console_sessions(pid)
    last = calls[-1] if calls else None
    age_seconds = None
    if last and last.get("timestamp"):
        try:
            stamp = datetime.fromisoformat(str(last["timestamp"]).replace("Z", "+00:00"))
            age_seconds = max(0, int((datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()))
        except ValueError:
            pass

    if pid is None:
        channel_state = "OFFLINE"
    elif sessions:
        channel_state = "WORKING"
    elif age_seconds is not None and age_seconds <= 10:
        channel_state = "ACTIVE"
    else:
        channel_state = "IDLE"

    return {
        "plugin": "Remote Desktop Commander",
        "online": pid is not None,
        "pid": pid,
        "state": channel_state,
        "last_activity_age_seconds": age_seconds,
        "last_call": last,
        "active_sessions": sessions,
        "recent_calls": list(reversed(calls)),
        "history_persistent": TOOL_HISTORY.is_file(),
    }


def _sample_loop() -> None:
    executor = ThreadPoolExecutor(max_workers=len(NODES))
    while True:
        cycle = time.monotonic()
        futures = {
            executor.submit(_sample_node, node_id, cfg): node_id
            for node_id, cfg in NODES.items()
        }
        nodes_by_id: dict[str, dict[str, Any]] = {}
        for future in as_completed(futures):
            node_id = futures[future]
            try:
                nodes_by_id[node_id] = future.result()
            except Exception:
                cfg = NODES[node_id]
                nodes_by_id[node_id] = {
                    "id": node_id,
                    "label": cfg["label"],
                    "role": cfg["role"],
                    "reachable": False,
                    "state": "OFFLINE",
                    "cpu_percent": None,
                    "cores": [],
                    "temperature_c": None,
                    "temperature_source": None,
                    "task": None,
                }

        nodes = [nodes_by_id[node_id] for node_id in NODES]
        recent = _recent_dispatches()
        payload = {
            "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
            "nodes": nodes,
            "dispatch": _dispatch(nodes, recent),
            "recent": recent,
            "chat": _chat_status(),
        }
        with _state_lock:
            _state.clear()
            _state.update(payload)

        remaining = POLL_SECONDS - (time.monotonic() - cycle)
        if remaining > 0:
            time.sleep(remaining)


class OpsHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PROJECT_DIR), **kwargs)

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'",
        )
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._json({"status": "ok", "service": "andy-ops-panel"})
            return
        if path == "/api/status":
            with _state_lock:
                payload = json.loads(json.dumps(_state))
            self._json(payload)
            return
        super().do_GET()


def main() -> None:
    threading.Thread(target=_sample_loop, name="ops-sampler", daemon=True).start()
    time.sleep(0.35)
    server = ThreadingHTTPServer((LISTEN_ADDRESS, LISTEN_PORT), OpsHandler)
    print(f"Andy Ops Panel listening on http://{LISTEN_ADDRESS}:{LISTEN_PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
