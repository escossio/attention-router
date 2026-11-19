"""Offline unit tests for the bounded, consent-neutral Ogg STT fallback."""
import unittest

from attention_router.application.speech_transcription import (
    SpeechTranscriptionError, SpeechTranscriptionResult,
)
from attention_router.application.voice_stt_fallback import transcribe_consensus, encode_mp3


class MockTranscriber:
    def __init__(self, texts):
        self.texts = iter(texts)
        self.calls = 0

    def transcribe_bytes(self, data, *, mime_type, max_bytes):
        self.calls += 1
        assert mime_type == "audio/mpeg" and data and len(data) <= max_bytes
        value = next(self.texts)
        if value == "ERROR":
            raise SpeechTranscriptionError("STT_EMPTY_TRANSCRIPT")
        return SpeechTranscriptionResult(
            transcript=value, provider="mock", model="synthetic-asr", request_id="synthetic"
        )


def mock_encoder(path, rate, maximum):
    assert rate in (24000, 48000)
    return ("encoded" + str(rate)).encode()


class FallbackTests(unittest.TestCase):
    def test_consistent_transcripts_pass(self):
        text = "Combinamos uma reunião na próxima semana e vamos confirmar o horário."
        client = MockTranscriber([text, text])
        result = transcribe_consensus("synthetic", max_bytes=4096, transcriber=client, encoder=mock_encoder)
        self.assertIsNotNone(result)
        self.assertEqual(result.transcript, text)
        self.assertEqual(client.calls, 2)
        self.assertEqual(result.char_agreement, 1.0)

    def test_conflicting_transcripts_rejected(self):
        a = "Combinamos uma reunião na próxima semana e vamos confirmar o horário."
        b = "A previsão mudou completamente e o equipamento está desligado desde ontem."
        self.assertIsNone(transcribe_consensus("synthetic", max_bytes=4096, transcriber=MockTranscriber([a,b]), encoder=mock_encoder))

    def test_first_empty_stt_rejected(self):
        c=MockTranscriber(["ERROR"])
        self.assertIsNone(transcribe_consensus("synthetic", max_bytes=4096, transcriber=c, encoder=mock_encoder))
        self.assertEqual(c.calls, 1)

    def test_short_text_rejected(self):
        text = "Sim está certo"
        self.assertIsNone(transcribe_consensus("synthetic", max_bytes=4096, transcriber=MockTranscriber([text,text]), encoder=mock_encoder))

    def test_encoder_expansion_rejected(self):
        client=MockTranscriber(["Not reachable"])
        self.assertIsNone(transcribe_consensus("synthetic",max_bytes=4,transcriber=client,encoder=mock_encoder))
        self.assertEqual(client.calls,0)

    def test_invalid_rate_rejected(self):
        with self.assertRaises(ValueError):
            encode_mp3("dummy", 44100, 4096)

    def test_same_message_different_case_punctuation(self):
        a="Ficou combinado o agendamento da consulta, na segunda-feira de manhã."
        b="ficou combinado o agendamento da consulta na segunda feira de manha"
        candidate=transcribe_consensus("synthetic",max_bytes=4096,transcriber=MockTranscriber([a,b]),encoder=mock_encoder)
        self.assertIsNotNone(candidate)


if __name__ == "__main__":
    unittest.main()
