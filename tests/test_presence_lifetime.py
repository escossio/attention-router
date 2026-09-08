from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from attention_router.application.platform.capability_pack import (
    execute_owner_capability,
    provision_internal_providers,
)
from attention_router.application.platform.context import DatabaseStateRetriever
from attention_router.application.platform.disclosure import evaluate_disclosure_authority
from attention_router.application.platform.registry import (
    capability_and_version,
    sync_capability_definitions,
)
from attention_router.core.capabilities import CapabilityRequest
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import (
    ActorBindingRow,
    AuditEventRow,
    CanonicalEventRow,
    CapabilityVersionRow,
    EntityStateRow,
    TimelineEventRow,
)
from attention_router.provisioning.manifests import CapabilityManifest, load_capability_manifest


OWNER = "presence-lifetime-owner"
EXECUTION_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
DEFAULT_TTL_SECONDS = 14_400
MAX_TTL_SECONDS = 86_400


def _presence_manifest_entry():
    return next(
        item
        for item in load_capability_manifest().capabilities
        if item.canonical_name == "presence.set"
    )


def _old_presence_manifest() -> CapabilityManifest:
    entry = _presence_manifest_entry().model_copy(
        update={
            "version": 2,
            "input_schema": {
                "type": "object",
                "required": ["state"],
                "properties": {
                    "state": {"type": "string"},
                    "expires_at": {"type": "string"},
                },
            },
            "output_schema": {"type": "object"},
            "metadata": {"component": "internal_presence"},
        }
    )
    return CapabilityManifest(schema_version="1", capabilities=[entry])


@pytest.fixture()
def presence_session(session, monkeypatch):
    stamp = now_utc()
    session.add(
        ActorBindingRow(
            id="presence-lifetime-owner-binding",
            tenant_id=DEFAULT_TENANT_ID,
            source="test",
            external_actor_id="presence-lifetime-owner@test",
            actor_key=OWNER,
            actor_category="owner",
            binding_metadata={"owner": True},
            is_active=True,
            created_at=stamp,
            updated_at=stamp,
        )
    )
    provision_internal_providers(session, DEFAULT_TENANT_ID)
    session.flush()
    monkeypatch.setattr(
        "attention_router.application.platform.capability_pack.now_utc",
        lambda: EXECUTION_NOW,
    )
    return session


def _run(session, parameters):
    return execute_owner_capability(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=OWNER,
        request=CapabilityRequest(
            capability="presence.set",
            parameters=parameters,
            user_requested=True,
            confidence="high",
        ),
        correlation_id=new_id(),
    )


def _state(session):
    return session.scalar(
        select(EntityStateRow).where(
            EntityStateRow.tenant_id == DEFAULT_TENANT_ID,
            EntityStateRow.subject_type == "ACTOR",
            EntityStateRow.subject_id == OWNER,
            EntityStateRow.state_namespace == "presence",
            EntityStateRow.state_key == "effective",
        )
    )


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def test_presence_manifest_v3_declares_bounded_lifetime():
    entry = _presence_manifest_entry()
    assert entry.version == 3
    assert entry.metadata["presence_is_perishable"] is True
    assert entry.metadata["default_ttl_seconds"] == DEFAULT_TTL_SECONDS
    assert entry.metadata["max_ttl_seconds"] == MAX_TTL_SECONDS
    properties = entry.input_schema["properties"]
    assert properties["state"]["enum"] == [
        "available",
        "busy",
        "sleeping",
        "away",
        "do_not_disturb",
        "custom",
    ]
    assert properties["audience_scope"]["type"] == "string"
    assert properties["expires_at"] == {"type": "string", "format": "date-time"}
    assert properties["ttl_seconds"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": MAX_TTL_SECONDS,
    }


def test_registry_preserves_v2_and_makes_v3_current(session):
    sync_capability_definitions(session, _old_presence_manifest())
    definition, old = capability_and_version(session, DEFAULT_TENANT_ID, "presence.set")
    old_snapshot = {
        column.name: getattr(old, column.name) for column in old.__table__.columns
    }

    sync_capability_definitions(session)
    definition, current = capability_and_version(session, DEFAULT_TENANT_ID, "presence.set")
    versions = session.scalars(
        select(CapabilityVersionRow)
        .where(CapabilityVersionRow.capability_id == definition.id)
        .order_by(CapabilityVersionRow.version)
    ).all()

    assert [row.version for row in versions] == [2, 3]
    assert definition.current_version_id == current.id
    assert current.version == 3
    assert {
        column.name: getattr(versions[0], column.name)
        for column in versions[0].__table__.columns
    } == old_snapshot


