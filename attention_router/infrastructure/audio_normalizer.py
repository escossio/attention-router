"""Canonicalize outbound WhatsApp voice media without trusting file names."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


CANONICAL_MIME = "audio/ogg"
CANONICAL_CODEC = "opus"
CANONICAL_SAMPLE_RATE = 48000
CANONICAL_CHANNELS = 1
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0


class AudioNormalizationError(RuntimeError):
    """Raised when media cannot be converted or validated safely."""


@dataclass(frozen=True, slots=True)
class NormalizedVoice:
    data: bytes
    mime_type: str
    metadata: dict[str, object]


def _tool_path(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise AudioNormalizationError(f"AUDIO_NORMALIZER_{name.upper()}_MISSING")
    return path


def _resolve_tool(name: str, override: str | None) -> str:
    if override is not None:
        if not Path(override).is_file():
            raise AudioNormalizationError(f"AUDIO_NORMALIZER_{name.upper()}_MISSING")
        return override
    return _tool_path(name)


def _run(command: list[str], *, timeout_seconds: float) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioNormalizationError("AUDIO_NORMALIZER_TIMEOUT") from exc
    except OSError as exc:
        raise AudioNormalizationError("AUDIO_NORMALIZER_EXECUTION_FAILED") from exc


def _probe(path: Path, *, ffprobe_path: str, timeout_seconds: float) -> dict[str, object]:
    result = _run(
        [
            ffprobe_path,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        timeout_seconds=timeout_seconds,
    )
    if result.returncode != 0:
        raise AudioNormalizationError("AUDIO_NORMALIZER_PROBE_FAILED")
    try:
        value = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AudioNormalizationError("AUDIO_NORMALIZER_PROBE_INVALID") from exc
    if not isinstance(value, dict):
        raise AudioNormalizationError("AUDIO_NORMALIZER_PROBE_INVALID")
    return value


def _validate_canonical(path: Path, *, ffprobe_path: str, timeout_seconds: float) -> dict[str, object]:
    probe = _probe(path, ffprobe_path=ffprobe_path, timeout_seconds=timeout_seconds)
    streams = probe.get("streams")
    if not isinstance(streams, list) or len(streams) != 1:
        raise AudioNormalizationError("AUDIO_NORMALIZER_STREAM_CONTRACT_INVALID")
    stream = streams[0]
    if not isinstance(stream, dict) or any(
        (
            stream.get("codec_type") != "audio",
            stream.get("codec_name") != CANONICAL_CODEC,
            stream.get("sample_rate") != str(CANONICAL_SAMPLE_RATE),
            stream.get("channels") != CANONICAL_CHANNELS,
        )
    ):
        raise AudioNormalizationError("AUDIO_NORMALIZER_CODEC_CONTRACT_INVALID")
    if (probe.get("format") or {}).get("format_name") != "ogg":
        raise AudioNormalizationError("AUDIO_NORMALIZER_CONTAINER_CONTRACT_INVALID")
    return probe


def normalize_voice_for_whatsapp(
    data: bytes,
    input_mime: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    ffmpeg_path: str | None = None,
    ffprobe_path: str | None = None,
) -> NormalizedVoice:
    """Return canonical Ogg/Opus voice bytes, or fail closed.

    The current TTS contract is MP3, so normalization is intentionally always
    performed. This keeps the persisted delivery artifact canonical and avoids
    generation-dependent passthrough decisions.
    """
    if not isinstance(data, bytes) or not data or len(data) > max_bytes:
        raise AudioNormalizationError("AUDIO_NORMALIZER_INPUT_INVALID")
    if input_mime not in {"audio/mpeg", "audio/mp3", "application/octet-stream"}:
        raise AudioNormalizationError("AUDIO_NORMALIZER_INPUT_MIME_UNSUPPORTED")
    ffmpeg = _resolve_tool("ffmpeg", ffmpeg_path)
    ffprobe = _resolve_tool("ffprobe", ffprobe_path)
    with tempfile.TemporaryDirectory(prefix="andy-voice-normalize-") as directory:
        root = Path(directory)
        source = root / "source.bin"
        target = root / "normalized.ogg"
        source.write_bytes(data)
        result = _run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-vn",
                "-map_metadata",
                "-1",
                "-ac",
                str(CANONICAL_CHANNELS),
                "-ar",
                str(CANONICAL_SAMPLE_RATE),
                "-c:a",
                "libopus",
                "-b:a",
                "32k",
                "-vbr",
                "on",
                "-application",
                "voip",
                "-f",
                "ogg",
                str(target),
            ],
            timeout_seconds=timeout_seconds,
        )
        if result.returncode != 0 or not target.is_file():
            raise AudioNormalizationError("AUDIO_NORMALIZER_TRANSCODE_FAILED")
        normalized = target.read_bytes()
        if not normalized or len(normalized) > max_bytes:
            raise AudioNormalizationError("AUDIO_NORMALIZER_OUTPUT_INVALID")
        probe = _validate_canonical(target, ffprobe_path=ffprobe, timeout_seconds=timeout_seconds)
        decode = _run(
            [ffmpeg, "-hide_banner", "-v", "error", "-i", str(target), "-f", "null", "-"],
            timeout_seconds=timeout_seconds,
        )
        if decode.returncode != 0:
            raise AudioNormalizationError("AUDIO_NORMALIZER_DECODE_FAILED")
    stream = probe["streams"][0]
    return NormalizedVoice(
        data=normalized,
        mime_type=CANONICAL_MIME,
        metadata={
            "source_mime_type": input_mime,
            "container": "ogg",
            "codec": CANONICAL_CODEC,
            "sample_rate": CANONICAL_SAMPLE_RATE,
            "channels": CANONICAL_CHANNELS,
            "stream_count": 1,
            "duration": (probe.get("format") or {}).get("duration"),
            "bit_rate": (probe.get("format") or {}).get("bit_rate"),
            "validated_codec": stream.get("codec_name"),
        },
    )
