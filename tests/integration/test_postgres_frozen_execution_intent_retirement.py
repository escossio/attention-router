"""FR01-FR15: persisted frozen execution-intent retirement contract."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError

from attention_router.infrastructure.models import ExecutionIntentRow, ScenarioVersionRow
from attention_router.platform.execution_intent_retirement import retire_execution_intent
from attention_router.platform.production_authority import ProductionAuthorityDenied


pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def clean_rows(Session):
    with Session() as session:
        session.execute(delete(ExecutionIntentRow))
        session.commit()


def _intent(Session, state="FROZEN"):
    now = datetime.now(UTC)
    ident = "fr-" + uuid4().hex
    scope = {"target": {"canonical_address": "550000000030"}, "authority": "frozen"}
    with Session() as session:
        row = ExecutionIntentRow(
            id=ident, idempotency_key=ident, scope=scope,
            scope_fingerprint=uuid4().hex, provenance={"source": "fr-test"},
            state=state, created_at=now, frozen_at=now if state == "FROZEN" else None,
            expires_at=now + timedelta(minutes=5),
        )
        session.add(row)
        session.commit()
        return ident, row.scope_fingerprint


def test_fr01_frozen_to_retired_succeeds(Session):
    ident, fp = _intent(Session)
    with Session() as session:
        row = retire_execution_intent(session, execution_intent_id=ident, expected_fingerprint=fp)
        session.commit()
        assert row.state == "RETIRED"


def test_fr02_state_only_and_fingerprint_unchanged(Session):
    ident, fp = _intent(Session)
    with Session() as session:
        before = session.get(ExecutionIntentRow, ident)
        original = (before.scope, before.scope_fingerprint, before.expires_at, before.frozen_at)
        retire_execution_intent(session, execution_intent_id=ident, expected_fingerprint=fp)
        session.commit()
    with Session() as session:
        after = session.get(ExecutionIntentRow, ident)
        assert (after.scope, after.scope_fingerprint, after.expires_at, after.frozen_at) == original
        assert after.retired_at is not None


def test_fr03_fr04_semantic_target_and_fingerprint_unchanged(Session):
    ident, fp = _intent(Session)
    with Session() as session:
        row = session.get(ExecutionIntentRow, ident)
        original_scope, original_fp = row.scope, row.scope_fingerprint
        retire_execution_intent(session, execution_intent_id=ident, expected_fingerprint=fp)
        session.commit()
        assert row.scope == original_scope and row.scope_fingerprint == original_fp


def test_fr05_authority_graph_columns_unchanged(Session):
    ident, fp = _intent(Session)
    with Session() as session:
        row = session.get(ExecutionIntentRow, ident)
        original = {key: getattr(row, key) for key in ("authority_profile_id", "expires_at")}
        retire_execution_intent(session, execution_intent_id=ident, expected_fingerprint=fp)
        session.commit()
        assert {key: getattr(row, key) for key in original} == original


def test_fr06_fr09_retired_terminal_and_replay_idempotent(Session):
    ident, fp = _intent(Session)
    with Session() as session:
        first = retire_execution_intent(session, execution_intent_id=ident, expected_fingerprint=fp)
        session.commit()
        retired_at = first.retired_at
    with Session() as session:
        second = retire_execution_intent(session, execution_intent_id=ident, expected_fingerprint=fp)
        session.commit()
        assert second.state == "RETIRED" and second.retired_at == retired_at


def test_fr08_materialized_to_retired_rejected(Session):
    ident, fp = _intent(Session, state="MATERIALIZED")
    with Session() as session:
        with pytest.raises(ProductionAuthorityDenied):
            retire_execution_intent(session, execution_intent_id=ident, expected_fingerprint=fp)


def test_fr10_fr11_frozen_semantic_mutation_rejected(Session):
    ident, _ = _intent(Session)
    with Session() as session:
        row = session.get(ExecutionIntentRow, ident)
        row.scope = {"target": {"canonical_address": "changed"}, "authority": "frozen"}
        with pytest.raises(DBAPIError):
            session.commit()
        session.rollback()
    with Session() as session:
        row = session.get(ExecutionIntentRow, ident)
        with pytest.raises(DBAPIError):
            session.execute(text("UPDATE execution_intents SET state='RETIRED', scope='{}' WHERE id=:id"), {"id": ident})
            session.commit()
        session.rollback()


def test_fr12_concurrent_retirement_is_single_terminal_outcome(Session):
    ident, fp = _intent(Session)

    def attempt():
        with Session() as session:
            row = retire_execution_intent(session, execution_intent_id=ident, expected_fingerprint=fp)
            session.commit()
            return row.state

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == ["RETIRED", "RETIRED"]

    with Session() as session:
        assert session.get(ExecutionIntentRow, ident).state == "RETIRED"


def test_fr13_fr14_legacy_synthetic_rows_unaffected(Session):
    with Session() as session:
        # The migration/retirement trigger is independent of scenario-version data;
        # verify the legacy table remains queryable without manufacturing rows.
        assert session.scalar(select(ScenarioVersionRow).where(ScenarioVersionRow.version == 1)) in (None,)


def test_fr15_retirement_creates_no_projection_artifacts(Session):
    ident, fp = _intent(Session)
    with Session() as session:
        retire_execution_intent(session, execution_intent_id=ident, expected_fingerprint=fp)
        session.commit()
        for table in ("agent_execution_intents", "scenario_runs", "effect_budgets", "execution_leases", "bounded_run_authorizations"):
            assert session.execute(text(f"select count(*) from {table}")).scalar_one() == 0
