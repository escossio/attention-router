"""Synthetic tests: GPS time correlation does not fabricate owner presence."""

from datetime import timedelta
import hashlib

import pytest
from sqlalchemy import func, select

from attention_router.application.client_location import (
    ClientLocationAuthorityRejected,
    ClientLocationHistoryDisabled,
    ClientLocationService,
    ClientLocationUnavailable,
)
from attention_router.infrastructure.client_bootstrap_models import (
    ClientDeviceRow, ClientTenantMembershipRow,
)
from attention_router.infrastructure.client_location_history_models import (
    ClientLocationObservationRow,
)
from attention_router.infrastructure.client_location_models import ClientLocationSnapshotRow
from attention_router.infrastructure.models import (
    ConversationMessageRow,
    ConversationThreadRow,
)
from tests.client.test_client_location_service import (
    DEVICE_ID, HUMAN_ID, NOW, TENANT_ID,
    issue_session, location_service as old_location_service,
    settings as old_settings, observation,
)


@pytest.fixture
def session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from attention_router.infrastructure.db import Base

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as db:
        yield db
    engine.dispose()


def location_service(session_service):
    return ClientLocationService(
        settings=old_settings().model_copy(
            update={"client_location_history_enabled": True}
        ),
        session_service=session_service,
    )


def test_old_share_current_still_does_not_create_history(session):
    auth, issued = issue_session(session)
    old_location_service(auth).put_current(
        session, session_token=issued.session_token,
        observation=observation(),
        now=NOW+timedelta(seconds=4),
    )
    session.commit()
    assert session.scalar(select(func.count()).select_from(ClientLocationSnapshotRow))==1
    assert session.scalar(select(func.count()).select_from(ClientLocationObservationRow))==0


def test_history_capture_disabled_until_explicitly_enabled(session):
    auth, issued = issue_session(session)
    with pytest.raises(ClientLocationHistoryDisabled):
        old_location_service(auth).put_context_observation(
            session, session_token=issued.session_token,
            observation=observation(),now=NOW+timedelta(seconds=4),
        )
    assert session.scalar(select(func.count()).select_from(ClientLocationObservationRow))==0


def seed_owner_message(session, *, at=NOW+timedelta(seconds=6),
                       basis="REPRESENTED_OWNER", source="wwebjs",
                       direction="OUTBOUND"):
    thread = ConversationThreadRow(
        id="cth_synthetic_history",
        tenant_id=TENANT_ID,
        source=source,
        source_account="synthetic",
        external_thread_key="wwebjs:synthetic-peer@lid",
        thread_type="DIRECT",
        title=None,
        first_message_at=at,
        last_message_at=at,
        created_at=at,
        updated_at=at,
    )
    msg = ConversationMessageRow(
        id="cmsg_synthetic_history",
        tenant_id=TENANT_ID,
        conversation_id=thread.id,
        source=source,
        source_account="synthetic",
        source_message_id="synthetic_msg_1",
        sender_actor_id=None,
        external_sender_key=None,
        direction=direction,
        from_me=direction=="OUTBOUND",
        sent_at=at,
        message_type="TEXT",
        text="Mensagem de teste, não contém dados privados.",
        normalized_text="mensagem de teste",
        content_hash=hashlib.sha256(b"synthetic").hexdigest(),
        sensitivity_class="NORMAL",
        searchable=True,
        metadata_json={
            "speaker_basis":basis,
            "capture_contract":"LIVE_CONVERSATION_ARCHIVE_V0",
            "event_origin":"OWNER_MANUAL_OUTBOUND_OBSERVED",
        },
        imported_at=at,
        created_at=at,
    )
    session.add_all([thread,msg])
    session.flush()
    return msg


