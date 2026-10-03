from attention_router.config import Settings


def test_whatsapp_history_settings_are_safe_by_default():
    configured = Settings(_env_file=None)
    assert configured.whatsapp_history_read_url == (
        "http://127.0.0.1:18103/internal/history/chats"
    )
    assert configured.whatsapp_history_hmac_secret is None
    assert configured.whatsapp_history_timeout_seconds == 10.0
    assert configured.whatsapp_history_max_response_bytes == 4 * 1024 * 1024
