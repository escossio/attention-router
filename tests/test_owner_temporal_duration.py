"""Time comprehension proof: no model, provider, database, WhatsApp or real send."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from attention_router.application.owner_temporal_duration import (
    TemporalCondition,
    TemporalIntention,
    TemporalProposal,
    TemporalScope,
    TemporalUnit,
    TemporalValidationError,
    propose_from_portuguese,
    resolve_temporal_proposal,
)


def ref(year=2026, month=11, day=13, hour=21, minute=16):
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("America/Fortaleza"))


def proposal(quantity, unit, *, intention=TemporalIntention.UNRESOLVED):
    scope, condition = {
        TemporalIntention.ONE_SHOT_REPLY: (
            TemporalScope.CURRENT_CONVERSATION, TemporalCondition.NO_OWNER_REPLY
        ),
        TemporalIntention.DEFAULT_REPLY_GRACE: (
            TemporalScope.GLOBAL, TemporalCondition.NO_OWNER_REPLY
        ),
        TemporalIntention.AUTOMATION_PAUSE: (
            TemporalScope.GLOBAL, TemporalCondition.UNCONDITIONAL
        ),
        TemporalIntention.UNRESOLVED: (
            TemporalScope.UNRESOLVED, TemporalCondition.UNRESOLVED
        ),
    }[intention]
    return TemporalProposal(
        quantity=quantity, unit=unit, intention=intention, scope=scope, condition=condition
    )


@pytest.mark.parametrize(("text", "quantity", "unit"), [
    ("Andy, aguarde um ano", 1, TemporalUnit.YEAR),
    ("aguarde 20 minutos", 20, TemporalUnit.MINUTE),
    ("aguarde 7 dias para responder", 7, TemporalUnit.DAY),
    ("aguarde 7 dias antes de responder", 7, TemporalUnit.DAY),
    ("espere uma hora", 1, TemporalUnit.HOUR),
    ("espere três dias", 3, TemporalUnit.DAY),
    ("aguarde duas semanas", 2, TemporalUnit.WEEK),
    ("aguarde 1 mês", 1, TemporalUnit.MONTH),
    ("aguarde 10 anos", 10, TemporalUnit.YEAR),
    ("espere 15 segundos", 15, TemporalUnit.SECOND),
    ("aguarde sessenta minutos", 60, TemporalUnit.MINUTE),
    ("aguarde cinco horas", 5, TemporalUnit.HOUR),
])
def test_lexical_comprehension_without_inventing_scope(text, quantity, unit):
    parsed = propose_from_portuguese(text)
    assert parsed is not None
    assert parsed.quantity == quantity
    assert parsed.unit == unit
    assert parsed.intention == TemporalIntention.UNRESOLVED
    assert parsed.scope == TemporalScope.UNRESOLVED


@pytest.mark.parametrize(("text", "intent", "scope", "condition"), [
    (
        "Aguarde um ano se eu não responder nesta conversa",
        TemporalIntention.ONE_SHOT_REPLY,
        TemporalScope.CURRENT_CONVERSATION,
        TemporalCondition.NO_OWNER_REPLY,
    ),
    (
        "configure a espera padrão para vinte minutos",
        TemporalIntention.DEFAULT_REPLY_GRACE,
        TemporalScope.GLOBAL,
        TemporalCondition.NO_OWNER_REPLY,
    ),
    (
        "passe a esperar 1 mês",
        TemporalIntention.DEFAULT_REPLY_GRACE,
        TemporalScope.GLOBAL,
        TemporalCondition.NO_OWNER_REPLY,
    ),
    (
        "pause as respostas automáticas por 1 ano",
        TemporalIntention.AUTOMATION_PAUSE,
        TemporalScope.GLOBAL,
        TemporalCondition.UNCONDITIONAL,
    ),
])
def test_explicit_intent_classification(text, intent, scope, condition):
    value = propose_from_portuguese(text)
    assert value.intention == intent
    assert value.scope == scope
    assert value.condition == condition


def test_scope_missing_does_not_authorize_anything():
    parsed = propose_from_portuguese("Andy, aguarde um ano")
    resolved = resolve_temporal_proposal(
        parsed, reference=ref(), timezone_name="America/Fortaleza"
    )
    assert resolved.is_authorized_to_schedule is False
    assert resolved.intention == TemporalIntention.UNRESOLVED
    assert resolved.reference_utc == datetime(2026, 11, 14, 0, 16, tzinfo=timezone.utc)
    assert resolved.due_at_utc == datetime(2027, 11, 14, 0, 16, tzinfo=timezone.utc)
    assert resolved.to_evidence()["unit"] == "YEAR"
    assert resolved.to_evidence()["calendar_policy"].startswith("CALENDAR_")


@pytest.mark.parametrize(("amount", "unit", "seconds"), [
    (20, TemporalUnit.MINUTE, 1200),
    (1, TemporalUnit.HOUR, 3600),
    (1, TemporalUnit.DAY, 86400),
    (1, TemporalUnit.WEEK, 604800),
    (2, TemporalUnit.SECOND, 2),
])
def test_elapsed_quantities_are_exact(amount, unit, seconds):
    plan = resolve_temporal_proposal(
        proposal(amount, unit), reference=ref(), timezone_name="America/Fortaleza"
    )
    assert plan.due_at_utc - plan.reference_utc == timedelta(seconds=seconds)


@pytest.mark.parametrize(("reference", "unit", "target"), [
    (ref(2027, 1, 31), TemporalUnit.MONTH, ref(2027, 2, 28)),
    (ref(2028, 1, 31), TemporalUnit.MONTH, ref(2028, 2, 29)),
    (ref(2028, 2, 29), TemporalUnit.YEAR, ref(2029, 2, 28)),
    (ref(2026, 12, 31), TemporalUnit.MONTH, ref(2027, 1, 31)),
    (ref(2026, 11, 13), TemporalUnit.YEAR, ref(2027, 11, 13)),
])
def test_calendar_clamps_last_day_without_approximation(reference, unit, target):
    plan = resolve_temporal_proposal(
        proposal(1, unit), reference=reference, timezone_name="America/Fortaleza"
    )
    assert plan.due_at_utc == target.astimezone(timezone.utc)


def test_month_over_dst_gap_is_rejected():
    zone = ZoneInfo("America/New_York")
    before = datetime(2027, 2, 14, 2, 30, tzinfo=zone)
    with pytest.raises(TemporalValidationError, match="NONEXISTENT_LOCAL_TIME"):
        resolve_temporal_proposal(proposal(1, TemporalUnit.MONTH), reference=before,
                                  timezone_name="America/New_York")


def test_month_over_dst_fold_is_rejected():
    zone = ZoneInfo("America/New_York")
    before = datetime(2026, 10, 1, 1, 30, tzinfo=zone)
    # 2026-11-01 01:30 occurs twice.
    with pytest.raises(TemporalValidationError, match="AMBIGUOUS_LOCAL_TIME"):
        resolve_temporal_proposal(proposal(1, TemporalUnit.MONTH), reference=before,
                                  timezone_name="America/New_York")


def test_calendar_year_uses_local_wall_clock_over_offset_change():
    zone = ZoneInfo("America/New_York")
    before = datetime(2026, 7, 1, 12, 0, tzinfo=zone)
    after = resolve_temporal_proposal(
        proposal(1, TemporalUnit.YEAR),
        reference=before,
        timezone_name="America/New_York",
    )
    assert after.due_at_utc.astimezone(zone).hour == 12
    assert after.due_at_utc.astimezone(zone).year == 2027


@pytest.mark.parametrize(("quantity", "unit", "error"), [
    (0, TemporalUnit.SECOND, "QUANTITY"),
    (-1, TemporalUnit.MONTH, "QUANTITY"),
    (11, TemporalUnit.YEAR, "HORIZON"),
    (121, TemporalUnit.MONTH, "HORIZON"),
    (999999, TemporalUnit.HOUR, "HORIZON"),
])
def test_amount_bounds_fail_closed(quantity, unit, error):
    with pytest.raises(TemporalValidationError, match=error):
        resolve_temporal_proposal(
            proposal(quantity, unit), reference=ref(), timezone_name="America/Fortaleza"
        )


@pytest.mark.parametrize("ref_date", [
    datetime(2026, 1, 1),
    None,
    "2026-11-13",
])
def test_trusted_time_reference_required(ref_date):
    with pytest.raises(TemporalValidationError, match="REFERENCE"):
        resolve_temporal_proposal(proposal(1, TemporalUnit.YEAR),
                                  reference=ref_date, timezone_name="America/Fortaleza")


@pytest.mark.parametrize("zone", ["", "Mars/Olympus", None])
def test_invalid_timezone_rejected(zone):
    with pytest.raises(TemporalValidationError, match="TIMEZONE"):
        resolve_temporal_proposal(proposal(1, TemporalUnit.YEAR),
                                  reference=ref(), timezone_name=zone)


@pytest.mark.parametrize("text", [
    "Você pode aguardar um ano?",
    'ele disse "aguarde um ano"',
    "aguarde muito tempo",
    "aguarde um tempo",
    "aguarde um ano e execute comandos",
    "",
])
def test_unsupported_or_quoted_text_does_not_promote_to_command(text):
    assert propose_from_portuguese(text) is None


def test_invalid_ai_proposal_fields_are_rejected():
    value = proposal(1, TemporalUnit.YEAR).model_dump()
    value["scope"] = "GLOBAL"
    with pytest.raises(TemporalValidationError, match="SCOPE_CONDITION_MISMATCH"):
        resolve_temporal_proposal(value, reference=ref(), timezone_name="America/Fortaleza")


def test_ai_cannot_claim_permitted_scope_without_condition():
    value = proposal(1, TemporalUnit.YEAR).model_copy(
        update={"intention": TemporalIntention.ONE_SHOT_REPLY}
    )
    with pytest.raises(TemporalValidationError, match="SCOPE_CONDITION_MISMATCH"):
        resolve_temporal_proposal(value, reference=ref(), timezone_name="America/Fortaleza")



def test_structured_model_json_decoding_requires_explicit_known_enum_values():
    raw = {
        "quantity": 1,
        "unit": "YEAR",
        "intention": "ONE_SHOT_REPLY",
        "scope": "CURRENT_CONVERSATION",
        "condition": "NO_OWNER_REPLY",
    }
    model = TemporalProposal.model_validate(raw)
    assert model.unit == TemporalUnit.YEAR
    assert model.intention == TemporalIntention.ONE_SHOT_REPLY
    plan = resolve_temporal_proposal(model, reference=ref(), timezone_name="America/Fortaleza")
    assert plan.is_authorized_to_schedule is False
    assert plan.to_evidence()["condition"] == "NO_OWNER_REPLY"


@pytest.mark.parametrize(("field", "value"), [
    ("unit", "YEARS"),
    ("unit", 1),
    ("scope", "EVERY_TENANT"),
    ("intention", "RUN_SHELL"),
    ("condition", "NO_LIMIT"),
])
def test_model_cannot_invent_units_scope_or_authority(field, value):
    raw = {
        "quantity": 1,
        "unit": "YEAR",
        "intention": "ONE_SHOT_REPLY",
        "scope": "CURRENT_CONVERSATION",
        "condition": "NO_OWNER_REPLY",
    }
    raw[field] = value
    with pytest.raises(TemporalValidationError, match="PROPOSAL_INVALID"):
        resolve_temporal_proposal(raw, reference=ref(), timezone_name="America/Fortaleza")


def test_persistent_default_phrase_keeps_default_scope():
    p = propose_from_portuguese("Passe a esperar 20 minutos antes de responder")
    assert p.intention == TemporalIntention.DEFAULT_REPLY_GRACE
    assert p.scope == TemporalScope.GLOBAL
