from datetime import timedelta
from uuid import uuid4
import pytest
from sqlalchemy import select, func
from tests.client.test_client_location_service import (
    session as location_session_fixture,
    issue_session,
    settings,
    NOW,
    HUMAN_ID,
    TENANT_ID,
    DEVICE_ID,
)
from attention_router.application.location_history import (
    LocationHistoryService,
    HistoryConflict,
    HistoryInvalid,
)
from attention_router.application.client_location import ClientLocationAuthorityRejected
from attention_router.api.v1.location_history import (
    ObservationBatch,
    Observation,
    build_location_history_router,
)
from attention_router.infrastructure.location_history_models import LocationObservationRow
from attention_router.infrastructure.client_location_models import ClientLocationSnapshotRow
from attention_router.infrastructure.client_bootstrap_models import ClientTenantMembershipRow


@pytest.fixture
def session():
    yield from location_session_fixture.__wrapped__()


def point(at=NOW, **kw):
    return Observation(
        observation_id=uuid4(),
        latitude=1,
        longitude=2,
        accuracy_m=20,
        captured_at=at,
        precision="PRECISE",
        provider="gps",
        **kw,
    )


def batch(points):
    return ObservationBatch(
        tenant_id=TENANT_ID, human_identity_id=HUMAN_ID, device_id=DEVICE_ID, observations=points
    )


def test_old_offline_batch_replay_keeps_original_time_and_current(session):
    sessions, issued = issue_session(session)
    svc = LocationHistoryService(settings=settings(), session_service=sessions)
    fresh = point(NOW + timedelta(seconds=3))
    old = point(NOW - timedelta(days=30))
    for items in [[fresh], [old], [fresh, old]]:
        result = svc.ingest(
            session,
            session_token=issued.session_token,
            batch=batch(items),
            now=NOW + timedelta(seconds=10),
        )
        session.commit()
        assert set(result["acknowledged_ids"]) == {str(p.observation_id) for p in items}
    assert session.scalar(select(func.count()).select_from(LocationObservationRow)) == 2
    current = session.scalar(select(ClientLocationSnapshotRow))
    assert current.captured_at.replace(tzinfo=NOW.tzinfo) == fresh.captured_at
    record = session.get(LocationObservationRow, (TENANT_ID, DEVICE_ID, str(old.observation_id)))
    assert record.captured_at.replace(tzinfo=NOW.tzinfo) == old.captured_at


def test_old_only_never_claims_current(session):
    sessions, issued = issue_session(session)
    svc = LocationHistoryService(settings=settings(), session_service=sessions)
    svc.ingest(
        session,
        session_token=issued.session_token,
        batch=batch([point(NOW - timedelta(days=365))]),
        now=NOW + timedelta(seconds=10),
    )
    assert session.scalar(select(func.count()).select_from(ClientLocationSnapshotRow)) == 0


def test_reused_id_with_changed_coordinates_is_conflict(session):
    sessions, issued = issue_session(session)
    svc = LocationHistoryService(settings=settings(), session_service=sessions)
    p = point()
    svc.ingest(
        session,
        session_token=issued.session_token,
        batch=batch([p]),
        now=NOW + timedelta(seconds=10),
    )
    session.commit()
    changed = p.model_copy(update={"latitude": 3})
    with pytest.raises(HistoryConflict):
        svc.ingest(
            session,
            session_token=issued.session_token,
            batch=batch([changed]),
            now=NOW + timedelta(seconds=10),
        )


@pytest.mark.parametrize("field", ["tenant_id", "device_id", "human_identity_id"])
def test_context_mismatch_rejected(session, field):
    sessions, issued = issue_session(session)
    svc = LocationHistoryService(settings=settings(), session_service=sessions)
    with pytest.raises(ClientLocationAuthorityRejected):
        svc.ingest(
            session,
            session_token=issued.session_token,
            batch=batch([point()]).model_copy(update={field: "other"}),
            now=NOW + timedelta(seconds=10),
        )


def test_member_cannot_upload_owner_location(session):
    sessions, issued = issue_session(session)
    membership = session.scalar(select(ClientTenantMembershipRow))
    membership.role = "MEMBER"
    session.commit()
    svc = LocationHistoryService(settings=settings(), session_service=sessions)
    with pytest.raises(ClientLocationAuthorityRejected):
        svc.ingest(
            session,
            session_token=issued.session_token,
            batch=batch([point()]),
            now=NOW + timedelta(seconds=10),
        )


def test_future_rejected_without_changing_timestamps(session):
    sessions, issued = issue_session(session)
    svc = LocationHistoryService(settings=settings(), session_service=sessions)
    with pytest.raises(HistoryInvalid):
        svc.ingest(
            session,
            session_token=issued.session_token,
            batch=batch([point(NOW + timedelta(hours=1))]),
            now=NOW + timedelta(seconds=10),
        )


def test_invalid_payload_does_not_echo_private_coordinates(session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    sessions, _ = issue_session(session)
    app = FastAPI()
    svc = LocationHistoryService(settings=settings(), session_service=sessions)
    app.include_router(build_location_history_router(get_session=lambda: session, service=svc))
    client = TestClient(app)
    payload = batch([point()]).model_dump(mode="json")
    payload["observations"][0]["latitude"] = 123.456789
    response = client.post("/api/v1/client/location/observations", json=payload)
    assert response.status_code == 422 and "123.456789" not in response.text
