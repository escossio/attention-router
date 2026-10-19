"""Durable presentation lifecycle and races, only on disposable distributed PG."""

from concurrent.futures import ThreadPoolExecutor
import os
import subprocess
import sys
from threading import Barrier
import uuid

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DatabaseSession

from attention_router.application import first_contact as fc
from attention_router.infrastructure.first_contact_models import (
    FirstContactAttemptRow, FirstContactPresentationRow,
)
from tests.test_first_contact import (
    denied_generated, event, queued, scope, signed_proof,
    test_accounts_and_tenants_have_separate_presentation_state as test_accounts_and_tenants_have_separate_presentation_state,
    test_ack_monotonicity_preserves_confirming_evidence as test_ack_monotonicity_preserves_confirming_evidence,
    test_authenticated_ack_confirms_once_and_survives_new_input as test_authenticated_ack_confirms_once_and_survives_new_input,
    test_authenticated_but_wrong_effect_proof_is_rejected as test_authenticated_but_wrong_effect_proof_is_rejected,
    test_authenticated_transport_acceptance_is_not_device_confirmation as test_authenticated_transport_acceptance_is_not_device_confirmation,
    test_bound_effect_rejects_changed_snapshot_or_correlation as test_bound_effect_rejects_changed_snapshot_or_correlation,
    test_claim_is_explicit_and_done_does_not_confirm_delivery as test_claim_is_explicit_and_done_does_not_confirm_delivery,
    test_committed_no_dispatch_denial_recovers_on_newer_event as test_committed_no_dispatch_denial_recovers_on_newer_event,
    test_confirmation_constraint_rejects_null_required_evidence as test_confirmation_constraint_rejects_null_required_evidence,
    test_database_rejects_confirmation_without_ack_or_effect as test_database_rejects_confirmation_without_ack_or_effect,
    test_generated_and_queued_are_not_confirmed_and_bind_is_idempotent as test_generated_and_queued_are_not_confirmed_and_bind_is_idempotent,
    test_generated_text_and_decision_must_match_reservation as test_generated_text_and_decision_must_match_reservation,
    test_forged_review_authorization_cannot_bind_first_contact as test_forged_review_authorization_cannot_bind_first_contact,
    test_generation_failure_releases_only_new_entry_and_preserves_attempt as test_generation_failure_releases_only_new_entry_and_preserves_attempt,
    test_ineligible_or_cross_scope_event_cannot_reserve as test_ineligible_or_cross_scope_event_cannot_reserve,
    test_linked_effect_revalidates_original_scope_and_decision_authority as test_linked_effect_revalidates_original_scope_and_decision_authority,
    test_late_confirmation_keeps_original_identity_after_new_binding as test_late_confirmation_keeps_original_identity_after_new_binding,
    test_no_dispatch_recovery_is_strictly_newer_and_identity_scoped as test_no_dispatch_recovery_is_strictly_newer_and_identity_scoped,
    test_no_dispatch_recovery_requires_complete_negative_evidence as test_no_dispatch_recovery_requires_complete_negative_evidence,
    test_proven_failure_before_effect_allows_only_next_new_entry as test_proven_failure_before_effect_allows_only_next_new_entry,
    test_reference_cannot_change_after_authenticated_sent_receipt as test_reference_cannot_change_after_authenticated_sent_receipt,
    test_reserve_requires_new_event_and_never_releases_by_age as test_reserve_requires_new_event_and_never_releases_by_age,
    test_review_authorized_original_snapshot_accepts_late_ack_after_revocation as test_review_authorized_original_snapshot_accepts_late_ack_after_revocation,
    test_review_cannot_bind_edited_response_as_original_first_contact as test_review_cannot_bind_edited_response_as_original_first_contact,
    test_scope_uniqueness_does_not_include_presentation_version as test_scope_uniqueness_does_not_include_presentation_version,
    test_service_mutations_remain_in_callers_transaction as test_service_mutations_remain_in_callers_transaction,
    test_uncertain_effect_never_releases_reservation as test_uncertain_effect_never_releases_reservation,
    test_unknown_signed_snapshot_never_confirms_or_records_transport_acceptance as test_unknown_signed_snapshot_never_confirms_or_records_transport_acceptance,
    test_unverified_object_cannot_confirm_receipt as test_unverified_object_cannot_confirm_receipt,
    test_verified_phone_and_lid_aliases_share_state_without_inheriting_binding as test_verified_phone_and_lid_aliases_share_state_without_inheriting_binding,
)


pytestmark = pytest.mark.postgres


@pytest.fixture()
def session(Session):
    with Session() as db:
        yield db


def test_uncommitted_reservation_fails_fast_and_replay_never_receives_token(Session, session):
    receipt = event(session)
    session.commit()
    with Session() as first, Session() as second:
        reserved = fc.reserve(first, scope(receipt), receipt.id)
        second.execute(text("SET LOCAL statement_timeout='1000ms'"))
        assert fc.reserve(second, scope(receipt), receipt.id) == fc.ReservationResult("BUSY")
        second.rollback()
        first.commit()
        assert reserved.status == "RESERVED"
        assert fc.reserve(second, scope(receipt), receipt.id) == fc.ReservationResult("REPLAY")


