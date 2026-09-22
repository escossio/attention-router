"""Provider-neutral internal speech transcription boundary."""

from __future__ import annotations

from dataclasses import dataclass
import json
from urllib import error, request

from attention_router.config import Settings


ALLOWED_SPEECH_MIME_TYPES = frozenset(
    {"audio/ogg", "audio/opus", "audio/mpeg", "audio/mp4"}
)


class SpeechTranscriptionError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class SpeechTranscriptionResult:
    transcript: str
    provider: str
    model: str
    request_id: str | None
class InternalSpeechTranscriber:
    def __init__(self, *, settings: Settings):
        self.settings = settings

    def transcribe_bytes(
        self,
        audio: bytes,
        *,
        mime_type: str,
        max_bytes: int,
    ) -> SpeechTranscriptionResult:
        if not self.settings.stt_enabled:
            raise SpeechTranscriptionError("STT_DISABLED")
        if not self.settings.stt_internal_token:
            raise SpeechTranscriptionError("STT_INTERNAL_TOKEN_MISSING")
        if mime_type not in ALLOWED_SPEECH_MIME_TYPES:
            raise SpeechTranscriptionError("STT_UNSUPPORTED_MIME")
        if not audio:
            raise SpeechTranscriptionError("STT_EMPTY_AUDIO")
        if len(audio) > max_bytes:
            raise SpeechTranscriptionError("STT_AUDIO_TOO_LARGE")

        req = request.Request(
            f"{self.settings.stt_internal_url.rstrip('/')}/transcribe",
            data=audio,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.settings.stt_internal_token}",
                "Content-Type": mime_type,
                "Accept": "application/json",
            },
        )
        try:
            with request.urlopen(
                req,
                timeout=self.settings.stt_timeout_seconds,
            ) as response:
                raw = response.read(64 * 1024)
        except TimeoutError as exc:
            raise SpeechTranscriptionError("STT_TIMEOUT") from exc
        except error.HTTPError as exc:
            raw_error = exc.read(4096)
            code = self._provider_error_code(raw_error)
            raise SpeechTranscriptionError(
                code or f"STT_HTTP_{exc.code}"
            ) from None
        except OSError as exc:
            raise SpeechTranscriptionError("STT_NETWORK_ERROR") from exc

        try:
            value = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise SpeechTranscriptionError("STT_INVALID_RESPONSE") from exc

        transcript = value.get("transcript")
        if (
            value.get("status") != "ok"
            or not isinstance(transcript, str)
            or not transcript.strip()
        ):
            raise SpeechTranscriptionError("STT_EMPTY_TRANSCRIPT")
        provider = str(value.get("provider") or "")[:32]
        model = str(value.get("model") or "")[:80]
        request_id = value.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            request_id = None
        else:
            request_id = request_id[:180]
        return SpeechTranscriptionResult(
            transcript=transcript.strip(),
            provider=provider,
            model=model,
            request_id=request_id,
        )

    @staticmethod
    def _provider_error_code(raw: bytes) -> str | None:
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return None
        code = value.get("error_code")
        return str(code)[:120] if isinstance(code, str) and code else None


__all__ = [
    "ALLOWED_SPEECH_MIME_TYPES",
    "InternalSpeechTranscriber",
    "SpeechTranscriptionError",
    "SpeechTranscriptionResult",
]
