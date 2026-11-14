# Optional voice integration

The Router contains a TTS client contract, not the TTS provider implementation. Live voice requires a separately versioned compatible service plus transport configuration; offline tests replace the HTTP boundary.

The client posts to `{TTS_INTERNAL_URL}/synthesize` with a bearer token, JSON `{ "profile": "andy", "text": "20", "language": "pt-BR" }` and accepts bounded MP3 bytes. The optional language field must be consumed by the service/provider mapping, not prepended to speech text. An English numeric response uses `language: "en"`. A missing language is a legacy request shape, not permission to switch spontaneously.

Configure TTS_INTERNAL_URL/TTS_INTERNAL_TOKEN only in private environment configuration and enable TTS_ENABLED explicitly. STT has separate STT_INTERNAL_URL/STT_INTERNAL_TOKEN and STT_ENABLED settings. Provider credentials belong to the external service, not Router configuration or this repository. No provider-specific voice selection belongs in the Router client.

ffmpeg performs outbound Ogg/Opus normalization. Tests cover the contract and codec locally; they do not prove a specific provider's speech pronunciation or actual handset playback. A manual consented canary is a separate operation, never a unit test.

## Voice modality contract and acceptance

An eligible inbound PTT may be transcribed for understanding and returned as an outbound PTT only when all visitor-guard, TTS and approval/execution gates permit it. Text input does not become audio solely because TTS is enabled. The returned PTT must remain a single authorized effect, with outbox/delivery evidence; readiness and synthetic tests do not certify real handset playback.

The non-production test matrix, exact-SHA provenance and controlled reboot evidence are recorded in [WhatsApp voice modality acceptance](../operations/whatsapp-voice-modality-20261113.md). A consented end-to-end text and voice canary remains an explicit gate and cannot run while owner automatic responses are paused.
