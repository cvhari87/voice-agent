"""Serve a tiny browser client for testing local LiveKit audio.

Run this after `./start_local_server.sh`, then open http://localhost:5173.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import threading
import warnings
from datetime import timedelta
from io import BytesIO
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import jwt
from livekit import api

from env_loader import load_env_files

# Railway (and most PaaS targets) inject $PORT and require binding 0.0.0.0;
# local dev has neither set, so it keeps the old localhost:5173 default.
HOST = os.getenv("TALK_HOST", "0.0.0.0" if os.getenv("PORT") else "localhost")
PORT = int(os.getenv("PORT") or os.getenv("TALK_PORT", "5173"))
ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
ASSIGNMENT_ROOT = ROOT.parent
PIPELINE_ROOT = ASSIGNMENT_ROOT / "pipeline"

TOKEN_TTL = timedelta(hours=1)

# Maximum request body size: 10 MB (generous for audio chunks).
MAX_BODY_BYTES = int(os.getenv("TALK_MAX_BODY_BYTES", str(10 * 1024 * 1024)))

# The demo only ever needs these two fixed room participants. Mapping a
# server-controlled role (rather than trusting client-supplied identity/room)
# means a caller can't mint a token for an arbitrary identity or room.
_DEMO_PARTICIPANTS = {
    "caller": ("caller-demo", "Caller Demo"),
    "agent": ("aurora-agent", "Aurora Agent"),
}

_session_registry_lock = threading.Lock()
_agent_sessions: dict[str, object] = {}
_session_locks: dict[str, threading.Lock] = {}

GREETING = "Thanks for calling Aurora Hotel reservations. How can I help?"


def _load_env_files() -> None:
    load_env_files((PIPELINE_ROOT / ".env", ROOT / ".env"))


def _agent_provider_name() -> str:
    return os.getenv("PROVIDER", "mock").lower()


def _livekit_url() -> str:
    raw = os.getenv("LIVEKIT_URL", "ws://localhost:7880")
    if raw.startswith("http://"):
        return "ws://" + raw[len("http://"):]
    if raw.startswith("https://"):
        return "wss://" + raw[len("https://"):]
    return raw


def _livekit_api_key() -> str:
    return os.getenv("LIVEKIT_API_KEY", "devkey")


def _livekit_api_secret() -> str:
    return os.getenv("LIVEKIT_API_SECRET", "secret")


def _livekit_room() -> str:
    return os.getenv("LIVEKIT_ROOM", "aurora-demo-room")


def _access_key() -> str | None:
    """Optional shared secret gating all API endpoints. Unset = no gate (local dev)."""
    return os.getenv("TALK_ACCESS_KEY") or None


def _request_authorized(handler: "Handler") -> bool:
    """Check authorization for any request.

    When TALK_ACCESS_KEY is set, the request must include it via the
    X-Access-Key header.  Query-parameter transport is intentionally
    avoided because query strings leak into browser history, server
    access logs, and Referer headers.

    When TALK_ACCESS_KEY is unset, all requests are allowed (local dev).
    """
    required = _access_key()
    if not required:
        return True
    header_value = handler.headers.get("X-Access-Key", "")
    if header_value and secrets.compare_digest(header_value, required):
        return True
    return False


def _new_agent():
    if str(PIPELINE_ROOT) not in sys.path:
        sys.path.insert(0, str(PIPELINE_ROOT))
    from agent import Agent
    from providers import make_provider

    return Agent(make_provider(_agent_provider_name()))


def _get_session(session_id: str):
    with _session_registry_lock:
        if session_id not in _agent_sessions:
            _agent_sessions[session_id] = _new_agent()
            _session_locks[session_id] = threading.Lock()
        return _agent_sessions[session_id], _session_locks[session_id]


def _reset_session(session_id: str) -> None:
    with _session_registry_lock:
        agent = _agent_sessions.pop(session_id, None)
        if agent is not None:
            agent.guardrail.remember_reset(session_id)
        _session_locks.pop(session_id, None)


def _trace(session_id: str, turn_id: str | None = None):
    if str(PIPELINE_ROOT) not in sys.path:
        sys.path.insert(0, str(PIPELINE_ROOT))
    from telemetry import TurnTrace

    return TurnTrace(session_id=session_id, turn_id=turn_id)


def _finish_response(agent, trace, reply: str, action: str | None, **extra) -> dict:
    from telemetry import write_trace

    sources = extra.pop("response_sources", agent.last_sources)
    payload = trace.finish(action=action, sources=sources)
    write_trace(payload)
    return {
        "reply": reply,
        "action": action,
        "provider": getattr(agent.provider, "name", _agent_provider_name()),
        "model": getattr(agent.provider, "llm_model", "unknown"),
        "language": agent.current_language,
        "locale": agent.current_locale,
        "sources": sources,
        "trace": payload,
        **extra,
    }


def _browser_tts_payload(agent, trace, text: str) -> dict:
    """Return provider audio for the browser or select its local voice fallback."""
    provider = agent.provider
    backend = getattr(provider, "tts_backend", "provider")
    if backend != "provider" or getattr(provider, "name", "") == "mock":
        return {"ttsBackend": "browser"}

    model = getattr(provider, "tts_model", "unknown")
    voice = getattr(provider, "tts_voice", "unknown")
    try:
        with trace.span("tts", model=model, voice=voice):
            audio = provider.synthesize(text)
    except Exception as exc:
        trace.event("tts.fallback", errorType=type(exc).__name__)
        return {"ttsBackend": "browser", "ttsFallback": True}

    if not audio:
        trace.event("tts.fallback", errorType="EmptyAudio")
        return {"ttsBackend": "browser", "ttsFallback": True}
    return {
        "ttsBackend": "provider",
        "ttsModel": model,
        "ttsVoice": voice,
        "audioContentType": "audio/wav",
        "audioBase64": base64.b64encode(audio).decode("ascii"),
    }


def _greeting_reply(session_id: str) -> dict:
    agent, lock = _get_session(session_id)
    trace = _trace(session_id, "greeting")
    trace.event("greeting.requested")
    with lock:
        tts = _browser_tts_payload(agent, trace, GREETING)
    return _finish_response(
        agent,
        trace,
        GREETING,
        None,
        response_sources=[],
        **tts,
    )


def _agent_reply(text: str, session_id: str, turn_id: str | None) -> dict:
    agent, lock = _get_session(session_id)
    trace = _trace(session_id, turn_id)
    trace.event("input.text")
    with lock:
        reply, action = agent.respond(text, trace=trace)
        tts = _browser_tts_payload(agent, trace, reply)
    return _finish_response(agent, trace, reply, action, **tts)


def _voice_agent_reply(
    audio: bytes,
    content_type: str,
    session_id: str,
    turn_id: str | None,
    was_barge_in: bool,
) -> dict:
    agent, lock = _get_session(session_id)
    trace = _trace(session_id, turn_id)
    trace.event("audio.received", bytes=len(audio), contentType=content_type)
    if was_barge_in:
        trace.event("barge_in.turn_started")
    with lock:
        if getattr(agent.provider, "name", "") == "mock":
            with trace.span("stt", model=getattr(agent.provider, "stt_model", "unknown")):
                transcript = agent.provider.transcribe(b"")
        else:
            audio_file = BytesIO(audio)
            if "mp4" in content_type:
                audio_file.name = "caller.mp4"
            elif "ogg" in content_type:
                audio_file.name = "caller.ogg"
            else:
                audio_file.name = "caller.webm"
            with trace.span("stt", model=getattr(agent.provider, "stt_model", "unknown")):
                transcription_args = {
                    "model": agent.provider.stt_model,
                    "file": audio_file,
                    "response_format": "text",
                }
                stt_prompt = getattr(agent.provider, "stt_prompt", "")
                if stt_prompt:
                    transcription_args["prompt"] = stt_prompt
                stt = agent.provider.client.audio.transcriptions.create(**transcription_args)
            transcript = (stt if isinstance(stt, str) else stt.text).strip()
        if getattr(agent.provider, "name", "") != "mock" and _is_probable_stt_hallucination(transcript):
            trace.event("stt.hallucination_suppressed", transcript=transcript)
            return _finish_response(
                agent,
                trace,
                "",
                None,
                transcript=transcript,
                sttModel=getattr(agent.provider, "stt_model", "unknown"),
                ignored=True,
                ignoreReason="probable_stt_hallucination",
                response_sources=[],
            )
        if was_barge_in and _is_probable_playback_echo(transcript):
            trace.event("barge_in.echo_suppressed", transcript=transcript)
            return _finish_response(
                agent,
                trace,
                "",
                None,
                transcript=transcript,
                sttModel=getattr(agent.provider, "stt_model", "unknown"),
                ignored=True,
                ignoreReason="probable_playback_echo",
                response_sources=[],
            )
        reply, action = agent.respond(transcript, trace=trace)
        tts = _browser_tts_payload(agent, trace, reply)
    return _finish_response(
        agent,
        trace,
        reply,
        action,
        transcript=transcript,
        sttModel=getattr(agent.provider, "stt_model", "unknown"),
        **tts,
    )


def _is_probable_playback_echo(transcript: str) -> bool:
    normalized = " ".join(
        transcript.lower().replace("'", "").replace(".", "").replace(",", "").split()
    )
    return normalized in {
        "all right",
        "alright",
        "thanks",
        "thank you",
        "youre welcome",
        "your welcome",
    }


# Meaningful short utterances that must NOT be filtered even though they are
# very short. These are common booking-flow responses.
_VALID_SHORT_UTTERANCES = {
    "no", "si", "sí", "ok", "yes", "ya", "go", "hi",
}

# Long, distinctive Whisper hallucination phrases for silence/non-speech --
# safe to substring-match since real booking speech would never contain
# these multi-word sequences.
_KNOWN_HALLUCINATIONS = {
    "thanks for watching",
    "thank you for watching",
    "please subscribe",
    "like and subscribe",
    "subtitles by the amara.org community",
    "subtítulos por la comunidad de amara.org",
}

# Short/ambiguous hallucination outputs that MUST be exact-matched, not
# substring-matched -- "you"/"bye" are common words inside completely normal
# speech ("thank you", "goodbye") and would over-trigger as substrings.
_EXACT_HALLUCINATIONS = {
    "you",
    "bye",
    "the end",
}

# Patterns that indicate subtitle-credit / website-spam hallucinations.
# These match specific structures, NOT bare domain suffixes.
_HALLUCINATION_PATTERN_MARKERS = (
    "www.",
    "amara.org",
    "subtitles by",
    "subtítulos por",
    "subscribe to",
    "follow us on",
)


def _is_probable_stt_hallucination(transcript: str) -> bool:
    """Heuristic filter for empty/near-empty audio and common Whisper
    hallucination artifacts -- website/subtitle-credit text Whisper models
    are known to emit for silence or non-speech audio -- so these aren't
    sent to the LLM as if they were real caller speech.

    Deliberately allows short meaningful utterances (no, sí, ok) and
    text containing email addresses (.com, .org, .net) since those are
    valid booking-flow inputs.
    """
    stripped = transcript.strip()
    if not stripped:
        return True
    normalized = stripped.lower()
    # Allow known meaningful short utterances.
    if normalized in _VALID_SHORT_UTTERANCES:
        return False
    # Reject single non-word characters (punctuation artifacts).
    if len(normalized) <= 1:
        return True
    # Reject known hallucination phrases -- substring match, not exact, since
    # these are long/distinctive enough (unlike short words) that trailing
    # punctuation or being embedded in a slightly longer generation
    # ("Thanks for watching!") shouldn't let them slip through.
    if any(phrase in normalized for phrase in _KNOWN_HALLUCINATIONS):
        return True
    # Short/ambiguous hallucinations ("you", "bye") must be an exact match
    # (after stripping trailing punctuation) -- substring matching these
    # would discard completely normal speech ("thank you", "goodbye").
    if normalized.rstrip(".!?,;: ") in _EXACT_HALLUCINATIONS:
        return True
    # Reject subtitle/website-spam patterns.
    return any(marker in normalized for marker in _HALLUCINATION_PATTERN_MARKERS)


def _token(identity: str, name: str, room: str) -> str:
    if _livekit_api_secret() == "secret":
        warnings.filterwarnings("ignore", category=jwt.InsecureKeyLengthWarning)
    return (
        api.AccessToken(_livekit_api_key(), _livekit_api_secret())
        .with_identity(identity)
        .with_name(name)
        .with_ttl(TOKEN_TTL)
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room,
                can_publish=True,
                can_subscribe=True,
            )
        )
        .to_jwt()
    )


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.path = "/index.html"
            return super().do_GET()
        if parsed.path == "/state":
            return self._send_json({
                "livekitRoom": _livekit_room(),
                "livekitUrl": _livekit_url(),
                "agentProvider": _agent_provider_name(),
                "languages": ["en", "es"],
                "accessKeyRequired": _access_key() is not None,
            })
        if parsed.path != "/token":
            return super().do_GET()

        # Auth check for /token.
        if not _request_authorized(self):
            return self._send_json({"error": "unauthorized"}, status=401)

        query = parse_qs(parsed.query)
        role = query.get("role", [""])[0]
        participant = _DEMO_PARTICIPANTS.get(role)
        if participant is None:
            return self._send_json({"error": "invalid role"}, status=400)
        identity, name = participant
        room = _livekit_room()

        payload = {
            "url": _livekit_url(),
            "room": room,
            "identity": identity,
            "token": _token(identity, name, room),
        }
        self._send_json(payload)

    def do_POST(self) -> None:
        # Auth check for all POST endpoints.
        if not _request_authorized(self):
            return self._send_json({"error": "unauthorized"}, status=401)

        parsed = urlparse(self.path)
        session_id = self.headers.get("X-Session-ID", "browser-demo")
        turn_id = self.headers.get("X-Turn-ID")

        if parsed.path == "/reset":
            _reset_session(session_id)
            return self._send_json({"reset": True, "sessionId": session_id})
        if parsed.path == "/greeting":
            try:
                return self._send_json(_greeting_reply(session_id))
            except Exception as exc:
                return self._send_json({"error": str(exc)}, status=500)
        if parsed.path == "/voice-agent":
            return self._handle_voice_agent(session_id, turn_id)
        if parsed.path != "/agent":
            self.send_error(404, "File not found")
            return

        try:
            body = self._read_body()
            if body is None:
                return  # error already sent
            payload = json.loads(body or b"{}")
            text = str(payload.get("text", "")).strip()
            if not text:
                raise ValueError("Missing text")
            response = _agent_reply(text, session_id, turn_id)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)
            return
        self._send_json(response)

    def _handle_voice_agent(self, session_id: str, turn_id: str | None) -> None:
        try:
            audio = self._read_body()
            if audio is None:
                return  # error already sent
            if not audio:
                raise ValueError("Missing audio")
            response = _voice_agent_reply(
                audio,
                self.headers.get("Content-Type", ""),
                session_id,
                turn_id,
                self.headers.get("X-Barge-In", "false").lower() == "true",
            )
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)
            return
        self._send_json(response)

    def _read_body(self) -> bytes | None:
        """Read the request body, enforcing the max body size limit.

        Returns the body bytes, or None if the body exceeds MAX_BODY_BYTES
        (in which case a 413 response is already sent).
        """
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY_BYTES:
            self._send_json(
                {"error": f"Request body too large (max {MAX_BODY_BYTES} bytes)"},
                status=413,
            )
            return None
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    _load_env_files()
    os.environ.setdefault(
        "TELEMETRY_JSONL",
        str(ASSIGNMENT_ROOT / "logs" / "voice-events.jsonl"),
    )
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Open http://{HOST}:{PORT}")
    print(f"LiveKit URL: {_livekit_url()}")
    print(f"Room: {_livekit_room()}")
    print(f"Agent provider: {_agent_provider_name()}")
    print(f"TTS backend: {os.getenv('TTS_BACKEND', 'provider').lower()}")
    access_status = "enabled" if _access_key() else "disabled (local dev)"
    print(f"Access key: {access_status}")
    print("Use the two panes for LiveKit audio. Use the conversation panel for the hotel agent.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
