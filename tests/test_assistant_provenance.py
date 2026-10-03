from attention_router.application.assistant_provenance import (
    ensure_introduction,
    label_automated_text,
)


def test_automated_text_is_always_labeled():
    assert label_automated_text("Olá.") == "[Andy] Olá."
    assert label_automated_text("[Andy] Olá.") == "[Andy] Olá."


def test_first_contact_introduces_andy_with_configured_owner_name():
    text, included = ensure_introduction(
        "Como posso ajudar?",
        introduced=False,
        represented_reference_name="Leonardo",
    )
    assert included is True
    assert text == (
        "Eu sou a Andy, assistente virtual de Leonardo. Como posso ajudar?"
    )


def test_follow_up_does_not_repeat_introduction():
    text, included = ensure_introduction(
        "Como posso ajudar?",
        introduced=True,
        represented_reference_name="Leonardo",
    )
    assert included is False
    assert text == "Como posso ajudar?"


def test_missing_owner_name_uses_neutral_identity():
    text, included = ensure_introduction(
        "Como posso ajudar?",
        introduced=False,
        represented_reference_name=None,
    )
    assert included is True
    assert "titular da conta" in text
