# Aurora — AI Voice Agent for Hotel Reservations

**Live demo:** https://voice-agent-production-8dbe.up.railway.app
**Video walkthrough:** https://www.loom.com/share/510d9537bfec47a6abaedae53958ffea
**Engineering write-up:** [RUN_REPORT.md](RUN_REPORT.md)

Aurora is a voice AI agent for hotel reservations, built the way a real production system is built rather than a scripted demo. A caller talks to it in the browser like a real front-desk call: it checks availability, books a room, answers policy questions grounded in the hotel's actual documentation, and switches fluidly between English and Spanish mid-conversation — deployed publicly, not just running on a laptop.

The design principle behind it: nothing the agent says should be invented. A deterministic safety layer runs ahead of the model on every turn, every factual claim is either tool-verified or retrieval-grounded, and the whole pipeline is instrumented with structured, privacy-redacted telemetry.

The core pipeline:

```text
caller audio -> VAD/endpointing -> STT -> guardrails -> LLM -> RAG/tools -> guardrails -> TTS
```

## What it does

- Checks availability and creates bookings through real business tools — no hallucinated rates, availability, or confirmation numbers
- Answers hotel-policy questions (cancellation, pets, parking, breakfast, accessibility) grounded in source documents, with visible citations, not model memory
- Switches between English and Spanish mid-call, with routing state that survives interruptions and courtesy phrases like "¡Gracias!"
- Runs on Groq or OpenAI interchangeably — same codebase, one config line, no code changes
- Deterministic guardrails ahead of and after every model call: emergency detection, cross-guest privacy protection, booking-consent enforcement, date/capacity validation, and prompt-injection/leak detection — none of it depends on the LLM's own judgment
- A full browser calling experience over LiveKit — real-time audio, adaptive voice-activity detection, and mid-response barge-in
- Structured, redacted telemetry on every turn (session/turn/trace IDs, per-stage timings, tool calls, grounding sources)
- A deterministic evaluation suite, including a red-team suite, that gates changes without spending a cent on model calls
- A capacity-planning calculator and SIP/IVR telephony mapping for reasoning about scale beyond a single laptop

## Project structure

```text
Voice Agent/
|-- README.md
|-- RUN_REPORT.md
|-- knowledge/
|   `-- hotel_policies.md
|-- evals/
|   |-- core.json
|   |-- red_team.json
|   `-- run_evals.py
|-- pipeline/
|   |-- agent.py
|   |-- guardrails.py
|   |-- knowledge.py
|   |-- providers.py
|   |-- router.py
|   |-- scale_check.py
|   |-- telemetry.py
|   |-- test_features.py
|   `-- voice_loop.py
|-- livekit/
|   |-- start_local_server.sh
|   |-- create_room.py
|   |-- talk_server.py
|   `-- web/
`-- mocks/
    |-- demo_call.py
    |-- ivr_menu_mock.py
    `-- sip-ivr-call-flow.md
```

## Try it locally, no API key needed

The full agent, tool, RAG, routing, evaluation, and scale-planning paths run offline with a deterministic mock model — no network access or paid API calls required.

```bash
cd pipeline
python3 smoke_test.py
python3 -m unittest -v test_features.py
PROVIDER=mock python3 voice_loop.py --text
```

Try these turns:

```text
What is the weather?
What is the cancellation policy?
I need a room from August 12 to August 14 for two guests.
Please speak Spanish.
¿Cuál es la política de mascotas?
Connect me to the front desk.
```

## Running it with a real model

```bash
cd pipeline
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.env .env
```

Set the following in `pipeline/.env`:

```env
PROVIDER=openai
OPENAI_API_KEY=your_key_here
TTS_BACKEND=system
TELEMETRY_JSONL=../logs/voice-events.jsonl
```

Verify the live model before adding audio:

```bash
python voice_loop.py --text
```

Run the local microphone cascade:

```bash
python voice_loop.py
```

The terminal reports capture, STT, routing, retrieval, LLM, tool, TTS, and total turn timing. `TTS_BACKEND=system` uses the local OS voice and avoids cloud TTS cost during development.

Set `TTS_BACKEND=provider` to use the selected provider's configured TTS model and voice — this incurs audio-generation cost.

Groq works identically — the provider adapter uses the same tool-calling interface for both:

```env
PROVIDER=groq
GROQ_API_KEY=your_key_here
TTS_BACKEND=system
```

## Local LiveKit room demo

Install the room demo once:

```bash
cd livekit
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
npm install
```

Use three terminals.

Terminal 1 starts the self-contained LiveKit development server:

```bash
cd livekit
./start_local_server.sh
```

Terminal 2 creates the room and starts the browser application:

```bash
cd livekit
source .venv/bin/activate
python create_room.py
python talk_server.py
```

Open `http://localhost:5173`, click **Start call**, allow microphone access, and speak naturally. The browser automatically joins the caller and Aurora participants, detects caller turns, displays grounding sources, and shows stage-by-stage telemetry.

The LiveKit bridge honors `TTS_BACKEND` from `pipeline/.env`. With `provider`, the server synthesizes WAV audio using `TTS_MODEL` and `TTS_VOICE`, and the UI labels the response with the selected voice. With `system` or `mock`, the browser uses its installed speech voice.

The browser exposes two live tuning controls:

