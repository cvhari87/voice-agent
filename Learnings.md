# Learnings — Aurora Hotel Voice Agent

Technical takeaways from reading and testing this codebase, organized by topic.

## Voice agent architecture (the cascade)

`caller audio -> VAD/endpointing -> STT -> AgentRouter -> LLM -> RAG/tools -> TTS`

Each stage is a separate latency contributor, and the project's telemetry (`telemetry.py`) times every one of them individually (`capture`, `stt`, `routing`, `retrieval`, `llm`, `tools`, `tts`). That per-stage breakdown is what lets you actually answer "why does this turn feel slow" instead of guessing — the whole turn budget is a sum of independently-optimizable pieces.

## Provider abstraction is one adaptor, not two

Groq and OpenAI both speak the OpenAI SDK dialect, so `providers.py` implements a single `Provider` class and only swaps `base_url`, `api_key`, and model-name presets per backend (`providers.py:26-44`). Practical implication: you can develop against a free Groq key and flip to OpenAI later by changing one `.env` line, with zero code changes. A `MockProvider` implements the exact same interface (`chat`/`transcribe`/`synthesize`) with rule-based logic, so the rest of the system (agent loop, voice loop, LiveKit bridge) genuinely cannot tell mock from live — that's what makes the offline smoke test and eval suite possible with no network access at all.

## Hybrid tool routing beats "let the LLM decide" for high-confidence intents

`agent.py`'s `required_tool_for()` pre-detects high-confidence policy/amenity phrases (including fuzzy matching via `SequenceMatcher` for typos/STT noise) and forces `tool_choice` to `search_hotel_knowledge` on the *first* model call only. This solves a real failure mode: once a model has just refused an off-topic question, it can get "sticky" and fail to call the right tool on the very next in-scope question. Forcing the tool call for known-good intents, then falling back to normal automatic tool selection, is a pattern worth remembering any time an agent's tool selection needs to be reliable rather than merely usually-correct.

## Language switching needs an explicit-intent gate, not vibes

The naive approach — let the model call `set_language` whenever it thinks the caller wants a language change — breaks the moment a caller says a stray foreign word or a courtesy phrase ("¡Gracias!"). This project's fix: `explicit_language_request()` requires the *literal current utterance* to name the target language before the session state is allowed to change, even if the model proposes the tool call. The router only commits the change after that check passes (`agent.py:403-446`). This is tested directly — `test_overeager_language_tool_cannot_change_state` simulates a model that *always* tries to switch language, and proves the session doesn't flip anyway.

## Self-contained RAG can skip a vector DB entirely

`knowledge.py` indexes Markdown sections into SQLite's FTS5 virtual table with BM25 ranking, plus a hand-rolled Spanish→English query expansion dict (`mascotas` → `pets, dogs`, etc.) and a lexical-overlap fallback if FTS5 isn't available. For a small, mostly-static knowledge base (a handful of hotel policy sections), this is genuinely simpler and more debuggable than standing up embeddings + a vector store — a good reminder that RAG doesn't always mean "vector DB."

## Telemetry: structured tracing without an observability vendor

`TurnTrace` (`telemetry.py`) implements a minimal span/event tracer from scratch — `span()` is a context manager that records start/complete/failed events with durations, `event()` appends arbitrary structured attributes, and `_sanitize()` redacts PII fields (`guest_name`, `contact`, etc.) and omits raw conversation content by default (`TELEMETRY_INCLUDE_CONTENT` opts back in for local debugging only). This is essentially a tiny OpenTelemetry-shaped tracer purpose-built for one voice turn — useful pattern for any project that needs structured traces before it's worth adopting a real tracing stack.

## Client-side barge-in is genuinely hard, and the tricks matter

The browser VAD (`web/talk.js`) has to solve a problem that doesn't exist in text chat: distinguishing "the caller is interrupting" from "the microphone is just picking up the agent's own TTS playback" (acoustic echo, no hardware echo cancellation guarantee in a browser demo). It does this with a *playback echo floor* that's calibrated for the first ~450ms of any agent response (`bargeInArmMs`), and only treats sustained audio above that floor as a real barge-in candidate, confirmed over a debounce window (`bargeInConfirmationMs`) before it actually cancels playback. Noise-floor calibration is adaptive and continuous (`noiseFloor` decays/updates only when the caller isn't speaking), not a fixed threshold — a fixed threshold would fail differently in a quiet room versus a noisy one.

## The LiveKit integration is a workshop bridge, not a production pattern — and that's explicit

The README/RUNBOOK are upfront about this: the caller and agent are real LiveKit room participants, but the actual audio processing goes through a plain HTTP endpoint (`/voice-agent`) rather than a LiveKit agent worker subscribing to a live audio track. A production version would need a room-native worker, distributed cancellation, and persistent session storage. Worth remembering when explaining the architecture in a demo — it's a deliberate simplification for a local workshop, not an oversight.

## Deployment reality: WebRTC media and app hosting are two different problems

