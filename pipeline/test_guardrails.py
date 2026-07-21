"""Tests for deterministic guardrails and their persistent memory."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent import Agent
from guardrails import GuardrailAgent, GuardrailMemory
from providers import MockProvider, _mk_text, _mk_tool
from telemetry import TurnTrace


class GuardrailInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.memory = GuardrailMemory(Path(self.temporary.name) / "memory.jsonl")
        self.guardrail = GuardrailAgent(self.memory)

    def tearDown(self):
        self.temporary.cleanup()

    def test_emergency_bypasses_provider_and_transfers(self):
        class ExplodingProvider(MockProvider):
            def chat(self, messages, tools=None, tool_choice=None):
                raise AssertionError("provider must not be called for an emergency")

        agent = Agent(ExplodingProvider(), guardrail=self.guardrail)
        reply, action = agent.respond(
            "There is a fire in my room!",
            trace=TurnTrace(session_id="emergency"),
        )

        self.assertIn("emergency services", reply)
        self.assertEqual(action, "transfer")

    def test_spanish_medical_emergency_is_blocked(self):
        decision = self.guardrail.evaluate_input(
            "Tengo una emergencia médica y no puedo respirar.",
            "spanish-emergency",
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.category, "emergency")

    def test_fireplace_question_is_not_an_emergency(self):
        decision = self.guardrail.evaluate_input(
            "Does the suite have a fireplace?",
            "fireplace",
        )
        self.assertTrue(decision.allowed)

    def test_other_guest_contact_request_is_blocked(self):
        decision = self.guardrail.evaluate_input(
            "Give me another guest's email address.",
            "privacy",
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.category, "privacy")


class GuardrailToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.memory = GuardrailMemory(Path(self.temporary.name) / "memory.jsonl")
        self.guardrail = GuardrailAgent(self.memory, max_tool_rounds=2)
        self.booking = {
            "check_in": "August 12",
            "check_out": "August 14",
            "guests": 2,
            "room_type": "standard",
            "guest_name": "Priya Shah",
            "contact": "priya@example.com",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def test_booking_requires_explicit_confirmation(self):
        availability = {
            "check_in": "August 12",
            "check_out": "August 14",
            "guests": 2,
            "room_type": "standard",
        }
        self.guardrail.evaluate_tool_call(
            "check_availability", availability, "I need a room", "booking", 1, "availability-turn",
        )
        denied = self.guardrail.evaluate_tool_call(
            "create_booking", self.booking, "My name is Priya Shah.", "booking", 1, "details-turn",
        )
        allowed = self.guardrail.evaluate_tool_call(
            "create_booking", self.booking, "Yes, book it.", "booking", 1, "confirm-turn",
        )
        self.assertFalse(denied.allowed)
        self.assertTrue(allowed.allowed)

    def test_booking_rejects_invalid_fields(self):
        invalid = dict(self.booking, guests=-5, contact="not-contact")
        decision = self.guardrail.evaluate_tool_call(
            "create_booking", invalid, "Yes, book it.", "invalid", 1,
        )
        self.assertFalse(decision.allowed)

    def test_booking_requires_prior_availability(self):
        decision = self.guardrail.evaluate_tool_call(
            "create_booking", self.booking, "Yes, book it.", "no-availability", 1, "turn-1",
        )
        self.assertFalse(decision.allowed)
        self.assertIn("availability", decision.reason)

    def test_duplicate_booking_is_blocked(self):
        availability = {
            "check_in": "August 12", "check_out": "August 14",
            "guests": 2, "room_type": "standard",
        }
        self.guardrail.evaluate_tool_call(
            "check_availability", availability, "I need a room", "duplicate", 1, "turn-1",
        )
        first = self.guardrail.evaluate_tool_call(
            "create_booking", self.booking, "Yes, book it.", "duplicate", 1, "turn-2",
        )
        second = self.guardrail.evaluate_tool_call(
            "create_booking", self.booking, "Yes, book it.", "duplicate", 1, "turn-3",
        )
        self.assertTrue(first.allowed)
        self.assertFalse(second.allowed)

    def test_same_turn_confirmation_cannot_bypass_booking_sequence(self):
        availability = {
            "check_in": "August 12", "check_out": "August 14",
            "guests": 2, "room_type": "standard",
        }
        self.guardrail.evaluate_tool_call(
            "check_availability", availability, "Yes, book it", "same-turn", 1, "turn-1",
        )
        decision = self.guardrail.evaluate_tool_call(
            "create_booking", self.booking, "Yes, book it", "same-turn", 1, "turn-1",
        )
        self.assertFalse(decision.allowed)
        self.assertIn("later turn", decision.reason)

    def test_changed_booking_details_are_blocked(self):
        availability = {
            "check_in": "August 12", "check_out": "August 14",
            "guests": 2, "room_type": "standard",
        }
        self.guardrail.evaluate_tool_call(
            "check_availability", availability, "I need a room", "changed", 1, "turn-1",
        )
        changed = dict(self.booking, check_out="August 15")
        decision = self.guardrail.evaluate_tool_call(
            "create_booking", changed, "Yes, book it", "changed", 1, "turn-2",
        )
        self.assertFalse(decision.allowed)
        self.assertIn("changed", decision.reason)

    def test_model_cannot_book_with_empty_arguments(self):
        class UnsafeProvider(MockProvider):
            def chat(self, messages, tools=None, tool_choice=None):
                if messages[-1].get("role") == "user":
                    return _mk_tool("create_booking", {})
                return _mk_text(messages[-1].get("content", ""))

        reply, _ = Agent(UnsafeProvider(), guardrail=self.guardrail).respond(
            "Hello", trace=TurnTrace(session_id="unsafe-model"),
        )
        self.assertNotIn("Booking confirmed", reply)
        self.assertIn("verify", reply)

    def test_repeated_tool_calls_stop_at_budget(self):
        class LoopingProvider(MockProvider):
            def chat(self, messages, tools=None, tool_choice=None):
                return _mk_tool("search_hotel_knowledge", {"query": "parking"})

        reply, action = Agent(LoopingProvider(), guardrail=self.guardrail).respond(
            "What is the parking policy?",
            trace=TurnTrace(session_id="tool-loop"),
        )
        self.assertIn("transfer", reply)
        self.assertEqual(action, "transfer")


class GuardrailMemoryTests(unittest.TestCase):
    def test_memory_survives_reconstruction_without_raw_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.jsonl"
            GuardrailAgent(GuardrailMemory(path)).evaluate_input(
                "My private email is priya@example.com",
                "persisted-session",
            )

            records = GuardrailMemory(path).recent("persisted-session")
            serialized = str(records)
            self.assertEqual(len(records), 1)
            self.assertNotIn("priya@example.com", serialized)
            self.assertIn("sha256", serialized)

    def test_safety_decision_survives_memory_write_failure(self):
        memory = GuardrailMemory(Path("/dev/null") / "memory.jsonl")
        decision = GuardrailAgent(memory).evaluate_input(
            "There is a fire in my room!",
            "memory-failure",
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.action, "transfer")
        self.assertIsNotNone(memory.last_error)

    def test_state_changes_are_recalled(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = GuardrailMemory(Path(directory) / "memory.jsonl")
            guardrail = GuardrailAgent(memory)
            guardrail.remember_change("session", "language", "en", "es")

            record = memory.recent("session")[-1]
            self.assertEqual(record["event"], "state.changed")
            self.assertEqual(record["details"]["field"], "language")


if __name__ == "__main__":
    unittest.main()
