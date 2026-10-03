from __future__ import annotations

import pytest

from attention_router.config import Settings


def _settings(**overrides) -> Settings:
    values = {
        "app_env": "test",
        "admin_auth_enabled": False,
        "internal_ingress_hmac_secret": "x" * 32,
        "client_session_enabled": True,
        "client_command_enabled": True,
        "stt_enabled": True,
        "stt_internal_token": "synthetic-stt-token",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_voice_command_requires_text_command_channel():
    with pytest.raises(
        ValueError,
        match="CLIENT_COMMAND_VOICE_ENABLED requires CLIENT_COMMAND_ENABLED=true",
    ):
        _settings(
            client_command_enabled=False,
            client_command_voice_enabled=True,
        )


def test_voice_command_requires_stt():
    with pytest.raises(
        ValueError,
        match="CLIENT_COMMAND_VOICE_ENABLED requires STT_ENABLED=true",
    ):
        _settings(
            client_command_voice_enabled=True,
            stt_enabled=False,
        )


@pytest.mark.parametrize("max_bytes", [0, 5 * 1024 * 1024 + 1])
def test_voice_command_max_bytes_is_hard_bounded(max_bytes):
    with pytest.raises(
        ValueError,
        match="CLIENT_COMMAND_VOICE_MAX_BYTES must be between 1 and 5242880",
    ):
        _settings(client_command_voice_max_bytes=max_bytes)


def test_voice_command_valid_governed_configuration():
    settings = _settings(
        client_command_voice_enabled=True,
        client_command_voice_max_bytes=1024 * 1024,
    )

    assert settings.client_command_voice_enabled is True
    assert settings.client_command_voice_max_bytes == 1024 * 1024
