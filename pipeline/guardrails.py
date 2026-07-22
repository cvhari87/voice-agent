"""Deterministic safety policy and persistent audit memory for Aurora."""

from __future__ import annotations

import calendar
import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path


POLICY_VERSION = "2026-07-21.2"
DEFAULT_MEMORY_PATH = Path(__file__).resolve().parent.parent / "logs" / "guardrail-memory.jsonl"
_write_lock = threading.Lock()
logger = logging.getLogger(__name__)

_EMERGENCY_TERMS = (
    "fire", "smoke", "medical emergency", "heart attack", "can't breathe",
    "cannot breathe", "overdose", "bleeding", "unconscious", "gun", "weapon",
    "intruder", "trapped", "elevator stuck", "incendio", "humo", "emergencia médica",
    "emergencia medica", "no puedo respirar", "arma", "atrapado", "atrapada",
)
_NON_EMERGENCY_CONTEXT = (
    "fireplace", "fire pit", "fire alarm policy", "smoke detector policy",
    "smoke-free", "smoke free", "smokefree", "no-smoking", "no smoking",
    "fire safety", "fire exit", "fire extinguisher", "fire escape",
    "fire alarm test", "fire drill", "fire prevention",
)
_NEGATION_PHRASES = (
    "do not have", "don't have", "dont have",
    "do not need", "don't need", "dont need",
    "is not", "isn't", "isnt",
    "was not", "wasn't", "wasnt",
    "not a", "not an", "no", "no hay", "no tengo", "no es",
    "no tiene", "without a", "without any",
    "never had", "never have",
)
_CLAUSE_SPLIT_RE = re.compile(
    r"[.!?;]|\s*,\s*(?:but|however|although|yet|still)\s+",
    re.IGNORECASE,
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
# Standalone rejections that don't pair with an explicit action noun nearby
# ("cancel that" refers to the booking only implicitly) -- checked directly
# per clause, since there's no specific term for a proximity check to attach
# to.
_BOOKING_REJECTION_PHRASES = (
    "cancel that", "cancel it", "stop that", "stop it",
    "never mind", "nevermind", "forget it",
    "cancela eso", "olvídalo", "olvidalo",
)
# Broader than _CONFIRMATION_TERMS so a negated action noun ("do not make
# the reservation", "stop the booking") is caught even though "reservation"/
# "booking" aren't themselves confirmation words. Only a match against a
# _CONFIRMATION_TERMS entry specifically counts as confirming, though --
# mentioning "reservation" isn't itself an agreement.
_BOOKING_ACTION_TERMS = _CONFIRMATION_TERMS + ("booking", "reservation")
# Negation of an actual booking action ("do not book it"), distinct from
# _NEGATION_PHRASES above which is tuned for negating an emergency's object
# ("don't have a fire extinguisher"). Includes bare rejection verbs (stop/
# cancel/forget) so "stop the booking" is caught via proximity to "booking".
_BOOKING_NEGATION_MARKERS = (
    "do not", "don't", "dont", "does not", "doesn't", "doesnt",
    "did not", "didn't", "didnt", "never", "no longer", "not",
    "stop", "cancel", "forget",
    "no confirmes", "no lo hagas", "no reserves",
)
# How many words immediately before a matched term count as "nearby" for
# negation purposes. Keeping this small is the point: a negation elsewhere
# in a long sentence (e.g. "I don't have a phone and there is smoke") must
# not suppress an unrelated match later in the same clause.
_NEGATION_WINDOW_WORDS = 4
_PHONE_RE = re.compile(r"^\+?[0-9][0-9() .-]{6,20}$")
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

# Room capacity limits (must stay in sync with agent._ROOMS).
_ROOM_CAPACITY = {
    "standard": 2,
    "king": 2,
    "suite": 4,
    "family": 5,
    "accessible": 2,
}

# Output guardrail: distinctive fragments from the system prompt that should
# never appear in model output sent to the caller.
_SYSTEM_PROMPT_FRAGMENTS = (
    "you are a friendly phone reservations agent",
    "do not answer questions outside hotel booking",
    "never invent availability, rates, confirmation",
    "keep replies short and spoken-friendly",
    "booking flow:",
    "guardrails:",
    "use tools for availability and booking",
    "use search_hotel_knowledge",
)
_INJECTION_OUTPUT_MARKERS = (
    "[system]", "[instruction", "<<sys>>", "<<user>>",
    "ignore previous", "disregard previous", "ignore above",
    "you are now", "new persona", "act as",
    "jailbreak", "developer mode", "dan mode",
)
_FABRICATED_CONFIRMATION_RE = re.compile(r"\bAH-\d{3,}", re.IGNORECASE)

# Date parsing formats, most specific first.
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m-%d-%Y",
    "%d/%m/%Y",
    "%B %d, %Y",
    "%B %d %Y",
    "%B %d",
    "%b %d, %Y",
    "%b %d %Y",
    "%b %d",
)

