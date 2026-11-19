"""Conservative, opt-in normalization fallback for historical Ogg/Opus STT only.

Never changes the original media. A candidate must agree across two independently
resampled audio encodings before it can become a derived transcription.
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
import re
import subprocess
import unicodedata

from attention_router.application.speech_transcription import (
    InternalSpeechTranscriber,
    SpeechTranscriptionError,
)


@dataclass(frozen=True)
class NormalizedConsensus:
    transcript: str
    provider: str
    model: str
    request_id: str | None
    char_agreement: float
    word_agreement: float


def encode_mp3(path: Path, sample_rate: int, maximum: int) -> bytes:
    """Bounded FFmpeg conversion, no shell or derived file written to disk."""
    if sample_rate not in (24000, 48000):
        raise ValueError("INVALID_NORMALIZATION_RATE")
    proc = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-i", str(path), "-ar", str(sample_rate), "-ac", "1",
            "-codec:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=20,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout or len(proc.stdout) > maximum:
        raise ValueError("NORMALIZATION_FAILED")
    return proc.stdout


def _words(transcript: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", transcript.casefold())
    text = "".join(char for char in folded if not unicodedata.combining(char))
    return re.findall(r"\w+", text, flags=re.UNICODE)


def transcribe_consensus(
    path: Path,
    *,
    max_bytes: int,
    transcriber: InternalSpeechTranscriber,
    encoder=encode_mp3,
) -> NormalizedConsensus | None:
    """Return None on inconclusive STT; never promote a solitary hypothesis."""
    if max_bytes < 1:
        raise ValueError("INVALID_STT_LIMIT")
    responses = []
    for sample_rate in (24000, 48000):
        try:
            encoded = encoder(path, sample_rate, max_bytes)
            if not encoded or len(encoded) > max_bytes:
                return None
            result = transcriber.transcribe_bytes(
                encoded, mime_type="audio/mpeg", max_bytes=max_bytes
            )
        except (SpeechTranscriptionError, ValueError, subprocess.TimeoutExpired):
            return None
        if not result.transcript or not result.transcript.strip():
            return None
        responses.append(result)

    a, b = (_words(item.transcript) for item in responses)
    if min(len(a), len(b)) < 5 or min(len(" ".join(a)), len(" ".join(b))) < 22:
        return None
    char_agreement = SequenceMatcher(None, " ".join(a), " ".join(b)).ratio()
    words_a, words_b = set(a), set(b)
    word_agreement = len(words_a & words_b) / max(1, len(words_a | words_b))
    if char_agreement < 0.92 or word_agreement < 0.85:
        return None

    first = responses[0]
    if first.model != responses[1].model or first.provider != responses[1].provider:
        return None
    return NormalizedConsensus(
        transcript=first.transcript.strip(),
        provider=first.provider,
        model=first.model,
        request_id=first.request_id,
        char_agreement=round(char_agreement, 4),
        word_agreement=round(word_agreement, 4),
    )
