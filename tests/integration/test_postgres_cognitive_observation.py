import pytest

from test_cognitive_observation import _exercise


@pytest.mark.postgres
def test_postgres_attachment_context(Session, monkeypatch):
    with Session() as session:
        _exercise(session, monkeypatch)


@pytest.mark.postgres
def test_postgres_revocation_preserves_decision_and_blocks_restoration(Session, monkeypatch):
    from test_cognitive_revocation_contract import test_admission_history_survives_technical_revocation
    with Session() as session:
        test_admission_history_survives_technical_revocation(session, monkeypatch)