# Booking stages (state machine):
# empty -> availability_pending -> availability_checked -> summary_presented -> booking_authorized -> booked
_VALID_STAGES = frozenset({
    "empty",
    "availability_pending",
    "availability_checked",
    "summary_presented",
    "booking_authorized",
    "booked",
})


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
                # Self-healing: an audit log shouldn't stay world-readable just
                # because it pre-dates this fix or the umask was permissive.
                os.chmod(self.path, 0o600)
            self.last_error = None
        except OSError as exc:
            # Safety decisions must still run if the audit destination is unavailable.
            self.last_error = type(exc).__name__
            logger.warning(
                "guardrail memory write failed (%s); safety decisions are still"
                " enforced but this event has no audit record",
                self.last_error,
            )

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
    """Evaluate caller input, tool calls, and model output before delivery."""

    def __init__(self, memory: GuardrailMemory | None = None, max_tool_rounds: int = 6):
        self.memory = memory or GuardrailMemory()
        self.max_tool_rounds = max_tool_rounds
        self._session_state: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Input guardrail
    # ------------------------------------------------------------------

    def evaluate_input(self, text: str, session_id: str) -> GuardrailDecision:
        normalized = " ".join(text.lower().split())
        if len(text) > 4000:
            return self._decision(
                session_id,
                GuardrailDecision(False, "input_length", "caller input exceeded 4000 characters",
                                  "That request is too long to process safely. Please say it more briefly."),
                text=text,
            )
        if _is_emergency(normalized):
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

    # ------------------------------------------------------------------
    # Tool-call guardrail
    # ------------------------------------------------------------------

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
        # On allowed check_availability: move to availability_pending (not
        # availability_checked). The actual result must be verified first via
        # process_availability_result() before booking can proceed.
        if recorded.allowed and name == "check_availability":
            previous_stage = self._session_state.get(session_id, {}).get("stage", "empty")
            self._session_state[session_id] = {
                "stage": "availability_pending",
                "availability": {
                    "check_in": str(arguments["check_in"]).strip(),
                    "check_out": str(arguments["check_out"]).strip(),
                    "guests": _guest_count(arguments["guests"]),
                    "room_type": str(arguments.get("room_type") or "").strip().lower(),
                },
                "turn_id": turn_id,
                "booking_fingerprints": set(),
            }
            self.remember_change(session_id, "bookingStage", previous_stage, "availability_pending")
        if recorded.allowed and name == "create_booking":
            state = self._session_state[session_id]
            fingerprint = _booking_fingerprint(arguments)
            state["booking_fingerprints"].add(fingerprint)
            previous_stage = state["stage"]
            state["stage"] = "booking_authorized"
            self.remember_change(session_id, "bookingStage", previous_stage, "booking_authorized")
        return recorded

    # ------------------------------------------------------------------
    # Availability result verification (Fix #1)
    # ------------------------------------------------------------------

    def process_availability_result(self, session_id: str, result: dict) -> None:
        """Verify the availability tool result before advancing the booking state.

        Must be called after check_availability executes. Only advances from
        availability_pending to availability_checked if rooms were found.
        """
        state = self._session_state.get(session_id)
        if not state or state.get("stage") != "availability_pending":
            return
        result_text = str(result.get("result", ""))
        if "no matching rooms" in result_text.lower():
            previous_stage = state["stage"]
            state["stage"] = "empty"
            self.remember_change(session_id, "bookingStage", previous_stage, "empty")
            self.memory.remember(
                session_id,
                "availability.no_rooms",
                resultSummary="no matching rooms found",
            )
            return
        state["stage"] = "availability_checked"
        state["available_rooms_text"] = result_text
        self.remember_change(session_id, "bookingStage", "availability_pending", "availability_checked")

    # ------------------------------------------------------------------
    # Output guardrail (Fix #4)
    # ------------------------------------------------------------------

    def evaluate_output(
        self,
        text: str,
        session_id: str,
        had_booking_tool: bool = False,
    ) -> GuardrailDecision:
        """Evaluate model output before sending to the caller.

        Args:
            text: The model's response text.
            session_id: Current session identifier.
            had_booking_tool: True if create_booking was executed this turn.
        """
        if not text or not text.strip():
            return self._decision(
                session_id,
                GuardrailDecision(True, "output", "empty output passed"),
            )

        normalized = " ".join(text.lower().split())

        # Check for system prompt leakage.
        for fragment in _SYSTEM_PROMPT_FRAGMENTS:
            if fragment in normalized:
                return self._decision(
                    session_id,
                    GuardrailDecision(
                        False,
                        "output_leak",
                        f"model output contained system prompt fragment",
                        "I can help you with hotel reservations. Would you like to book, change, or cancel a stay?",
                    ),
                    fragment=fragment,
                )

        # Check for prompt injection output markers.
        for marker in _INJECTION_OUTPUT_MARKERS:
            if marker in normalized:
                return self._decision(
                    session_id,
                    GuardrailDecision(
                        False,
                        "output_injection",
                        "model output contained injection marker",
                        "I can help you with hotel reservations. Would you like to book, change, or cancel a stay?",
                    ),
                    marker=marker,
                )

        # Check for fabricated booking confirmations.
        if _FABRICATED_CONFIRMATION_RE.search(text) and not had_booking_tool:
            return self._decision(
                session_id,
                GuardrailDecision(
                    False,
                    "output_fabrication",
                    "model output contained confirmation code without booking tool execution",
                    "I need to complete the booking process first. Could you confirm the details so I can reserve your room?",
                ),
            )

        # Advance booking state: if availability was checked and the model
        # is now speaking (presenting options), advance to summary_presented.
        state = self._session_state.get(session_id)
        if state and state.get("stage") == "availability_checked":
            previous_stage = state["stage"]
            state["stage"] = "summary_presented"
            self.remember_change(session_id, "bookingStage", previous_stage, "summary_presented")

        return self._decision(
            session_id,
            GuardrailDecision(True, "output", "output passed deterministic policy"),
        )

    # ------------------------------------------------------------------
    # State & memory helpers
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Tool-specific validators
    # ------------------------------------------------------------------

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

        # Date validation: parse and enforce checkout > checkin.
        check_in_date = _parse_date(str(args["check_in"]).strip())
        check_out_date = _parse_date(str(args["check_out"]).strip())
        if check_in_date and check_out_date:
            if check_out_date <= check_in_date:
                return "check-out date must be after check-in date"

        # Room capacity pre-check: if a room type is specified, reject early
        # when guest count exceeds its capacity.
        room_type = str(args.get("room_type") or "").strip().lower()
        if room_type and room_type in _ROOM_CAPACITY:
            if guests > _ROOM_CAPACITY[room_type]:
                return (
                    f"{room_type} room holds {_ROOM_CAPACITY[room_type]} guests "
                    f"but {guests} were requested"
                )

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

        room_type = str(args["room_type"]).strip().lower()
        if room_type not in _ROOM_CAPACITY:
            return "unknown room type"

        # Room capacity enforcement.
        if guests > _ROOM_CAPACITY[room_type]:
            return (
                f"{room_type} room holds {_ROOM_CAPACITY[room_type]} guests "
                f"but {guests} were requested"
            )

        # Date validation.
        check_in_str = str(args["check_in"]).strip()
        check_out_str = str(args["check_out"]).strip()
        if check_in_str.lower() == check_out_str.lower():
            return "check-in and check-out cannot be the same"
        check_in_date = _parse_date(check_in_str)
        check_out_date = _parse_date(check_out_str)
        if check_in_date and check_out_date:
            if check_out_date <= check_in_date:
                return "check-out date must be after check-in date"

        contact = str(args["contact"]).strip()
        if not (_EMAIL_RE.fullmatch(contact) or _PHONE_RE.fullmatch(contact)):
            return "contact must be a valid email address or phone number"
        normalized_caller = " ".join(caller_text.lower().split())
        if not _caller_confirmed_booking(normalized_caller):
            return "caller has not explicitly confirmed this booking"
        state = self._session_state.get(session_id)
        if not state:
            return "availability must be checked before booking"

        # Require summary_presented (not just availability_checked). This
        # ensures Aurora actually spoke to the caller before booking.
        if state.get("stage") not in ("summary_presented",):
            if state.get("stage") == "availability_checked":
                return "booking summary must be presented to the caller before booking"
            if state.get("stage") == "availability_pending":
                return "availability result has not been verified"
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