@pytest.mark.parametrize(
    "presence_state",
    ["available", "busy", "sleeping", "away", "do_not_disturb", "custom"],
)
def test_every_presence_state_without_expiry_uses_default_ttl(
    presence_session, presence_state
):
    outcome = _run(presence_session, {"state": presence_state})
    row = _state(presence_session)

    assert outcome.status == "EXECUTED"
    assert outcome.result == {
        "status": presence_state,
        "version": 1,
        "expires_at": (EXECUTION_NOW + timedelta(seconds=DEFAULT_TTL_SECONDS)).isoformat(),
        "expiry_source": "DEFAULT_TTL",
    }
    assert _aware(row.effective_at) == EXECUTION_NOW
    assert _aware(row.expires_at) == EXECUTION_NOW + timedelta(
        seconds=DEFAULT_TTL_SECONDS
    )
    assert presence_session.scalar(
        select(func.count())
        .select_from(EntityStateRow)
        .where(
            EntityStateRow.state_namespace == "presence",
            EntityStateRow.expires_at.is_(None),
        )
    ) == 0


def test_explicit_future_expiry_is_preserved_and_reported(presence_session):
    expiry = EXECUTION_NOW + timedelta(hours=23, minutes=30)
    outcome = _run(
        presence_session,
        {"state": "sleeping", "expires_at": expiry.isoformat()},
    )

    assert outcome.status == "EXECUTED"
    assert outcome.result["expires_at"] == expiry.isoformat()
    assert outcome.result["expiry_source"] == "EXPLICIT_EXPIRES_AT"
    assert _aware(_state(presence_session).expires_at) == expiry


def test_naive_explicit_expiry_is_rejected_without_state_mutation(presence_session):
    outcome = _run(
        presence_session,
        {"state": "away", "expires_at": "2026-09-05T08:00:00"},
    )

    assert outcome.status == "FAILED"
    assert outcome.reason_code == "PRESENCE_EXPIRY_TIMEZONE_REQUIRED"
    assert _state(presence_session) is None
    assert presence_session.scalar(
        select(func.count())
        .select_from(CanonicalEventRow)
        .where(CanonicalEventRow.event_type == "PRESENCE_SET")
    ) == 0


def test_offset_explicit_expiry_is_normalized_to_utc(presence_session):
    outcome = _run(
        presence_session,
        {"state": "away", "expires_at": "2026-09-05T08:00:00-03:00"},
    )

    expected = datetime(2026, 9, 5, 11, 0, tzinfo=timezone.utc)
    assert outcome.status == "EXECUTED"
    assert outcome.result["expires_at"] == expected.isoformat()
    assert _aware(_state(presence_session).expires_at) == expected


def test_z_explicit_expiry_matches_equivalent_offset(presence_session):
    outcome = _run(
        presence_session,
        {"state": "away", "expires_at": "2026-09-05T11:00:00Z"},
    )

    expected = datetime(2026, 9, 5, 11, 0, tzinfo=timezone.utc)
    assert outcome.status == "EXECUTED"
    assert outcome.result["expires_at"] == expected.isoformat()
    assert _aware(_state(presence_session).expires_at) == expected


@pytest.mark.parametrize(
    ("parameters", "reason"),
    [
        (
            {"state": "sleeping", "expires_at": "not-a-date"},
            "PRESENCE_EXPIRY_INVALID",
        ),
        (
            {"state": "sleeping", "expires_at": EXECUTION_NOW.isoformat()},
            "PRESENCE_EXPIRY_NOT_FUTURE",
        ),
        (
            {
                "state": "sleeping",
                "expires_at": (EXECUTION_NOW + timedelta(seconds=MAX_TTL_SECONDS + 1)).isoformat(),
            },
            "PRESENCE_EXPIRY_EXCEEDS_MAX",
        ),
        (
            {"state": "sleeping", "expires_at": None},
            "PRESENCE_EXPIRY_INVALID",
        ),
    ],
)
def test_invalid_explicit_expiry_fails_without_state_mutation(
    presence_session, parameters, reason
):
    outcome = _run(presence_session, parameters)

    assert outcome.status == "FAILED"
    assert outcome.reason_code == reason
    assert _state(presence_session) is None
    assert presence_session.scalar(
        select(func.count())
        .select_from(CanonicalEventRow)
        .where(CanonicalEventRow.event_type == "PRESENCE_SET")
    ) == 0


def test_invalid_expiry_does_not_mutate_existing_presence(presence_session):
    first = _run(presence_session, {"state": "busy", "ttl_seconds": 600})
    row = _state(presence_session)
    before = (
        row.state_value.copy(),
        row.version,
        row.effective_at,
        row.expires_at,
        row.updated_at,
    )

    rejected = _run(
        presence_session,
        {"state": "sleeping", "expires_at": EXECUTION_NOW.isoformat()},
    )

    assert first.status == "EXECUTED"
    assert rejected.status == "FAILED"
    assert rejected.reason_code == "PRESENCE_EXPIRY_NOT_FUTURE"
    assert (
        row.state_value,
        row.version,
        row.effective_at,
        row.expires_at,
        row.updated_at,
    ) == before