def test_location_writes_preserve_history_without_changing_current_location_contract(session):
    auth, issued=issue_session(session)
    service=location_service(auth)
    first=service.put_context_observation(
        session, session_token=issued.session_token,
        observation=observation(captured_at=NOW+timedelta(seconds=3)),
        now=NOW+timedelta(seconds=4),
    )
    session.commit()
    next_fix=service.put_context_observation(
        session, session_token=issued.session_token,
        observation=observation(
            captured_at=NOW+timedelta(seconds=6),
            latitude=-3.731,longitude=-38.541,
        ),
        now=NOW+timedelta(seconds=7),
    )
    session.commit()
    assert first.location_snapshot_id==next_fix.location_snapshot_id
    assert session.scalar(select(func.count()).select_from(ClientLocationSnapshotRow))==1
    fixes=session.scalars(select(ClientLocationObservationRow).order_by(
        ClientLocationObservationRow.captured_at
    )).all()
    assert len(fixes)==2
    assert [round(x.latitude,3) for x in fixes]==[-3.730,-3.731]
    assert fixes[0].human_identity_id==HUMAN_ID
    assert fixes[0].device_id==DEVICE_ID
    assert fixes[1].tenant_id==TENANT_ID
    # Replayed same event is idempotent, not a second GPS position.
    service.put_context_observation(
        session, session_token=issued.session_token,
        observation=observation(
            captured_at=NOW+timedelta(seconds=6),
            latitude=-3.731,longitude=-38.541,
        ),
        now=NOW+timedelta(seconds=8),
    )
    session.commit()
    assert session.scalar(select(func.count()).select_from(ClientLocationObservationRow))==2


def test_message_is_linked_to_nearest_measured_phone_position_not_realtime(session):
    auth, issued=issue_session(session)
    service=location_service(auth)
    message=seed_owner_message(session)
    first=observation(captured_at=NOW+timedelta(seconds=2))
    second=observation(
        captured_at=NOW+timedelta(seconds=7),
        latitude=-3.731,longitude=-38.541,
    )
    service.put_context_observation(
        session,session_token=issued.session_token,
        observation=first,now=NOW+timedelta(seconds=3),
    )
    service.put_context_observation(
        session,session_token=issued.session_token,
        observation=second,now=NOW+timedelta(seconds=8),
    )
    session.commit()
    result=service.get_message_context(
        session,session_token=issued.session_token,
        message_id=message.id, now=NOW+timedelta(seconds=9),
    )
    assert result.status=="DEVICE_POSITION_NEAR_MESSAGE"
    assert result.message_at==message.sent_at
    assert result.delta_seconds==1.0
    assert result.latitude==-3.731
    assert result.longitude==-38.541
    assert result.accuracy_m==12.5
    assert result.precision=="PRECISE"
    assert result.location_device_id==DEVICE_ID
    assert result.evidence_basis=="ANDROID_DEVICE_POSITION_NEAR_WHATSAPP_MESSAGE_TIME"


def test_no_historical_fix_means_no_invented_position(session):
    auth,issued=issue_session(session)
    message=seed_owner_message(session,at=NOW+timedelta(days=2))
    service=location_service(auth)
    service.put_context_observation(
        session,session_token=issued.session_token,
        observation=observation(),now=NOW+timedelta(seconds=4),
    )
    session.commit()
    r=service.get_message_context(
        session,session_token=issued.session_token,
        message_id=message.id,now=NOW+timedelta(seconds=5),
    )
    assert r.status=="NO_TIME_MATCHED_LOCATION"
    assert r.latitude is None and r.longitude is None
    assert r.location_observation_id is None


@pytest.mark.parametrize("basis,direction,source",[
    ("UNRESOLVED","OUTBOUND","wwebjs"),
    ("EXTERNAL_SENDER","INBOUND","wwebjs"),
    ("REPRESENTED_OWNER","OUTBOUND","gmail"),
])
def test_nonowner_or_unresolved_message_does_not_infer_person(basis,direction,source,session):
    auth,issued=issue_session(session)
    message=seed_owner_message(session,basis=basis,direction=direction,source=source)
    s=location_service(auth)
    s.put_context_observation(
        session,session_token=issued.session_token,
        observation=observation(captured_at=NOW+timedelta(seconds=6)),
        now=NOW+timedelta(seconds=7),
    )
    session.commit()
    r=s.get_message_context(session,session_token=issued.session_token,
                            message_id=message.id,now=NOW+timedelta(seconds=8))
    assert r.status=="AUTHORSHIP_NOT_PROVEN"
    assert r.latitude is None and r.longitude is None


