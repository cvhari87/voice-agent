"""Offline tests for browser TTS payload selection."""

from __future__ import annotations

import base64
import unittest
from contextlib import contextmanager

from talk_server import _browser_tts_payload, _is_probable_stt_hallucination


class FakeTrace:
    def __init__(self):
        self.events = []

    @contextmanager
    def span(self, name, **attributes):
        yield

    def event(self, name, **attributes):
        self.events.append((name, attributes))


class FakeProvider:
    name = "openai"
    tts_model = "tts-test"
    tts_voice = "voice-test"

    def __init__(self, backend="provider", error=None):
        self.tts_backend = backend
        self.error = error
        self.calls = []

    def synthesize(self, text):
        self.calls.append(text)
        if self.error:
            raise self.error
        return b"RIFFtest-wave"


class FakeAgent:
    def __init__(self, provider):
        self.provider = provider


class BrowserTtsPayloadTests(unittest.TestCase):
    def test_provider_backend_returns_audio(self):
        provider = FakeProvider()
        payload = _browser_tts_payload(FakeAgent(provider), FakeTrace(), "Hello")

        self.assertEqual(payload["ttsBackend"], "provider")
        self.assertEqual(base64.b64decode(payload["audioBase64"]), b"RIFFtest-wave")
        self.assertEqual(payload["ttsVoice"], "voice-test")
        self.assertEqual(provider.calls, ["Hello"])

    def test_system_backend_selects_browser_voice_without_provider_call(self):
        provider = FakeProvider(backend="system")
        payload = _browser_tts_payload(FakeAgent(provider), FakeTrace(), "Hello")

        self.assertEqual(payload, {"ttsBackend": "browser"})
        self.assertEqual(provider.calls, [])

    def test_provider_failure_falls_back_without_exposing_error(self):
        provider = FakeProvider(error=RuntimeError("secret provider response"))
        trace = FakeTrace()
        payload = _browser_tts_payload(FakeAgent(provider), trace, "Hello")

        self.assertEqual(payload, {"ttsBackend": "browser", "ttsFallback": True})
        self.assertEqual(trace.events[0][0], "tts.fallback")
        self.assertNotIn("secret provider response", str(payload))


class SttHallucinationFilterTests(unittest.TestCase):
    def test_empty_and_near_empty_transcripts_are_filtered(self):
        for text in ("", "   ", "a"):
            with self.subTest(text=text):
                self.assertTrue(_is_probable_stt_hallucination(text))

    def test_known_whisper_hallucination_patterns_are_filtered(self):
        """Whisper models are known to emit website/subtitle-credit text
        for silence or non-speech audio -- reproduces an artifact actually
        observed live in production."""
        for text in (
            "Más información www.alimmenta.com",
            "Thanks for watching!",
            "Subtitles by the Amara.org community",
        ):
            with self.subTest(text=text):
                self.assertTrue(_is_probable_stt_hallucination(text))

    def test_real_short_utterances_are_not_filtered(self):
        for text in ("What are you doing?", "¡Gracias!", "If you need a"):
            with self.subTest(text=text):
                self.assertFalse(_is_probable_stt_hallucination(text))

    def test_meaningful_short_booking_responses_are_kept(self):
        """A prior version of this filter discarded anything under 3
        characters, silently dropping "No", "OK", and "Sí" -- all common,
        legitimate booking-flow answers."""
        for text in ("No", "no", "Sí", "si", "OK", "ok", "Yes"):
            with self.subTest(text=text):
                self.assertFalse(_is_probable_stt_hallucination(text))

    def test_email_addresses_are_not_filtered(self):
        """A prior version of this filter treated any ".com"/".org"/".net"
        substring as a hallucination marker, which discarded real email
        addresses -- a required booking-flow input."""
        for text in ("hari@example.com", "My email is hari@example.com", "contact@aurorahotel.org"):
            with self.subTest(text=text):
                self.assertFalse(_is_probable_stt_hallucination(text))

    def test_short_hallucination_words_do_not_over_match_real_speech(self):
        """"you" and "bye" are real hallucination outputs Whisper can emit
        for silence, but they're also substrings of extremely common real
        speech ("thank you", "goodbye") -- these must require an exact
        match, not a substring match, unlike the longer/more distinctive
        hallucination phrases."""
        for text in ("Thank you very much", "Goodbye", "I need you to check availability", "At the end of my stay"):
            with self.subTest(text=text):
                self.assertFalse(_is_probable_stt_hallucination(text))
        for text in ("you", "You.", "bye", "Bye!", "the end"):
            with self.subTest(text=text):
                self.assertTrue(_is_probable_stt_hallucination(text))


if __name__ == "__main__":
    unittest.main()
