from experiments.incremental_memory.memory import interpret, present


def test_vocabulary_is_not_fixture_specific():
    assert interpret("Qual é a cor do veículo de Marina?") == ("marina", "vehicle.color")
    assert interpret("De que cor é o carro de João?") == ("joao", "vehicle.color")
    assert interpret("Onde Selma trabalha?") == ("selma", "employment.company")
    assert interpret("Qual é a idade de Roberto?") is None


def test_empty_and_ambiguous_are_not_answers():
    assert present([], "trace")["status"] == "INSUFFICIENT_EVIDENCE"
    row = dict(
        evidence_id="e",
        information_id="i",
        revision=1,
        excerpt="source",
        origin="synthetic",
        recorded_at="now",
        subject="Nina",
        attribute="vehicle.color",
        value="verde",
    )
    assert present([row, row], "trace")["status"] == "AMBIGUOUS"
    answer = present([row], "trace")
    assert answer["answer"] == "O carro de Nina é verde."
    assert answer["evidence"][0]["revision"] == 1
