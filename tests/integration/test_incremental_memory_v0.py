"""Distributed PostgreSQL tests, isolated schema in the suite's disposable DB."""

import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from experiments.incremental_memory import memory

pytestmark = pytest.mark.postgres


def test_persist_correction_abstention_and_conflict(pg_url, monkeypatch):
    url = pg_url.replace("postgresql+psycopg://", "postgresql://")
    schema = "memory_v0_" + uuid.uuid4().hex
    with psycopg.connect(url) as db:
        db.execute("CREATE SCHEMA " + schema)

    def connect(write=False):
        return psycopg.connect(url, options="-c search_path=" + schema, row_factory=dict_row)

    monkeypatch.setattr(memory, "connect", connect)
    try:
        sql = (Path(__file__).parents[2] / "experiments/incremental_memory/schema.sql").read_text()
        # Application-role ACLs are checked separately by the offline container
        # acceptance scenario. Do not alter cluster roles on shared CI workers.
        with connect(True) as db:
            db.execute(sql.split("GRANT CONNECT")[0])
        registered = memory.register("Nina", "vehicle.color", "verde", "Nina possui veículo verde.")
        first = memory.answer("Qual é a cor do carro de Nina?")
        assert first["answer"] == "O carro de Nina é verde."
        assert first["evidence"][0]["evidence_id"] == registered["evidence_id"]
        memory.register("Nina", "vehicle.color", "prata", "Correção: veículo prata.", expected=1)
        assert (
            memory.answer("De que cor é o veículo de Nina?")["answer"] == "O carro de Nina é prata."
        )
        with pytest.raises(ValueError, match="REVISION_CONFLICT"):
            memory.register("Nina", "vehicle.color", "preto", "Stale", expected=1)
        assert [
            (r["value"], r["current"]) for r in memory.retrieve("Nina", "vehicle.color", True)
        ] == [("verde", False), ("prata", True)]
        assert memory.answer("Onde Nina trabalha?")["status"] == "INSUFFICIENT_EVIDENCE"
        # Induced failure after evidence insertion must roll the entire transaction back.
        with connect() as db:
            count = db.execute("SELECT count(*) AS n FROM evidence").fetchone()["n"]
        with pytest.raises(psycopg.Error):
            with connect(True) as db:
                db.execute(
                    "INSERT INTO evidence(id,excerpt,origin) VALUES(%s,'rollback','synthetic')",
                    (uuid.uuid4(),),
                )
                db.execute("UPDATE information SET current_revision=999")
        with connect() as db:
            assert db.execute("SELECT count(*) AS n FROM evidence").fetchone()["n"] == count
        assert memory.answer("Qual a cor do carro de Nina?")["answer"] == "O carro de Nina é prata."
    finally:
        with psycopg.connect(url) as db:
            db.execute("DROP SCHEMA " + schema + " CASCADE")