The hardest part of deploying this isn't the Python server — it's that real-time audio needs UDP/ICE/TURN traversal, which a local Docker dev LiveKit server doesn't provide and self-hosting properly is a multi-day problem on its own. Decoupling that concern (use a managed service like LiveKit Cloud for the room/media) from the app-hosting concern (Railway/Fly for the stateful HTTP bridge + frontend) turns a hard infra problem into a config change. Also: this app holds session state in process memory (`_agent_sessions` dict), which rules out serverless platforms like Vercel without adding an external session store — a good example of how a platform choice is really a question about where your state lives.

## Deterministic evals let you red-team an agent without spending money

`evals/run_evals.py` drives the exact same `Agent` class used in production against scripted turns, using `MockProvider`, and asserts on tool calls, actions, language, and grounding sources — including a red-team suite covering prompt injection, fabricated-policy requests, other-guest privacy leaks, and SQL-injection-shaped tool input. Because it's deterministic (no live LLM call), it runs in milliseconds and can gate every change — a much tighter loop than manually re-testing conversations by hand after every prompt tweak.

## A strict JSON-Schema type is a promise the model can break — even when your handler doesn't care

The mock suite never surfaced it (its rule-based provider always emits well-typed args), but the first real Groq call did: asked for "two guests," the model's tool call serialized `"guests": "2"` — a quoted string, not a JSON number. Groq validates tool-call arguments against the declared schema *before* the app's code ever runs, so a `type: "integer"` field rejected the call outright with a 400, even though `agent.py`'s own handler already does `int(args.get("guests") or 1)` and would have coerced it fine. The fix is at the schema, not the handler: widen it to `type: ["integer", "string"]`. General lesson — a tool schema's declared type is enforced by the provider, independent of how defensively your own code reads the argument; if a field is likely to arrive as a spelled-out number, loosen the schema rather than trusting downstream coercion to ever get a chance to run.

## A tool with no parameters can still send malformed arguments

Zero-argument tools (`end_call`, `transfer_to_human`) feel like they can't have an arguments bug — there's nothing to get wrong. Groq's Llama tool-calling proved that wrong: it emitted the literal JSON string `"null"` for `end_call` instead of `"{}"`. `json.loads("null")` decodes to Python `None`, which is not a dict, so a schema-validating guardrail (correctly) rejected it as "arguments were not an object." The costly part wasn't the rejection — it's that the model then retried the *identical* malformed call against the *identical* rejection six more times, burning the whole tool-round budget and forcing a transfer instead of the hangup the caller actually asked for. The fix is the same shape as the `guests` string/integer bug: normalize at the single boundary where the provider's JSON gets parsed (`args = {} if not isinstance(parsed, dict) else parsed`), not in each tool's handler. General lesson: "no parameters" is a claim about the tool's contract, not a guarantee about what a model will actually send — the parsing boundary should treat *anything* that isn't a dict as no arguments, not just handle the fields that happen to exist.

## "Serve the whole directory, block a few routes" inverts the safe default

`talk_server.py`'s `Handler` extended `SimpleHTTPRequestHandler` with `directory=ROOT` (the entire `livekit/` folder) and special-cased three routes (`/`, `/state`, `/token`) before falling through to the default static-file behavior for everything else. That's an *allow-by-default, deny-by-exception* posture — safe only as long as nothing sensitive ever lands anywhere in `ROOT`, which is a property of the moment, not a guarantee (one `cp .env.example .env` in `livekit/` and it's served over HTTP next to the demo page). The fix is the inversion: scope `directory` to only what must be public (`web/`) and vendor in anything the frontend needs from outside that tree — here, `livekit-client` had been imported straight from `/node_modules/...`, which only worked because the whole repo was exposed. Once serving was scoped down, that import would have 404'd, so the client bundle got copied into `web/vendor/` instead. General lesson: a directory-serving handler's safety is a function of everything that could ever exist in that directory, forever — allowlist the folder, don't denylist the routes.

## Trusting client-supplied identity for a token-minting endpoint defeats the token

The LiveKit `/token` endpoint took `identity`, `name`, and `room` straight from query params and minted a signed `AccessToken` with `room_join`/`can_publish`/`can_subscribe` grants for whatever was asked — meaning the token's cryptographic signature was protecting nothing, since anyone could request a valid token for any identity in any room. The actual frontend, though, only ever needed two fixed participants (`caller-demo`, `aurora-agent`) in one fixed room. Once that was true, the server had no reason to trust the client for those values at all — a `role=caller|agent` param mapped server-side to the fixed pair closes the hole with zero loss of functionality. General lesson: if an endpoint mints credentials, check whether the caller-supplied parameters are actually load-bearing for the legitimate use case before trusting them — often the real degrees of freedom are much narrower than the API surface suggests.

## A rate limit can impersonate a hang

Groq's free tier caps at 100,000 tokens/day, and it's easy to burn through during iterative live testing. When the cap hits, the OpenAI SDK's default retry behavior reads the server's `Retry-After` header and silently waits it out before finally raising — which, for a multi-minute backoff, looks exactly like the process froze: no error, no new telemetry, just an idle process still holding an open connection. The tell is telemetry, not the terminal: a turn that never appends its `turn.completed` record to the JSONL trace is still in flight, so before assuming a new infinite-loop bug, check for a 429 (or the reused/reopening TCP connection pattern) rather than start debugging the agent loop itself.