# ------------------------------------------------------------------
# Emergency detection with clause-level analysis (Fix #3)
# ------------------------------------------------------------------

def _is_emergency(normalized_text: str) -> bool:
    """Detect emergencies with clause-splitting and negation awareness.

    Splits compound sentences so that non-emergency context (e.g. "fire alarm
    policy") in one clause does not suppress a real emergency ("smoke in my
    room") in another clause. Negation is checked in a short word-window
    immediately before the *specific* matched emergency term, not anywhere
    in the whole clause -- otherwise "I don't have a phone and there is
    smoke" would have its unrelated "don't have" (about the phone) suppress
    the real hazard ("smoke") elsewhere in the same clause. Likewise, benign
    context ("fire alarm policy") only neutralizes the specific term
    occurrence it overlaps -- "The fire alarm policy is fine but there is a
    fire" has a second, independent "fire" later in the same clause that
    must still be examined.
    """
    clauses = _CLAUSE_SPLIT_RE.split(normalized_text)
    if not clauses:
        clauses = [normalized_text]

    for clause in clauses:
        clause = clause.strip()
        if not clause:
            continue
        benign_spans = [
            m.span()
            for phrase in _NON_EMERGENCY_CONTEXT
            for m in re.finditer(rf"(?<!\w){re.escape(phrase)}(?!\w)", clause, flags=re.UNICODE)
        ]
        for term in _EMERGENCY_TERMS:
            for match in re.finditer(rf"(?<!\w){re.escape(term)}(?!\w)", clause, flags=re.UNICODE):
                if any(start <= match.start() < end for start, end in benign_spans):
                    continue
                if _term_negated_nearby(clause, match.start(), _NEGATION_PHRASES):
                    continue
                # Unambiguous emergency term, not neutralized or negated.
                return True
    return False


