import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib import error, request

from attention_router.config import settings


MP3_SIGNATURES = (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"ID3")


class TTSClientError(RuntimeError):
    pass


class TTSProvider(Protocol):
    def synthesize(self, text: str, output_path: Path, *, language: str | None = None) -> "TTSResult | None":
        ...


@dataclass
class TTSResult:
    profile: str
    provider: str | None
    request_id: str | None
    output_path: Path
    size_bytes: int
    output_mime: str = "audio/mpeg"


class DisabledTTSProvider:
    def synthesize(self, text: str, output_path: Path, *, language: str | None = None) -> TTSResult | None:
        return None


class SwitcherTTSClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        profile: str | None = None,
        timeout_seconds: float | None = None,
        max_response_bytes: int | None = None,
    ) -> None:
        self.base_url = (base_url or settings.tts_internal_url).rstrip("/")
        self.token = token if token is not None else settings.tts_internal_token
        self.profile = profile or settings.tts_profile
        self.timeout_seconds = timeout_seconds or settings.tts_timeout_seconds
        self.max_response_bytes = max_response_bytes or settings.tts_max_response_bytes

    def synthesize(self, text: str, output_path: Path, *, language: str | None = None) -> TTSResult:
        if self.profile != "andy":
            raise TTSClientError("unsupported_tts_profile")
        if not self.token:
            raise TTSClientError("missing_tts_internal_token")
        clean_text = text.strip()
        if not clean_text:
            raise TTSClientError("empty_tts_text")
        payload = {"profile": self.profile, "text": clean_text}
        if language:
            payload["language"] = language
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        req = request.Request(
            f"{self.base_url}/synthesize",
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json", "Accept": "audio/mpeg"},
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                audio = response.read(self.max_response_bytes + 1)
                request_id = response.headers.get("x-request-id")
                provider = response.headers.get("x-tts-provider")
        except TimeoutError as exc:
            raise TTSClientError("tts_timeout") from exc
        except error.HTTPError as exc:
            exc.read(4096)
            raise TTSClientError(f"tts_http_{exc.code}") from exc
        except OSError as exc:
            raise TTSClientError("tts_network_error") from exc
        if len(audio) > self.max_response_bytes:
            raise TTSClientError("tts_audio_too_large")
        if content_type not in {"audio/mpeg", "audio/mp3", "application/octet-stream"}:
            raise TTSClientError("tts_invalid_content_type")
        if not audio.startswith(MP3_SIGNATURES):
            raise TTSClientError("tts_invalid_audio")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(audio)
        return TTSResult(
            profile=self.profile,
            provider=provider,
            request_id=request_id,
            output_path=output_path,
            size_bytes=len(audio),
            output_mime="audio/mpeg",
        )


def configured_tts_provider() -> TTSProvider:
    if not settings.tts_enabled:
        return DisabledTTSProvider()
    return SwitcherTTSClient()