def test_valid_ttl_is_calculated_from_single_execution_time(presence_session):
    outcome = _run(presence_session, {"state": "busy", "ttl_seconds": 7_200})

    assert outcome.status == "EXECUTED"
    assert outcome.result["expiry_source"] == "EXPLICIT_TTL"
    assert outcome.result["expires_at"] == (EXECUTION_NOW + timedelta(hours=2)).isoformat()
    assert _aware(_state(presence_session).expires_at) == EXECUTION_NOW + timedelta(hours=2)


@pytest.mark.parametrize("ttl", [0, -1, MAX_TTL_SECONDS + 1])
def test_ttl_outside_contract_is_rejected(presence_session, ttl):
    outcome = _run(presence_session, {"state": "busy", "ttl_seconds": ttl})

    assert outcome.status == "FAILED"
    assert outcome.reason_code == "PRESENCE_TTL_OUT_OF_RANGE"
    assert _state(presence_session) is None


@pytest.mark.parametrize("ttl", [True, 60.5, "60", None])
def test_non_integer_ttl_is_rejected(presence_session, ttl):
    outcome = _run(presence_session, {"state": "busy", "ttl_seconds": ttl})

    assert outcome.status == "FAILED"
    assert outcome.reason_code == "PRESENCE_TTL_INVALID"
    assert _state(presence_session) is None


def test_explicit_expiry_and_ttl_are_rejected_as_ambiguous(presence_session):
    outcome = _run(
        presence_session,
        {
            "state": "away",
            "expires_at": (EXECUTION_NOW + timedelta(hours=1)).isoformat(),
            "ttl_seconds": 3_600,
        },
    )

    assert outcome.status == "FAILED"
    assert outcome.reason_code == "PRESENCE_EXPIRY_AMBIGUOUS"
    assert _state(presence_session) is None


def test_update_without_expiry_replaces_old_expiry_with_new_default(presence_session):
    first_expiry = EXECUTION_NOW + timedelta(hours=1)
    first = _run(
        presence_session,
        {"state": "busy", "expires_at": first_expiry.isoformat()},
    )
    second = _run(presence_session, {"state": "available"})
    row = _state(presence_session)

    assert first.result["version"] == 1
    assert second.result["version"] == 2
    assert row.version == 2
    assert row.state_value["status"] == "available"
    assert _aware(row.expires_at) == EXECUTION_NOW + timedelta(
        seconds=DEFAULT_TTL_SECONDS
    )
    assert row.expires_at is not None


@pytest.mark.parametrize("presence_state", ["sleeping", "available"])
def test_live_bug_regression_presence_disappears_after_default_ttl(
    presence_session, monkeypatch, presence_state
):
    outcome = _run(
        presence_session,
        {"state": presence_state, "audience_scope": "everyone"},
    )
    assert outcome.status == "EXECUTED"

    class ContextClock(datetime):
        current = EXECUTION_NOW

        @classmethod
        def now(cls, tz=None):
            return cls.current if tz else cls.current.replace(tzinfo=None)

    monkeypatch.setattr(
        "attention_router.application.platform.context.datetime", ContextClock
    )
    retriever = DatabaseStateRetriever(presence_session)
    directive = [SimpleNamespace(effect_type="DISCLOSE_CURRENT_PRESENCE")]

    ContextClock.current = EXECUTION_NOW + timedelta(hours=3)
    current = retriever.retrieve(DEFAULT_TENANT_ID, OWNER, 20)
    assert current[0]["value"]["status"] == presence_state
    assert evaluate_disclosure_authority(
        directives=directive,
        allowed_disclosures=["availability_hint"],
        operational_state=current,
    ).allowed

    ContextClock.current = EXECUTION_NOW + timedelta(hours=4, seconds=1)
    expired = retriever.retrieve(DEFAULT_TENANT_ID, OWNER, 20)
    assert expired == []
    assert not evaluate_disclosure_authority(
        directives=directive,
        allowed_disclosures=["availability_hint"],
        operational_state=expired,
    ).allowed
    assert not any(
        item.get("value", {}).get("status") == "available" for item in expired
    )


def test_presence_audit_and_timeline_include_lifetime_provenance(presence_session):
    outcome = _run(presence_session, {"state": "away", "ttl_seconds": 600})
    timeline = presence_session.scalar(
        select(TimelineEventRow).where(TimelineEventRow.event_type == "STATE_CHANGED")
    )
    audit = presence_session.scalar(
        select(AuditEventRow).where(AuditEventRow.event_type == "capability_state_changed")
    )

    assert timeline.event_ref["expires_at"] == outcome.result["expires_at"]
    assert timeline.event_ref["expiry_source"] == "EXPLICIT_TTL"
    assert audit.payload["expiry_present"] is True
    assert audit.payload["expiry_source"] == "EXPLICIT_TTL"
    assert audit.payload["ttl_seconds"] == 600