def _term_negated_nearby(text: str, term_start: int, markers: tuple[str, ...]) -> bool:
    """Check for a negation marker in the few words immediately before a
    matched term's position, rather than anywhere in the whole text.

    Markers are matched with word boundaries so a short marker like "no" or
    "not" can't false-match inside an unrelated longer word ("noticed",
    "cannot" as a single token, etc.).
    """
    preceding_words = text[:term_start].split()[-_NEGATION_WINDOW_WORDS:]
    window = " ".join(preceding_words)
    return any(
        re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", window, flags=re.UNICODE)
        for marker in markers
    )


def _caller_confirmed_booking(normalized_caller: str) -> bool:
    """Require an explicit, unnegated confirmation, failing closed on any
    conflicting intent.

    Splits into clauses (same boundary as emergency detection) so a
    contrastive correction anywhere in the utterance overrides any earlier
    bare agreement, rather than an isolated "yes" satisfying the check
    regardless of what follows it. Two distinct rejection shapes are
    checked: standalone rejections with no explicit action noun nearby
    ("cancel that"), and negation of an action term via proximity ("do not
    make the reservation", "stop the booking") -- the latter uses a broader
    term set than plain confirmation words so it catches negated action
    nouns, not just negated confirmation phrases.
    """
    clauses = _CLAUSE_SPLIT_RE.split(normalized_caller)
    if not clauses:
        clauses = [normalized_caller]

    found_confirmation = False
    for clause in clauses:
        clause = clause.strip()
        if not clause:
            continue
        if _contains_phrase(clause, _BOOKING_REJECTION_PHRASES):
            return False
        for term in _BOOKING_ACTION_TERMS:
            match = re.search(rf"(?<!\w){re.escape(term)}(?!\w)", clause, flags=re.UNICODE)
            if not match:
                continue
            if _term_negated_nearby(clause, match.start(), _BOOKING_NEGATION_MARKERS):
                return False
            if term in _CONFIRMATION_TERMS:
                found_confirmation = True
    return found_confirmation


# ------------------------------------------------------------------
# Date parsing (Fix #1)
# ------------------------------------------------------------------

def _parse_date(text: str) -> date | None:
    """Best-effort date parsing. Returns None if no format matches."""
    text = text.strip()
    if not text:
        return None
    # Try each format.
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
            # Formats without a year default to 1900; assume current or next year.
            if "%Y" not in fmt and "%y" not in fmt:
                today = date.today()
                candidate = parsed.replace(year=today.year).date()
                if candidate < today:
                    candidate = parsed.replace(year=today.year + 1).date()
                return candidate
            return parsed.date()
        except ValueError:
            continue
    return None


# ------------------------------------------------------------------
# Shared helpers
# ------------------------------------------------------------------

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
