from datetime import timedelta

from attention_router.domain.models import now_utc
from attention_router.platform.standing_directives import (
    create_standing_directive,
    resolve_effective_standing_directives,
    revoke_standing_directive,
)


def test_standing_directive_resolves_by_audience_and_expires(session):
    now = now_utc()
    row = create_standing_directive(
        session,
        tenant_id="tenant-default",
        subject_actor_id="owner",
        created_by_actor_id="owner",
        trigger_type="INBOUND_MESSAGE",
        effect_type="DISCLOSE_CURRENT_PRESENCE",
        audience_selector={"type": "AUDIENCE", "audience": "synthetic_test"},
        provenance="test",
        valid_from=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=1),
    )
    session.flush()
    assert resolve_effective_standing_directives(
        session, tenant_id="tenant-default", subject_actor_id="owner",
        trigger_type="INBOUND_MESSAGE", audience="synthetic_test", now=now,
    ) == [row]
    assert resolve_effective_standing_directives(
        session, tenant_id="tenant-default", subject_actor_id="owner",
        trigger_type="INBOUND_MESSAGE", audience="other", now=now,
    ) == []


def test_revoked_directive_never_resolves(session):
    row = create_standing_directive(
        session, tenant_id="tenant-default", subject_actor_id="owner",
        created_by_actor_id="owner", trigger_type="INBOUND_MESSAGE",
        effect_type="DISCLOSE_CURRENT_PRESENCE", audience_selector={"type": "EVERYONE"},
        provenance="test",
    )
    revoke_standing_directive(session, row.id, revoked_by="owner")
    assert resolve_effective_standing_directives(
        session, tenant_id="tenant-default", subject_actor_id="owner",
        trigger_type="INBOUND_MESSAGE", audience="any", now=now_utc(),
    ) == []