- **Endpoint silence** — how long a pause must be before a turn is committed
- **Speech sensitivity** — the adaptive speech threshold relative to the measured noise floor

Speak while Aurora is playing a response to trigger playback barge-in: the browser cancels speech output, records the interruption, and opens the next caller turn.

### Where this simplifies vs. a production system

The caller and Aurora identities are real LiveKit room participants. The current agent processes completed browser audio through an HTTP endpoint (`/voice-agent`), returns provider-generated WAV audio when enabled, and otherwise uses browser speech synthesis. It is not yet a room-native agent worker that subscribes to a live LiveKit audio track and publishes a TTS track directly — that's the natural next step for a fully real-time production version, along with persistent session storage, distributed cancellation, and SIP dispatch.

## Grounding and tools

Aurora uses different boundaries for different kinds of truth:

| Information | Mechanism | Reason |
|-------------|-----------|--------|
| Policies, parking, pets, breakfast, accessibility | Local RAG | Read-oriented knowledge with source evidence |
| Availability and room rates | Tool call | Dynamic operational truth |
| Booking creation | Tool call | Auditable state mutation |
| Language switching | `set_language` control tool | Validated session state and matching TTS locale |
| Transfer and hangup | Control action | Runtime and telephony behavior |

The local retriever indexes Markdown sections with SQLite FTS5, including English/Spanish query expansion, while keeping the source documents unchanged.

Aurora uses hybrid tool routing: high-confidence policy and amenity phrases select `search_hotel_knowledge` in application code before the first model call, while other tool decisions remain automatic. This keeps retrieval reliable after interruptions or off-topic turns, without misrouting something like "cancel my reservation" into policy search.

## Telemetry

Each turn carries a session ID, turn ID, trace ID, provider, model, language, per-stage timings, tool arguments, tool results, grounding sources, action, and ordered runtime events.

Raw transcript and response content are omitted by default, and sensitive tool fields such as guest name and contact details are redacted. `TELEMETRY_INCLUDE_CONTENT=true` exists only for controlled local debugging with non-sensitive data.

```text
logs/voice-events.jsonl
```

The path is gitignored. Set `TELEMETRY_JSONL` to change or disable the destination.

Production-grade measures worth tracking on top of this: endpoint delay, STT latency, LLM time to first token, tool latency, TTS time to first audio, end-of-turn to first audio, interruption latency, task completion, critical entity accuracy, transfer rate, and cost per successful outcome.

## Guardrail agent and audit memory

`pipeline/guardrails.py` is a deterministic safety layer that runs before the LLM and again before every requested tool call. It intercepts emergencies and cross-guest privacy requests, validates booking and retrieval arguments (including date sanity and room capacity), and stops repeated tool-call loops — none of these safety decisions depend on a second LLM call.

The guardrail keeps append-only audit memory in `logs/guardrail-memory.jsonl` (0600 permissions) by default: policy version, session ID, decision reason, allowed argument names, and state changes such as language switches or transfer actions. Raw caller text, guest names, and contact details are never stored — only a short hash and length. Override the path with `GUARDRAIL_MEMORY_JSONL`.

Inspect recent decisions during development:

```bash
tail -f logs/guardrail-memory.jsonl
```

This is an audit aid, not an authorization database. A real booking integration still needs authenticated sessions, transactional confirmation state, idempotency, retention controls, and centralized durable storage — see `RUN_REPORT.md` for the specific hardening items already identified and still open.

## Evaluation and red teaming

Run all deterministic scenarios:

```bash
cd evals
python3 run_evals.py --suite all
```

Run one suite with conversation details:

```bash
python3 run_evals.py --suite core --verbose
python3 run_evals.py --suite red-team --verbose
```

The suites verify expected tools, actions, languages, sources, allowed text, and forbidden text. The red-team set covers prompt injection, policy fabrication, privacy, structured tool input, and guardrails after a language switch.

## Scale check

A calculator that converts product assumptions into peak concurrency and service demand, with no provider calls:

```bash
cd pipeline
python3 scale_check.py --dau 1000000
```

Default assumptions: 0.25 calls per DAU, four minutes per call, three turns per minute, an 8x peak factor, 40 sessions per worker, and 30% headroom. Change every assumption before treating the result as an actual capacity plan.

```bash
python3 scale_check.py --dau 1000000 --cost-per-minute 0.035
```

## Telephony mapping

```text
PSTN caller -> carrier -> SIP trunk -> SBC or SIP edge -> LiveKit room -> agent -> tools
```

Run the local signaling demonstrations:

```bash
cd mocks
python3 demo_call.py
python3 demo_call.py --transfer
python3 ivr_menu_mock.py
```

The mock maps booking completion to SIP BYE and human escalation to SIP REFER. A real phone deployment also needs a carrier or telephony provider, an internet-reachable SIP edge, codec/media negotiation, security policy, dispatch rules, and a room-native agent worker.

## Security and cost notes

- `.env`, virtual environments, and telemetry logs are gitignored and never committed
- Mock mode is free and deterministic — used for rehearsal, evaluation, and scale exercises
- System/browser TTS avoids cloud TTS charges during development
- Booking tools are intentionally mock systems — see `RUN_REPORT.md` for the specific authentication, validation, idempotency, and persistence work a real integration would still need
