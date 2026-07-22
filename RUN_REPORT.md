# Aurora Voice Agent — Run Report

**Date:** 2026-07-22 · **Host:** macOS (Darwin) · **Python:** 3.9.6 (`pipeline/.venv`, `livekit/.venv`)
**Scope:** Local live-provider testing (Groq → OpenAI), three rounds of adversarial guardrail hardening, full public deployment (Railway + LiveKit Cloud), and a recorded browser demo.

Unlike a pure local text-mode report, this run includes an actual **public deployment** with a real LiveKit room, real STT/TTS through the browser, and a recorded demo — not just `voice_loop.py --text`.

## Run configuration & decisions

- **Local testing:** `voice_loop.py --text` (RUNBOOK Stage 2) for provider/tool-calling verification; drives the same agent state, router, RAG, and tools as voice mode.
- **Providers:** started with **Groq** (`llama-3.3-70b-versatile`) for local and initial deployed testing. Groq's free-tier daily cap (100,000 TPD) was exhausted repeatedly during iterative testing (confirmed via `openai.RateLimitError` in telemetry, not a code bug). Switched production to **OpenAI** (`gpt-4o-mini` / `whisper-1` / `tts-1`) — same codebase, zero code changes, since both providers speak the OpenAI-compatible API dialect this project's `Provider` class is built around.
- **TTS:** `TTS_BACKEND=system` (routes to browser TTS) in the deployed environment — OpenAI's `tts-1` added ~3s of network latency per turn (total turn time ~6s); switching to browser TTS cut it to ~1.4s. Also avoids Groq's `canopylabs/orpheus-v1-english` TTS, which 400s in production (a preview-tier limitation on Groq's side, not ours) — the app's existing fallback already handles this gracefully.
- **Telemetry:** per-turn JSONL trace (`logs/voice-events.jsonl`), default redaction (`TELEMETRY_INCLUDE_CONTENT=false`). A second audit trail, `logs/guardrail-memory.jsonl`, records every guardrail decision (redacted/hashed sensitive fields, `0600` permissions).
- No real secrets are committed. `pipeline/.env`/`livekit/.env`/venvs are gitignored. One incident: `TALK_ACCESS_KEY`'s real value was briefly committed in `DEMO_SCRIPT.md`; rotated in Railway immediately and the doc replaced with a placeholder (see Caveats).

## Preflight (offline, free) — ✅

| Check | Result |
|---|---|
| `python3 smoke_test.py` | **PASS** (availability → booking → transfer → hangup) |
| `python3 -m unittest test_features.py test_guardrails.py` (pipeline) | **74/74** |
| `python3 -m unittest test_env_loader.py test_talk_server.py` (livekit) | **10/10** |
| `python3 run_evals.py --suite all` (core + red-team) | **14/14** |
| `python3 scale_check.py --dau 1000000` | matches RUNBOOK's ~5,556 peak concurrency |
| `mocks/demo_call.py`, `--transfer`, `ivr_menu_mock.py` | all clean |

---

## Run 1 — Live-provider bug discovery (Groq, `llama-3.3-70b-versatile`)

Four real, reproducible bugs surfaced during live testing — none visible in the mock-based test suite, since `MockProvider`'s deterministic rule-based responses can't reproduce a live model's generation quirks.

| # | Scenario | Symptom | Root cause | Fix |
|---|---|---|---|---|
| 1 | "…for two guests" | `400 tool_use_failed` on every booking attempt | Groq serializes spelled-out numbers as a JSON string (`"guests": "2"`) against a `type: integer` schema | Widen schema to `["integer", "string"]` in `agent.py` |
| 2 | "Goodbye" (fresh session) | `end_call` retried 7x, force-transferred instead of hangup | Groq emits literal JSON `"null"` for a zero-argument tool call; `json.loads("null")` → `None`, rejected as "not an object" | Normalize non-dict parsed arguments to `{}` at the JSON-parsing boundary |
| 3 | "Goodbye" (post-fix #2) | `end_call` succeeds once (`action: hangup`), then gets re-invoked 6 more times anyway, still force-transfers | Nothing stopped the model from redundantly re-calling a terminal tool once it had already fired | Force `tool_choice="none"` on the next model call once a terminal action is set |
| 4 | "Please speak Spanish." | Raw `openai.BadRequestError` displayed to the caller as a crash | Groq occasionally emits malformed native function-call syntax (missing `>` before JSON args) | Wrap the LLM-call boundary in try/except, retry once (non-deterministic failure), degrade to an apologetic reply only if both attempts fail |

Each bug was reproduced live via direct API calls (not inferred), root-caused via the embedded telemetry trace, fixed, and covered by a permanent regression test using a stub provider that reproduces the exact observed behavior (so re-verification never needs a live call or provider quota).

## Run 2 — Live-provider correctness (OpenAI, `gpt-4o-mini` / `whisper-1`)

Same scenarios re-verified cleanly against production after switching providers:

| Scenario | Result |
|---|---|
| "What is the cancellation policy?" | Correctly grounded — `sources: ["hotel_policies.md#Cancellation"]` |
| "Can you speak in Spanish instead?" | `language: es`, correct Spanish reply, no errors — the exact scenario that failed repeatedly on Groq (Run 1, #4) |
| Booking flow (5 guests → room match) | Correctly matched to Family Double Queen (respects room-capacity validation), real Whisper STT transcription |
| Latency | LLM ~0.9–1.9s per turn; total turn time dropped ~6s → ~1.4s after switching TTS to browser-side |

---

## Security/guardrail hardening — three rounds of adversarial review

All findings below were independently verified by direct reproduction (not taken on faith) before and after each fix.

**Round 1 — P0 deployment vulnerabilities** (`talk_server.py`):
- `GET /token` minted a valid LiveKit token for *any* client-supplied identity/room, no auth. Fixed: server-controlled `role=caller|agent` mapping, 1-hour token TTL, optional `TALK_ACCESS_KEY` gate (later hardened to header-only transport — query strings leak into browser history/access logs).
- The HTTP handler served the entire `livekit/` directory as static files. Fixed: vendored `livekit-client` into `web/vendor/`, scoped serving to `web/` only. Verified: `.env`/`.py` paths 404 in production.

**Round 2 — `pipeline/guardrails.py` (deterministic safety layer, built alongside a parallel Codex session):**
Emergency detection (clause-splitting, negation-aware), privacy blocking (other-guest info requests), a 6-round tool-call cap, booking validation (date parsing, room-capacity limits, confirmation sequencing, duplicate-booking fingerprinting), and an output guardrail (system-prompt leakage, injection markers, fabricated confirmation codes).

**Round 3 — Adversarial negation testing found real bypasses in that layer, fixed across two follow-up passes:**
| Bypass phrase | Bug | Fix |
|---|---|---|
| `"I do not have a phone and there is smoke in my room"` | Whole-clause negation suppressed an unrelated real emergency | Negation checked in a small word-window before the *specific* matched term |
| `"Yes, but do not book it"` | A bare "yes" satisfied confirmation regardless of what followed | Explicit rejection anywhere in the utterance overrides an earlier bare agreement |
| `"Yes, but cancel that"` / `"stop the booking"` | No recognized confirmation word to negate — nothing for the proximity check to attach to | Standalone-rejection-phrase list + broadened action-term set (`booking`/`reservation`) |
| `"The fire alarm policy is fine but there is a fire"` | Benign-context check suppressed the *whole clause*, not just the overlapping term | Per-occurrence span checking (`re.finditer`, not `re.search`) |
| `"There is no fire"` | Bare "no" was never in the negation vocabulary | Added it; made all negation matching word-boundary-safe |

Every fix was re-verified against all previously-reported bypass phrases (no regressions) plus adversarial cases invented specifically to probe the new logic. Pipeline test count grew from 16 → **74** across this hardening work.

**Separately:** the STT pipeline was found emitting confusing replies to empty/silent audio and (once, live) a documented Whisper hallucination artifact (`"Más información www.alimmenta.com"` — Whisper models are known to emit website/subtitle-credit text for non-speech audio). Fixed with a filter mirroring the existing barge-in echo-suppression pattern.

---

## Deployment

- **Public URL:** https://voice-agent-production-8dbe.up.railway.app (Railway, deployed from GitHub via Docker)
- **Media layer:** LiveKit Cloud project (replaces the local Docker dev server; Agent Observability deliberately disabled — would ship real caller audio to a third party, inconsistent with this app's own telemetry-redaction defaults)
- **Verified end-to-end via direct API calls against production:** auth gate (401 on missing/wrong key, 200 on correct), static asset scoping (path traversal to `.env`/`.py` blocked), a real grounded Groq/OpenAI turn, the header-only access-key transport, and the negation-fix guardrail decisions (`category: emergency` for the reported bypass phrases, confirmed live post-deploy each time)
- **Image:** trimmed after a slow (~7 min) rebuild — `talk_server.py`'s actual import chain never needed `sounddevice`/`webrtcvad`/`numpy`/`python-dotenv` (only the local-mic CLI path does); dropped those plus the `apt-get build-essential`/`portaudio19-dev` step entirely. Rebuilds now take under 2 minutes.
- **Recorded demo:** booking flow, policy grounding with visible sources, English↔Spanish switching, and barge-in (`"Caller interrupted agent playback"`, driven by real mic audio, not scripted) — per `DEMO_SCRIPT.md`.

## What the runs confirm

- **Single adapter, provider parity.** Same agent code, tools, guardrails, and hangup→SIP-BYE path across Groq and OpenAI — only the provider line changes.
- **Hybrid tool routing.** High-confidence policy questions force `search_hotel_knowledge` before the first model call, grounded in `hotel_policies.md#…`, surviving a prior off-topic refusal.
- **Guardrails hold under adversarial testing**, after three rounds of fixes — emergency detection, privacy blocking, and booking-consent all resist the specific bypass phrasings a security review generated, not just the originally-reported ones.
- **Language routing correct.** Explicit switches fire `set_language` + `router.language_changed`; courtesy phrases ("¡Gracias!") don't falsely trigger a switch.
- **Booking is auditable state via tool**, not model narration — confirmation IDs are tool-generated and validated against a state machine (availability checked → summary presented → confirmed → booked), with duplicate-booking fingerprinting.
- **Telemetry & redaction hold** across both providers and the deployed environment — every turn traced with `traceId`/`sessionId`/`turnId`, tool args/results, sources, and guardrail decisions; sensitive fields redacted by default.

## Observations & caveats

- **Groq's free tier is unreliable for iterative live testing.** The 100K TPD cap was exhausted multiple times during this session, sometimes within minutes of resuming — not a reflection of demo-scale usage, but a real constraint worth knowing before relying on Groq for a live-graded demo.
- **Acoustic echo can mimic a code bug.** A recording session produced garbled real-Whisper transcripts because the agent's own browser-TTS playback was picked up by the mic; every *actually* well-formed utterance in the same session was handled correctly — the fix was headphones, not code.
- **Deliberately deferred** (lower-visibility than what was fixed, explicitly disclosed rather than silently skipped): "any model output advances booking-summary-presented state" (a bare "Okay." satisfies the gate), unparseable/past dates pass availability and booking validation, `talk_server.py` still returns raw exception text in three spots, no session cap/TTL on `_agent_sessions`, auth fails open if `TALK_ACCESS_KEY` is unset, dependencies remain unpinned.
- **Leaked-key incident.** The real `TALK_ACCESS_KEY` value was briefly committed to `DEMO_SCRIPT.md` and pushed to the public repo. Rotated in Railway immediately (the action that actually neutralizes an exposed secret, independent of any git operation); the doc now references the Variables tab instead of a literal value.

## Artifacts

- `logs/voice-events.jsonl` — turn-by-turn telemetry (git-ignored)
- `logs/guardrail-memory.jsonl` — guardrail decision audit trail (git-ignored, `0600`)
- `Learnings.md` — technical writeups of every bug found, with root cause and general lesson
- `What I Did.md` — full working log, decision-by-decision
- `DEMO_SCRIPT.md` — the recording script/checklist used for the submitted demo
- GitHub repo: https://github.com/cvhari87/voice-agent (commit history documents each fix individually, in order)
