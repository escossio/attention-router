"""Disposable PostgreSQL contract of temporal phone location evidence.

These tests never contact real location providers and do not use real positions.
"""

from datetime import timedelta
import hashlib

import pytest
from sqlalchemy import func, inspect, select, text

from attention_router.core.client.location import (
    ClientLocationPrecision, CurrentLocationObservation,
)
from attention_router.infrastructure.client_location_history_models import (
    ClientLocationObservationRow,
)
from attention_router.infrastructure.client_location_models import (
    ClientLocationSnapshotRow,
)
from attention_router.infrastructure.models import (
    ConversationThreadRow, ConversationMessageRow,
)
from tests.integration.test_postgres_client_location import (
    NOW, TENANT_ID,
    issue_session, keypair, session_service, settings as old_settings, seed,
)
from attention_router.application.client_location import ClientLocationService


def location_service():
    return ClientLocationService(
        settings=old_settings().model_copy(
            update={"client_location_history_enabled":True}
        ),
        session_service=session_service(),
    )


pytestmark = pytest.mark.postgres


def _insert_message(session, *, at):
    session.add(ConversationThreadRow(
        id="cth_pg_location",tenant_id=TENANT_ID,
        source="wwebjs",source_account="synthetic",
        external_thread_key="wwebjs:synthetic@lid",thread_type="DIRECT",
        first_message_at=at,last_message_at=at,created_at=at,updated_at=at,
    ))
    session.flush()
    session.add(ConversationMessageRow(
        id="cmsg_pg_location",tenant_id=TENANT_ID,
        conversation_id="cth_pg_location",source="wwebjs",
        source_account="synthetic",source_message_id="pg-msg-synthetic",
        direction="OUTBOUND",from_me=True,sent_at=at,message_type="TEXT",
        text="texto sintético de contexto temporal",
        normalized_text="texto sintético",content_hash=hashlib.sha256(b"pg-msg").hexdigest(),
        sensitivity_class="NORMAL",searchable=True,
        metadata_json={"speaker_basis":"REPRESENTED_OWNER",
                       "capture_contract":"LIVE_CONVERSATION_ARCHIVE_V0"},
        imported_at=at,created_at=at,
    ))


def test_pg_location_history_schema_append_only_and_owner_evidence(Session):
    with Session() as s:
        schema=inspect(s.bind)
        assert "client_location_observations" in schema.get_table_names()
        cols={x["name"] for x in schema.get_columns("client_location_observations")}
        assert cols=={
            "id","tenant_id","human_identity_id","device_id",
            "latitude","longitude","accuracy_m","precision","captured_at","received_at",
        }
        assert all(x not in cols for x in [
            "session_token","password","address","speed","bearing","altitude",
        ])
        unique={x["name"] for x in schema.get_unique_constraints(
            "client_location_observations"
        )}
        assert "uq_client_location_observation_device_time" in unique
        assert s.scalar(text("SELECT version_num FROM alembic_version"))==(
            "0062_client_location_observations"
        )


def test_pg_repeated_location_fixes_preserved_and_nearest_join_proved(Session):
    private, spki, public=keypair()
    seed(Session,spki)
    issued=issue_session(Session,private,public)
    moment=NOW+timedelta(seconds=6)
    with Session.begin() as s:
        _insert_message(s,at=moment)

    with Session.begin() as s:
        location_service().put_context_observation(
            s,session_token=issued.session_token,
            observation=CurrentLocationObservation(
                latitude=-3.730,longitude=-38.54,accuracy_m=15.0,
                captured_at=NOW+timedelta(seconds=3),
                precision=ClientLocationPrecision.PRECISE,
            ),
            now=NOW+timedelta(seconds=4),
        )
    with Session.begin() as s:
        location_service().put_context_observation(
            s,session_token=issued.session_token,
            observation=CurrentLocationObservation(
                latitude=-3.731,longitude=-38.54,accuracy_m=15.0,
                captured_at=NOW+timedelta(seconds=7),
                precision=ClientLocationPrecision.PRECISE,
            ),
            now=NOW+timedelta(seconds=8),
        )
    with Session() as s:
        assert s.scalar(select(func.count()).select_from(ClientLocationSnapshotRow))==1
        assert s.scalar(select(func.count()).select_from(ClientLocationObservationRow))==2
        r=location_service().get_message_context(
            s,session_token=issued.session_token,message_id="cmsg_pg_location",
            now=NOW+timedelta(seconds=10),
        )
        assert r.status=="DEVICE_POSITION_NEAR_MESSAGE"
        assert r.evidence_basis=="ANDROID_DEVICE_POSITION_NEAR_WHATSAPP_MESSAGE_TIME"
        assert r.latitude==-3.731
        assert r.delta_seconds==1.0
        m=s.scalar(select(ConversationMessageRow))
        assert m.text=="texto sintético de contexto temporal"
        assert "location" not in m.metadata_json
        assert s.scalar(select(func.count()).select_from(ConversationThreadRow))==1
        assert len(s.scalars(select(ClientLocationObservationRow)).all())==2