def test_imprecise_fix_cannot_claim_exact_position(session):
    auth, issued=issue_session(session)
    msg=seed_owner_message(session)
    from attention_router.core.client.location import (
        ClientLocationPrecision, CurrentLocationObservation
    )
    fix=CurrentLocationObservation(
        latitude=-3.731,longitude=-38.542,
        accuracy_m=800,precision=ClientLocationPrecision.APPROXIMATE,
        captured_at=NOW+timedelta(seconds=7),
    )
    service=location_service(auth)
    service.put_context_observation(session,session_token=issued.session_token,
                        observation=fix,now=NOW+timedelta(seconds=8))
    session.commit()
    r=service.get_message_context(session,session_token=issued.session_token,
                                  message_id=msg.id,now=NOW+timedelta(seconds=9))
    assert r.status=="LOCATION_TOO_IMPRECISE"
    assert r.accuracy_m==800
    assert r.latitude is None and r.longitude is None


def test_two_devices_with_simultaneous_fixes_are_ambiguous(session):
    auth,issued=issue_session(session)
    msg=seed_owner_message(session)
    service=location_service(auth)
    service.put_context_observation(session,session_token=issued.session_token,
                        observation=observation(captured_at=NOW+timedelta(seconds=6)),
                        now=NOW+timedelta(seconds=7))
    other_id="cdev_"+"x"*24
    spki=b"synthetic-second-device-public-key"
    session.add(ClientDeviceRow(
        id=other_id,human_identity_id=HUMAN_ID,
        public_key_fingerprint="sha256:"+hashlib.sha256(spki).hexdigest(),
        public_key_spki=spki,canonical_name="Synthetic secondary Android",
        platform="ANDROID",roles=["CAPABILITY_NODE","CLIENT"],
        status="ACTIVE",created_at=NOW,updated_at=NOW,
    ))
    session.add(ClientLocationObservationRow(
        id="clh_other_device",tenant_id=TENANT_ID,
        human_identity_id=HUMAN_ID,device_id=other_id,
        latitude=-4.0,longitude=-39.0,accuracy_m=5,
        precision="PRECISE",captured_at=NOW+timedelta(seconds=7),
        received_at=NOW+timedelta(seconds=8),
    ))
    session.commit()
    r=service.get_message_context(session,session_token=issued.session_token,
                                  message_id=msg.id,now=NOW+timedelta(seconds=9))
    assert r.status=="AMBIGUOUS_DEVICES"
    assert r.latitude is None and r.longitude is None


def test_nonowner_membership_denied_without_exposing_position(session):
    auth,issued=issue_session(session)
    msg=seed_owner_message(session)
    owner=session.scalar(select(ClientTenantMembershipRow))
    owner.role="MEMBER"
    session.commit()
    with pytest.raises(ClientLocationAuthorityRejected):
        location_service(auth).get_message_context(
            session,session_token=issued.session_token,
            message_id=msg.id,now=NOW+timedelta(seconds=7),
        )


def test_distinct_timestamp_same_device_conflicting_coordinates_are_rejected(session):
    auth,issued=issue_session(session)
    service=location_service(auth)
    service.put_context_observation(
        session,session_token=issued.session_token,
        observation=observation(),now=NOW+timedelta(seconds=4),
    )
    session.commit()
    with pytest.raises(ClientLocationUnavailable):
        service.put_context_observation(
            session,session_token=issued.session_token,
            observation=observation(latitude=-3.9),
            now=NOW+timedelta(seconds=5),
        )
    session.rollback()
    assert session.scalar(select(func.count()).select_from(ClientLocationObservationRow))==1
