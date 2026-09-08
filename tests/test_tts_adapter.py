from pathlib import Path
from unittest.mock import patch

from attention_router.adapters.tts import DisabledTTSProvider, SwitcherTTSClient, TTSClientError
from attention_router.infrastructure.models import OutboxMessageRow


MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x21" + b"\xff\xfb" + b"\x00" * 64


class FakeResponse:
    headers = {"content-type": "audio/mpeg", "x-request-id": "req-1", "x-tts-provider": "xai"}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, *_):
        return MP3


def test_disabled_tts_does_not_call_network(tmp_path):
    provider = DisabledTTSProvider()
    with patch("attention_router.adapters.tts.request.urlopen") as urlopen:
        result = provider.synthesize("Oi", tmp_path / "x.mp3")
    assert result is None
    urlopen.assert_not_called()


def test_switcher_client_sends_only_andy_profile(tmp_path):
    captured = {}

    def fake_urlopen(req, timeout):
        captured["body"] = req.data.decode()
        captured["timeout"] = timeout
        captured["url"] = req.full_url
        return FakeResponse()

    client = SwitcherTTSClient(base_url="http://tts-api-switcher:8090/internal/tts", token="secret", timeout_seconds=3)
    with patch("attention_router.adapters.tts.request.urlopen", side_effect=fake_urlopen):
        result = client.synthesize("Oi", tmp_path / "andy.mp3", language="pt-BR")
    assert result.profile == "andy"
    assert result.provider == "xai"
    assert Path(result.output_path).read_bytes().startswith(b"ID3")
    assert '"profile":"andy"' in captured["body"]
    assert '"language":"pt-BR"' in captured["body"]
    assert "eve" not in captured["body"]
    assert captured["timeout"] == 3


def test_switcher_client_handles_invalid_audio_and_timeout(tmp_path):
    class BadResponse(FakeResponse):
        headers = {"content-type": "text/html"}

        def read(self, *_):
            return b"<html>bad</html>"

    client = SwitcherTTSClient(base_url="http://tts", token="secret")
    with patch("attention_router.adapters.tts.request.urlopen", return_value=BadResponse()):
        try:
            client.synthesize("Oi", tmp_path / "bad.mp3")
            raise AssertionError("expected invalid audio")
        except TTSClientError as exc:
            assert str(exc) == "tts_invalid_content_type"

    with patch("attention_router.adapters.tts.request.urlopen", side_effect=TimeoutError()):
        try:
            client.synthesize("Oi", tmp_path / "timeout.mp3")
            raise AssertionError("expected timeout")
        except TTSClientError as exc:
            assert str(exc) == "tts_timeout"


def test_tts_failure_does_not_alter_outbox(session):
    before = session.query(OutboxMessageRow).count()
    client = SwitcherTTSClient(base_url="http://tts", token="secret")
    with patch("attention_router.adapters.tts.request.urlopen", side_effect=TimeoutError()):
        try:
            client.synthesize("Oi", Path("/tmp/andy.mp3"))
        except TTSClientError:
            pass
    assert session.query(OutboxMessageRow).count() == before


def test_attention_router_does_not_contain_xai_secret_or_voice_id():
    root = Path(__file__).resolve().parents[1]
    text = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in [*root.glob("attention_router/**/*.py"), *root.glob("scripts/*.py")])
    assert "XAI" + "_API_KEY" not in text
    assert "voice" + "_id" not in text.lower()
    assert '"' + "ev" + "e" + '"' not in text
