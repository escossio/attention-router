"""Original-message canary contracts on the disposable migrated PostgreSQL DB."""
import pytest

from tests import test_semantic_canary_scope as acceptance

pytestmark = pytest.mark.postgres


def test_original_evidence_review_and_revocation(Session, tmp_path):
    with Session() as session:
        acceptance.test_original_message_candidate_review_query_revoke_no_mirror(session, tmp_path)


def test_quarantine_before_commit_without_action_queue(Session, tmp_path, monkeypatch):
    with Session() as session:
        acceptance.test_canary_capture_is_quarantined_before_commit_without_action_queue(session, tmp_path, monkeypatch)


@pytest.mark.parametrize('case', ['restore', 'scope', 'secret', 'expired'])
def test_original_evidence_negative_boundaries(Session, tmp_path, case):
    with Session() as session:
        acceptance.test_original_canary_negative_boundaries(session, tmp_path, case)