def test_simultaneous_new_entries_have_exactly_one_presentation_owner(Session, session):
    receipts = [event(session, offset=n) for n in range(2)]
    event_ids = [receipt.id for receipt in receipts]
    target = scope(receipts[0])
    session.commit()
    barrier = Barrier(2)

    def claim(event_id):
        with Session() as db:
            db.execute(text("SET LOCAL statement_timeout='2000ms'"))
            barrier.wait(timeout=5)
            result = fc.reserve(db, target, event_id)
            db.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, event_ids))
    assert sorted(result.status for result in results) == ["BUSY", "RESERVED"]
    assert sum(result.token is not None for result in results) == 1
    assert session.scalar(select(func.count()).select_from(FirstContactPresentationRow)) == 1
    assert session.scalar(select(func.count()).select_from(FirstContactAttemptRow)) == 1


def test_simultaneous_recovery_has_one_new_generation(Session, session):
    first, _, _, _ = denied_generated(session)
    receipts = [event(session, offset=10 + index) for index in range(2)]
    ids = [receipt.id for receipt in receipts]
    target = scope(first)
    session.commit()
    barrier = Barrier(2)

    def claim(event_id):
        with Session() as db:
            db.execute(text("SET LOCAL statement_timeout='2000ms'"))
            barrier.wait(timeout=5)
            result = fc.reserve(db, target, event_id)
            db.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ids))
    assert sorted(result.status for result in results) == ["BUSY", "RESERVED"]
    assert session.scalar(select(func.count()).select_from(FirstContactPresentationRow)) == 1
    assert session.scalar(select(func.count()).select_from(FirstContactAttemptRow)) == 2
    assert session.scalar(select(FirstContactPresentationRow)).generation == 2


def test_committed_ambiguous_reservation_survives_restart_and_accepts_late_proof(Session, session):
    case = queued(session)
    case.outbox.status, case.outbox.attempt_count = "PROCESSING", 1
    session.flush()
    fc.mark_sending(session, case.outbox.id)
    session.commit()
    case.outbox.status = "RECONCILIATION_REQUIRED"
    session.flush()
    fc.observe_outbox(session, case.outbox.id)
    session.commit()
    proof = signed_proof(session, case, ack=2)
    with Session() as restarted:
        assert restarted.get(FirstContactPresentationRow, case.token.scope_id).state == "AMBIGUOUS"
        assert fc.confirm_delivery(restarted, case.outbox.id, proof)
        restarted.commit()
    session.expire_all()
    assert session.get(FirstContactPresentationRow, case.token.scope_id).state == "CONFIRMED"


def test_database_rejects_pointer_to_attempt_from_different_scope(session):
    first = event(session)
    first_token = fc.reserve(session, scope(first), first.id).token
    session.commit()
    second = event(session, account="separate-account")
    second_token = fc.reserve(session, scope(second), second.id).token
    session.commit()
    with pytest.raises(IntegrityError), session.begin_nested():
        parent = session.get(FirstContactPresentationRow, first_token.scope_id)
        parent.current_attempt_id = second_token.attempt_id
        session.flush()


@pytest.fixture
def migration_db(pg_url):
    name = "first_contact_migration_" + uuid.uuid4().hex[:12]
    admin = create_engine(pg_url.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = pg_url.rsplit("/", 1)[0] + "/" + name
    engine = create_engine(url)

    def migrate(direction, target, check=True):
        return subprocess.run(
            [sys.executable, "-m", "alembic", direction, target],
            env={**os.environ, "DATABASE_URL": url}, check=check, capture_output=True, text=True,
        )

    try:
        migrate("upgrade", "0059_maestro_relay_receipts")
        yield engine, migrate
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


def test_migration_preserves_predecessor_and_roundtrips_empty_schema(migration_db):
    engine, migrate = migration_db
    migrate("upgrade", "head")
    with engine.connect() as connection:
        schema = inspect(connection)
        assert "maestro_relay_receipts" in schema.get_table_names()
        assert {"first_contact_presentations", "first_contact_attempts"}.issubset(schema.get_table_names())
        unique = {item["name"]: item["column_names"] for item in schema.get_unique_constraints(
            "first_contact_presentations",
        )}
        assert unique["uq_first_contact_scope"] == [
            "tenant_id", "source", "channel", "source_account", "canonical_peer_hash",
        ]
        foreign = {item["name"]: item for item in schema.get_foreign_keys("first_contact_presentations")}
        assert foreign["fk_first_contact_current_attempt_scope"]["constrained_columns"] == ["id", "current_attempt_id"]
    migrate("downgrade", "0059_maestro_relay_receipts")
    with engine.connect() as connection:
        assert "first_contact_presentations" not in inspect(connection).get_table_names()
        assert "maestro_relay_receipts" in inspect(connection).get_table_names()
    migrate("upgrade", "head")


def test_migration_refuses_to_destroy_committed_presentation_history(migration_db):
    engine, migrate = migration_db
    migrate("upgrade", "head")
    with DatabaseSession(engine) as db:
        receipt = event(db)
        fc.reserve(db, scope(receipt), receipt.id)
        db.commit()
    outcome = migrate("downgrade", "0059_maestro_relay_receipts", check=False)
    assert outcome.returncode != 0
    assert "FIRST_CONTACT_DOWNGRADE_REQUIRES_DATA_EXPORT" in outcome.stderr
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0060_first_contact_v1"
        assert connection.scalar(text("SELECT count(*) FROM first_contact_presentations")) == 1
        assert connection.scalar(text("SELECT count(*) FROM first_contact_attempts")) == 1
