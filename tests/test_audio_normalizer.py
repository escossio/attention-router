import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from attention_router.infrastructure.audio_normalizer import (
    AudioNormalizationError,
    normalize_voice_for_whatsapp,
)


def valid_mp3() -> bytes:
    result = subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.25",
            "-ac", "1", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "64k",
            "-f", "mp3", "pipe:1",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout


def probe_bytes(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return json.loads(result.stdout)


def test_valid_mp3_normalizes_to_canonical_voice_contract():
    source = valid_mp3()
    normalized = normalize_voice_for_whatsapp(source, "audio/mpeg")
    assert normalized.mime_type == "audio/ogg"
    assert normalized.data.startswith(b"OggS")
    with tempfile.NamedTemporaryFile(suffix=".ogg") as output:
        output.write(normalized.data)
        output.flush()
        probe = probe_bytes(Path(output.name))
    assert probe["format"]["format_name"] == "ogg"
    assert len(probe["streams"]) == 1
    stream = probe["streams"][0]
    assert stream["codec_name"] == "opus"
    assert stream["sample_rate"] == "48000"
    assert stream["channels"] == 1
    assert stream["codec_type"] == "audio"
    assert normalized.metadata["container"] == "ogg"
    assert normalized.metadata["codec"] == "opus"


def test_corrupt_input_fails_closed():
    with pytest.raises(AudioNormalizationError, match="TRANSCODE_FAILED|PROBE_FAILED"):
        normalize_voice_for_whatsapp(b"not-an-audio-file", "audio/mpeg")


def test_unsupported_mime_fails_closed():
    with pytest.raises(AudioNormalizationError, match="INPUT_MIME_UNSUPPORTED"):
        normalize_voice_for_whatsapp(b"bytes", "audio/wav")


def test_missing_ffmpeg_is_explicit():
    with pytest.raises(AudioNormalizationError, match="FFMPEG_MISSING"):
        normalize_voice_for_whatsapp(b"bytes", "audio/mpeg", ffmpeg_path="/missing/ffmpeg")


def test_timeout_is_fail_closed():
    with pytest.raises(AudioNormalizationError, match="TIMEOUT"):
        normalize_voice_for_whatsapp(valid_mp3(), "audio/mpeg", timeout_seconds=0.000001)


def test_source_bytes_unchanged_and_temp_files_cleaned():
    source = valid_mp3()
    original = source
    before = set(Path(tempfile.gettempdir()).glob("andy-voice-normalize-*"))
    normalize_voice_for_whatsapp(source, "audio/mpeg")
    after = set(Path(tempfile.gettempdir()).glob("andy-voice-normalize-*"))
    assert source == original
    assert after == before


def test_repeated_normalization_has_same_structural_contract():
    source = valid_mp3()
    first = normalize_voice_for_whatsapp(source, "audio/mpeg")
    second = normalize_voice_for_whatsapp(source, "audio/mpeg")
    assert first.mime_type == second.mime_type == "audio/ogg"
    assert first.metadata["codec"] == second.metadata["codec"] == "opus"
    assert first.metadata["sample_rate"] == second.metadata["sample_rate"] == 48000
    assert first.metadata["channels"] == second.metadata["channels"] == 1
