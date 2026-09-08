import argparse
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from attention_router.application.real_pilot import (
    MAX_PROVIDER_CALLS,
    OpenAIPilotProvider,
    PilotConfig,
    build_prompt,
    ensure_output_files,
    load_behavior_spec_text,
    process_events,
    summarize_outputs,
)
from attention_router.config import settings
from attention_router.infrastructure.db import SessionLocal


SESSION_ID = "a52ef536-fdee-407c-ac25-59501445563e"


def parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Andy Real Pilot V0 shadow runner.")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--started-at", default=None)
    parser.add_argument("--test-actor-id", default=os.getenv("ANDY_PILOT_TEST_ACTOR_ID"))
    parser.add_argument("--consent-adult", action="store_true")
    parser.add_argument("--consent-ai", action="store_true")
    parser.add_argument("--kill-switch", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--matrix-path", default=None)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    pilot_secret = os.getenv("ANDY_PILOT_HMAC_SECRET") or secrets.token_urlsafe(32)
    config = PilotConfig(
        output_dir=output_dir,
        session_id=SESSION_ID,
        started_at=parse_dt(args.started_at),
        test_actor_external_id=args.test_actor_id,
        consent_adult=args.consent_adult,
        consent_ai=args.consent_ai,
        pilot_secret=pilot_secret,
        shadow_mode=True,
        kill_switch=args.kill_switch,
        max_provider_calls=MAX_PROVIDER_CALLS,
        allow_manual_send=False,
    )
    behavior_spec, spec_sources = load_behavior_spec_text(Path(args.matrix_path) if args.matrix_path else None)
    with SessionLocal() as session:
        row = session.execute(
            text(
                "select status,current_stage,blueprint_id,answers->>'_ai_needs_clarification' as needs,"
                " answers->>'ai_success_criteria' as criteria from configuration_sessions where id=:id"
            ),
            {"id": SESSION_ID},
        ).mappings().one()
        outbox = session.execute(text("select count(*) as count, coalesce(max(created_at)::text,'') as max_created_at from outbox_messages")).mappings().one()
        session_snapshot = {
            "id": SESSION_ID,
            "status": row["status"],
            "stage": row["current_stage"],
            "blueprint_id": row["blueprint_id"],
            "needs_clarification": row["needs"],
            "criteria_text_present": bool(row["criteria"]),
            "outbox_count_before": outbox["count"],
            "outbox_max_created_at_before": outbox["max_created_at"],
        }
        capabilities = {
            "whatsapp_inbound_real": True,
            "candidate_generation_provider": "openai",
            "candidate_generation_model": settings.agent_builder_openai_model,
            "shadow_mode": True,
            "manual_send_enabled": False,
            "tts_enabled": settings.tts_enabled,
            "tts_profile": settings.tts_profile,
            "tts_auto_send": settings.tts_auto_send,
            "real_escalation_with_retumption": False,
        }
        prompt = build_prompt(session_snapshot, behavior_spec, capabilities)
        actor_pseudonym = config.pseudonym(args.test_actor_id) if args.test_actor_id else None
        sanitized_config = {
            "pilot": "Andy Real Pilot V0",
            "behavior_spec": "ExperimentalBehaviorSpec V0 — NOT APPROVED — PILOT ONLY",
            "started_at": config.started_at.isoformat(),
            "session_id": SESSION_ID,
            "test_actor_configured": config.test_actor_configured(),
            "test_actor_pseudonym": actor_pseudonym,
            "consent_adult": config.consent_adult,
            "consent_ai": config.consent_ai,
            "provider": "openai",
            "model": settings.agent_builder_openai_model,
            "shadow_mode": True,
            "manual_send_enabled": False,
            "max_provider_calls": MAX_PROVIDER_CALLS,
            "concurrency": 1,
            "tts_enabled": settings.tts_enabled,
            "tts_profile": settings.tts_profile,
            "tts_auto_send": settings.tts_auto_send,
            "sources": spec_sources,
        }
        ensure_output_files(config, prompt, behavior_spec, sanitized_config)
        provider = OpenAIPilotProvider()
        summary = process_events(session, config, provider, prompt, capabilities)
        summarize_outputs(
            output_dir,
            summary,
            {
                "provider_model": settings.agent_builder_openai_model,
                "actor_pseudonym": actor_pseudonym,
                "session_snapshot_before": session_snapshot,
                "manual_send_enabled": False,
                "tts_auto_send": settings.tts_auto_send,
            },
        )
    print(json.dumps({"output_dir": str(output_dir), "summary": summary, "test_actor_pseudonym": actor_pseudonym}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
