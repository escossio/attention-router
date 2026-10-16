"""One admitted synthetic investigation; the parent alone supervises the SDK child."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
from uuid import UUID, uuid4

from attention_router.investigator.activity_journal import InvestigatorJournal
from attention_router.investigator.agents_sdk_runtime import (
    AgentsSDKInvestigatorRuntime, ModelPricing,
)
from attention_router.investigator.model_usage_gate import ModelLimits, ModelUsage, ModelUsageGate
from attention_router.investigator.process_supervisor import run_with_deadline
from attention_router.investigator.projection import IncidentProjection
from attention_router.investigator.service import Budget, Investigator
from attention_router.investigator.simulated import MemoryAudit, MemoryStore
from evals.investigator_v0.manifest import load_public_synthetic_case

CASE = Path(__file__).resolve().parents[2] / "evals/investigator_v0/synthetic_case_001.json"
MODEL = "gpt-5.6-luna"
PRICING = ModelPricing(MODEL, "standard-2026-10-16", 200_000, 1_200_000)
QUOTE = ModelUsage(4_000, 2_000, 3_200)
LIMITS = ModelLimits(4_000, 2_000, 3_200, max_calls=1)
EXIT = {"completed": 0, "blocked": 2, "failed": 3, "limited": 4, "timeout": 124}


def _outside_repo(path: Path) -> bool:
    return path.is_absolute() and CASE.parents[2] not in path.resolve(strict=False).parents


def _private_file(path: Path) -> bool:
    if not _outside_repo(path) or path.is_symlink():
        return False
    try:
        info = path.stat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and (info.st_mode & 0o777) == 0o600 \
        and info.st_uid == os.geteuid()


_SAFE_TEXT = re.compile(r"^[\w\s.,;:!?()%-]+$", re.UNICODE)
_PRIVATE_PATTERN = re.compile(
    r"\b\d{1,3}(?:\.\d{1,3}){3}\b|\b\d{6,}\b|"
    r"[A-Za-z0-9_]{24,}|sk-[A-Za-z0-9_-]{8,}|\bBearer\b",
    re.IGNORECASE,
)


def _safe_text(value: object, limit: int) -> str | None:
    if type(value) is not str or any(ord(char) < 32 and not char.isspace() for char in value):
        return None
    clean = " ".join(value.split())
    if not clean or len(clean) > limit or not _SAFE_TEXT.fullmatch(clean):
        return None
    return None if _PRIVATE_PATTERN.search(clean) else clean


def _safe_conclusion(result, allowed: dict[str, frozenset[str]]) -> dict[str, object] | None:
    if result.status != "completed" or result.findings is None:
        return None
    conclusion = result.findings.conclusion
    if conclusion is None or conclusion.classification not in {"PROVEN", "INDICATIVE", "UNKNOWN"}:
        return None
    evidence = result.findings.evidence
    if any(item not in result.partial_evidence for item in evidence):
        return None
    indexes = conclusion.evidence_indexes
    if len(indexes) > 4 or any(type(i) is not int or i < 0 or i >= len(evidence) for i in indexes):
        return None
    refs = []
    for index in indexes:
        query = evidence[index].query
        if query.operation not in allowed.get(query.source, frozenset()):
            return None
        refs.append({"index": index, "source": query.source, "operation": query.operation})
    summary = _safe_text(conclusion.summary, 320)
    limitations = [
        _safe_text(item, 160) or "REDACTED"
        for item in conclusion.limitations[:8]
    ]
    return {
        "classification": conclusion.classification,
        "summary": summary,
        "summary_redacted": summary is None,
        "limitations": limitations,
        "limitations_truncated": len(conclusion.limitations) > 8,
        "evidence_refs": refs,
    }


def _summary(run_id: str, status: str, *, query_count: int = 0,
             usage: ModelUsage | None = None, usage_state: str = "unknown",
             conclusion: dict[str, object] | None = None,
             limited: bool = False) -> dict[str, object]:
    return {
        "run_id": run_id, "status": status, "query_count": query_count,
        "usage_state": usage_state, "conclusion": conclusion,
        "usage": None if usage is None else {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_micro_usd": usage.cost_micro_usd,
        },
        "limited": limited,
    }


def _write_summary(path: Path, data: dict[str, object]) -> None:
    if not _outside_repo(path):
        raise ValueError("RESULT_PRIVATE_PATH_REQUIRED")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _admit():
    event, connectors, allowed = load_public_synthetic_case(CASE)
    manifest = json.loads(CASE.read_text(encoding="utf-8"))
    if (manifest["case_id"] != "synthetic-resource-observation-001"
            or event.tenant_id != "synthetic-tenant"
            or event.source != "synthetic-zabbix"
            or not connectors or not allowed):
        raise ValueError("SYNTHETIC_ADMISSION_INVALID")
    projection = IncidentProjection(
        event_sha256=event.digest, symptom="resource_pressure", severity="warning",
        observed_at="2030-01-02T11:30:00Z", target_role="ci_worker",
        observed_value=91, unit="percent",
    )
    projection.for_event(event)
    return event, connectors, allowed, projection


def _preflight(key_file: Path) -> bool:
    if not _private_file(key_file) or PRICING.model != MODEL:
        return False
    if QUOTE != ModelUsage(4_000, 2_000, PRICING.cost_micro_usd(4_000, 2_000)):
        return False
    try:
        _admit()
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return True


def _child(key_file: Path, result_file: Path, journal_file: Path, run_id: str) -> int:
    if not _preflight(key_file):
        _write_summary(result_file, _summary(run_id, "blocked"))
        return EXIT["blocked"]
    # Open only the operator-projected key file. Never load an ambient .env.
    fd = os.open(key_file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (info.st_mode & 0o777) != 0o600 or info.st_uid != os.geteuid():
            raise ValueError("KEY_FILE_NOT_PRIVATE")
        key = os.read(fd, 4097).decode("utf-8").strip()
    finally:
        os.close(fd)
    if not key or len(key) > 4096 or "\n" in key:
        _write_summary(result_file, _summary(run_id, "blocked"))
        return EXIT["blocked"]

    from agents import set_default_openai_client, set_tracing_disabled
    from openai import AsyncOpenAI

    set_tracing_disabled(True)
    set_default_openai_client(AsyncOpenAI(api_key=key, max_retries=0), use_for_tracing=False)
    del key
    event, connectors, allowed, projection = _admit()
    gate = ModelUsageGate(LIMITS)
    runtime = AgentsSDKInvestigatorRuntime(
        model=MODEL, enable_provider=True, max_turns=4,
        projection_resolver=lambda candidate: projection,
        model_usage_gate=gate, model_quote=QUOTE, model_pricing=PRICING,
        model_attempt_timeout_seconds=15, max_output_tokens_per_attempt=500,
    )
    investigator = Investigator(
        runtime, connectors, MemoryStore(), MemoryAudit(), Budget(4, 70),
        authorized_tenants=frozenset({"synthetic-tenant"}),
        allowed_operations=allowed, activity_sink=InvestigatorJournal(journal_file),
    )
    result = investigator.investigate(event, investigation_id=run_id)
    status = result.status if result.status in EXIT else "failed"
    measured = gate.spent
    usage = measured if measured.input_tokens + measured.output_tokens else None
    usage_state = (
        "partial" if usage is not None and gate.terminal_reason is not None
        else "complete" if usage is not None else "unknown"
    )
    _write_summary(result_file, _summary(
        run_id, status, query_count=result.query_count,
        usage=usage, usage_state=usage_state,
        conclusion=_safe_conclusion(result, allowed),
        limited=status == "limited" or gate.terminal_reason is not None,
    ))
    return EXIT[status]


def _parent(key_file: Path, result_file: Path, journal_file: Path) -> int:
    run_id = str(uuid4())
    if not _outside_repo(result_file) or result_file.exists() or result_file.is_symlink() or not _outside_repo(journal_file) or journal_file.is_symlink():
        return EXIT["blocked"]
    if not _preflight(key_file) or (journal_file.exists() and not _private_file(journal_file)):
        _write_summary(result_file, _summary(run_id, "blocked"))
        return EXIT["blocked"]
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(CASE.parents[2]),
           "OPENAI_API_KEY_FILE": str(key_file), "PYTHONUNBUFFERED": "1"}
    argv = [sys.executable, "-m", "ops.investigator.real_once", "--child",
            "--result", str(result_file), "--journal", str(journal_file), "--run-id", run_id]
    outcome = run_with_deadline(argv, deadline_seconds=75, env=env, cwd=str(CASE.parents[2]))
    if outcome.timed_out:
        InvestigatorJournal(journal_file).emit(run_id, "timeout")
        if not result_file.exists():
            _write_summary(result_file, _summary(run_id, "timeout", limited=True))
        return EXIT["timeout"]
    if not result_file.exists():
        _write_summary(result_file, _summary(run_id, "failed"))
        return EXIT["failed"]
    try:
        summary = json.loads(result_file.read_text(encoding="utf-8"))
        if summary["run_id"] != run_id or summary["status"] not in EXIT:
            return EXIT["failed"]
    except (OSError, ValueError, KeyError, TypeError):
        return EXIT["failed"]
    return EXIT[summary["status"]] if outcome.exit_code == EXIT[summary["status"]] else EXIT["failed"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-id", help=argparse.SUPPRESS)
    args = parser.parse_args()
    key_file = Path(os.environ.get("OPENAI_API_KEY_FILE", ""))
    if args.child:
        try:
            UUID(args.run_id)
        except (TypeError, ValueError):
            return EXIT["blocked"]
        return _child(key_file, args.result, args.journal, args.run_id)
    return _parent(key_file, args.result, args.journal)


if __name__ == "__main__":
    raise SystemExit(main())
