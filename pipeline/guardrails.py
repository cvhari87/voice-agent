"""Deterministic safety policy and persistent audit memory for Aurora."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path


POLICY_VERSION = "2026-07-21.1"
DEFAULT_MEMORY_PATH = Path(__file__).resolve().parent.parent / "logs" / "guardrail-memory.jsonl"
_write_lock = threading.Lock()

_EMERGENCY_TERMS = (
    "fire", "smoke", "medical emergency", "heart attack", "can't breathe",
    "cannot breathe", "overdose", "bleeding", "unconscious", "gun", "weapon",
    "intruder", "trapped", "elevator stuck", "incendio", "humo", "emergencia médica",
    "emergencia medica", "no puedo respirar", "arma", "atrapado", "atrapada",
)
_NON_EMERGENCY_CONTEXT = (
    "fireplace", "fire pit", "fire alarm policy", "smoke detector policy",
)
_PRIVACY_TARGETS = (
    "another guest", "other guest", "someone else's", "another reservation",
    "otro huésped", "otro huesped", "otra reserva",
)
_PRIVACY_FIELDS = (
    "email", "e-mail", "phone", "number", "address", "contact", "reservation details",
    "correo", "teléfono", "telefono", "dirección", "direccion", "datos",
)
_CONFIRMATION_TERMS = (
    "yes", "confirm", "book it", "go ahead", "reserve it", "do it",
    "sí", "si", "confirmo", "resérvala", "reservala", "adelante",
)
_PHONE_RE = re.compile(r"^\+?[0-9][0-9() .-]{6,20}$")
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@dataclass(frozen=True)
class GuardrailDecision:
    allowed: bool
    category: str
    reason: str
    response: str = ""
    action: str | None = None


class GuardrailMemory:
    """Append-only JSONL memory containing decisions, not conversation content."""

    def __init__(self, path: str | Path | None = None):
        configured = os.getenv("GUARDRAIL_MEMORY_JSONL") if path is None else str(path)
        self.path = Path(configured).expanduser() if configured else DEFAULT_MEMORY_PATH
        self.last_error: str | None = None

    def remember(self, session_id: str, event: str, **details) -> None:
        record = {
            "timestamp": time.time(),
            "policyVersion": POLICY_VERSION,
            "sessionId": _safe_session_id(session_id),
            "event": event,
            "details": _safe_details(details),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with _write_lock:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=True) + "\n")
            self.last_error = None
        except OSError as exc:
            # Safety decisions must still run if the audit destination is unavailable.
            self.last_error = type(exc).__name__

    def recent(self, session_id: str | None = None, limit: int = 50) -> list[dict]:
        try:
            if limit <= 0 or not self.path.exists():
                return []
            with _write_lock:
                lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            self.last_error = type(exc).__name__
            return []
        records: list[dict] = []
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if session_id is None or record.get("sessionId") == _safe_session_id(session_id):
                records.append(record)
            if len(records) >= limit:
                break
        return list(reversed(records))


class GuardrailAgent:
    """Evaluate caller input and model-requested tool calls before execution."""

    def __init__(self, memory: GuardrailMemory | None = None, max_tool_rounds: int = 6):
        self.memory = memory or GuardrailMemory()
        self.max_tool_rounds = max_tool_rounds
        self._session_state: dict[str, dict] = {}

    def evaluate_input(self, text: str, session_id: str) -> GuardrailDecision:
        normalized = " ".join(text.lower().split())
        if len(text) > 4000:
            return self._decision(
                session_id,
                GuardrailDecision(False, "input_length", "caller input exceeded 4000 characters",
                                  "That request is too long to process safely. Please say it more briefly."),
                text=text,
            )
        if (_contains_phrase(normalized, _EMERGENCY_TERMS)
                and not _contains_phrase(normalized, _NON_EMERGENCY_CONTEXT)):
            return self._decision(
                session_id,
                GuardrailDecision(
                    False,
                    "emergency",
                    "emergency language detected",
                    "If anyone is in immediate danger, call local emergency services now. I am also transferring you to the front desk.",
                    "transfer",
                ),
                text=text,
            )
        if (_contains_phrase(normalized, _PRIVACY_TARGETS)
                and _contains_phrase(normalized, _PRIVACY_FIELDS)):
            return self._decision(
                session_id,
                GuardrailDecision(
                    False,
                    "privacy",
                    "request targeted another guest's information",
                    "I cannot disclose another guest's information. I can help with your own reservation or transfer you to the front desk.",
                ),
                text=text,
            )
        return self._decision(
            session_id,
            GuardrailDecision(True, "input", "input passed deterministic policy"),
            text=text,
        )

    def evaluate_tool_call(
        self,
        name: str,
        arguments,
        caller_text: str,
        session_id: str,
        tool_round: int,
        turn_id: str | None = None,
    ) -> GuardrailDecision:
        if tool_round > self.max_tool_rounds:
            return self._decision(
                session_id,
                GuardrailDecision(False, "tool_budget", "maximum tool rounds exceeded",
                                  "I could not complete that safely. Let me transfer you to the front desk.", "transfer"),
                tool=name,
            )
        if not isinstance(arguments, dict):
            return self._decision(
                session_id,
                GuardrailDecision(False, "tool_schema", "tool arguments were not an object",
                                  "I need to verify those details before continuing."),
                tool=name,
            )
        validators = {
            "set_language": self._validate_language,
            "check_availability": self._validate_availability,
            "create_booking": lambda args: self._validate_booking(
                args, caller_text, session_id, turn_id,
            ),
            "search_hotel_knowledge": self._validate_knowledge,
            "transfer_to_human": lambda args: "" if not args else "transfer tool takes no arguments",
            "end_call": lambda args: "" if not args else "end-call tool takes no arguments",
        }
        validator = validators.get(name)
        if validator is None:
            return self._decision(
                session_id,
                GuardrailDecision(False, "tool_allowlist", "unknown tool requested",
                                  "I cannot perform that action."),
                tool=name,
            )
        error = validator(arguments)
        decision = GuardrailDecision(
            not error,
            "tool_call",
            error or "tool call passed deterministic policy",
            "I need to verify the booking details and receive your explicit confirmation before booking." if error else "",
        )
        recorded = self._decision(
            session_id,
            decision,
            tool=name,
            argumentKeys=sorted(arguments),
            toolRound=tool_round,
        )
        if recorded.allowed and name == "check_availability":
            previous_stage = self._session_state.get(session_id, {}).get("stage", "empty")
            self._session_state[session_id] = {
                "stage": "availability_checked",
                "availability": {
                    "check_in": str(arguments["check_in"]).strip(),
                    "check_out": str(arguments["check_out"]).strip(),
                    "guests": _guest_count(arguments["guests"]),
                    "room_type": str(arguments.get("room_type") or "").strip().lower(),
                },
                "turn_id": turn_id,
                "booking_fingerprints": set(),
            }
            self.remember_change(session_id, "bookingStage", previous_stage, "availability_checked")
        if recorded.allowed and name == "create_booking":
            state = self._session_state[session_id]
            fingerprint = _booking_fingerprint(arguments)
            state["booking_fingerprints"].add(fingerprint)
            previous_stage = state["stage"]
            state["stage"] = "booking_authorized"
            self.remember_change(session_id, "bookingStage", previous_stage, "booking_authorized")
        return recorded

    def remember_change(self, session_id: str, field: str, before, after) -> None:
        if before == after:
            return
        self.memory.remember(
            session_id,
            "state.changed",
            field=field,
            before=before,
            after=after,
        )

    def remember_tool_result(self, session_id: str, name: str, result: dict) -> None:
        if name != "create_booking" or not str(result.get("result", "")).startswith("Booking confirmed"):
            return
        state = self._session_state.get(session_id)
        if not state:
            return
        previous_stage = state.get("stage", "booking_authorized")
        state["stage"] = "booked"
        self.remember_change(session_id, "bookingStage", previous_stage, "booked")

    def remember_reset(self, session_id: str) -> None:
        previous_stage = self._session_state.pop(session_id, {}).get("stage", "empty")
        self.memory.remember(
            session_id,
            "session.reset",
            previousBookingStage=previous_stage,
        )

    def _decision(self, session_id: str, decision: GuardrailDecision, **context) -> GuardrailDecision:
        self.memory.remember(
            session_id,
            "guardrail.decision",
            decision=asdict(decision),
            context=context,
        )
        return decision

    @staticmethod
    def _validate_language(args: dict) -> str:
        return "" if args.get("language") in {"en", "es"} else "unsupported language"

    @staticmethod
    def _validate_availability(args: dict) -> str:
        if not _present_text(args.get("check_in")) or not _present_text(args.get("check_out")):
            return "check-in and check-out are required"
        guests = _guest_count(args.get("guests"))
        if guests is None or not 1 <= guests <= 20:
            return "guest count must be between 1 and 20"
        return ""

    def _validate_booking(
        self,
        args: dict,
        caller_text: str,
        session_id: str,
        turn_id: str | None,
    ) -> str:
        required = ("check_in", "check_out", "room_type", "guest_name", "contact")
        if any(not _present_text(args.get(key)) for key in required):
            return "required booking fields are missing"
        guests = _guest_count(args.get("guests"))
        if guests is None or not 1 <= guests <= 5:
            return "booking guest count must be between 1 and 5"
        if str(args["room_type"]).strip().lower() not in {
            "standard", "king", "suite", "family", "accessible",
        }:
            return "unknown room type"
        if str(args["check_in"]).strip().lower() == str(args["check_out"]).strip().lower():
            return "check-in and check-out cannot be the same"
        contact = str(args["contact"]).strip()
        if not (_EMAIL_RE.fullmatch(contact) or _PHONE_RE.fullmatch(contact)):
            return "contact must be a valid email address or phone number"
        normalized_caller = " ".join(caller_text.lower().split())
        if not _contains_phrase(normalized_caller, _CONFIRMATION_TERMS):
            return "caller has not explicitly confirmed this booking"
        state = self._session_state.get(session_id)
        if not state or state.get("stage") != "availability_checked":
            return "availability must be checked before booking"
        if turn_id is not None and state.get("turn_id") == turn_id:
            return "booking confirmation must occur on a later turn"
        offered = state["availability"]
        if (
            str(args["check_in"]).strip() != offered["check_in"]
            or str(args["check_out"]).strip() != offered["check_out"]
            or guests != offered["guests"]
        ):
            return "booking details changed after availability was checked"
        offered_room = offered.get("room_type")
        if offered_room and str(args["room_type"]).strip().lower() != offered_room:
            return "room type changed after availability was checked"
        if _booking_fingerprint(args) in state["booking_fingerprints"]:
            return "duplicate booking request"
        return ""

    @staticmethod
    def _validate_knowledge(args: dict) -> str:
        query = args.get("query")
        if not _present_text(query):
            return "knowledge query is required"
        return "" if len(str(query)) <= 1000 else "knowledge query is too long"


def _present_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _guest_count(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _contains_phrase(text: str, phrases: tuple[str, ...]) -> bool:
    return any(
        re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, flags=re.UNICODE)
        for phrase in phrases
    )


def _booking_fingerprint(arguments: dict) -> str:
    material = {
        key: str(arguments.get(key, "")).strip().lower()
        for key in ("check_in", "check_out", "guests", "room_type", "guest_name", "contact")
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_details(value, key: str = ""):
    sensitive = {"text", "caller_text", "guest_name", "contact", "email", "phone"}
    if key.lower() in sensitive:
        raw = str(value)
        return {
            "omitted": True,
            "length": len(raw),
            "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
        }
    if isinstance(value, dict):
        return {str(k): _safe_details(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_details(item) for item in value]
    if isinstance(value, str) and len(value) > 256:
        return {
            "omitted": True,
            "length": len(value),
            "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()[:16],
        }
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return type(value).__name__


def _safe_session_id(session_id: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", session_id):
        return session_id
    return "session-hash-" + hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
