# What I Did — Aurora Hotel Voice Agent Assignment

Working log and plan for Assignment 2 (Voice Agent). Due Wednesday; target finish Tuesday.

## Completed

1. **Cloned the assignment.** Pulled `FDE/Assignment_2_voice_agent` from `hamzafarooq/multi-agent-course` (the local checkout was 4 commits behind `origin/main`, which is where this assignment actually lives) and copied it into `Voice Agent/` as its own working folder.
2. **Read the docs.** Went through `README.md` and `RUNBOOK.md` end to end — the project is a progressive workshop build: `caller audio -> VAD/endpointing -> STT -> AgentRouter -> LLM -> RAG/tools -> TTS`, staged across 10 RUNBOOK stages from a deterministic text agent up to LiveKit, telemetry, evals, and SIP mapping.
3. **Read all the source.** Reviewed `pipeline/` (`agent.py`, `voice_loop.py`, `providers.py`, `router.py`, `knowledge.py`, `telemetry.py`), the LiveKit bridge (`talk_server.py`, `create_room.py`), the full browser frontend (`web/talk.js`, `web/index.html`, `web/styles.css`), the mocks (`demo_call.py`, `ivr_menu_mock.py`), and the eval harness (`run_evals.py`, `core.json`, `red_team.json`).
4. **Verified everything offline, for real** (not just by reading):
   - `python3 smoke_test.py` → PASS
   - `python3 -m unittest -v test_features.py` (pipeline) → 16/16 OK
   - `python3 -m unittest -v test_env_loader.py` (livekit) → 4/4 OK
   - `python3 run_evals.py --suite all` → 12/12 scenarios passed (core + red-team)
   - `python3 scale_check.py --dau 1000000` → matches the RUNBOOK's stated ~5,556 peak concurrency
   - `mocks/demo_call.py`, `demo_call.py --transfer`, `ivr_menu_mock.py` → all ran cleanly
5. **Checked the local environment.** Python 3.9.6, Node/npm present, **Docker not installed** (needed for the local LiveKit dev server in Stage 5 — flagged as the first real blocker).
6. **Answered the five scoping questions** (steps to complete, base data, frontend/backend readiness, bugs, deployment target) using the verified evidence above rather than guesswork.
7. **Confirmed assignment scope with the user:** no separate rubric/AGENTS.md from the instructor (unlike Assignment 1, which ships `rubric.json`/`eval.py`/`AGENTS.md`); a demo is mandatory; a **public deployment is required**.
8. **Weighed deployment options.** Compared Railway, Fly.io, and Vercel for the app tier, and flagged that the local Docker LiveKit dev server (`devkey`/`secret`, single node, no TURN) is not deployable as-is. Recommended: **Railway** for `talk_server.py` + the web frontend (user already has a hobby plan; matches the app's stateful long-running-process shape) and **LiveKit Cloud free tier** to replace the local Docker room server (removes the hard UDP/ICE/TURN problem from the deployment). Vercel ruled out — wrong shape for a stateful realtime bridge server.
9. **Installed Docker Desktop.** `brew install --cask docker` (needed the user's Mac password interactively for the privileged-helper install), then launched Docker.app to complete first-run setup. Verified the daemon is up with `docker info` (`Context: desktop-linux`).
10. **Pulled the LiveKit server image** (`docker pull livekit/livekit-server:latest`) ahead of time so Stage 5 isn't blocked on a slow first pull later.

## Remaining / Next Steps

- [x] Install Docker (Sunday) so the local LiveKit demo can even run before we touch deployment.
- [x] Pull `livekit/livekit-server:latest` early so Stage 5 isn't blocked later.
- [x] Get a free Groq API key; set `PROVIDER=groq` in `pipeline/.env` for cost-free live testing (`TTS_BACKEND` was already `system` from the template).
- [x] Created `pipeline/.venv` and `livekit/.venv` per the RUNBOOK preflight (previous offline verification had run straight on system Python); reran smoke test and 16/16 unittest — still green.
- [x] Set up `livekit/` deps (`pip install -r requirements.txt`, `npm install`) — clean install, 0 vulnerabilities; `livekit/.venv` unittest 4/4 green.
- [x] Walk RUNBOOK Stage 2 (live provider) in text mode — **found and fixed a real bug**: Groq's Llama tool-calling emits spelled-out numbers ("two guests") as a quoted string (`"guests": "2"`), which the `check_availability`/`create_booking` tool schemas (`type: integer`) rejected outright as a 400, crashing every live booking attempt. Fixed by widening both schemas to `type: ["integer", "string"]` in `agent.py` (the handlers already coerce via `int()`/interpolation downstream, so no other code changes needed). Confirmed fix holds — reran the same live request 3x clean.
- [x] Hit the **Groq free-tier daily token cap** (100,000 TPD) partway through Stage 2 testing — further live calls 429 until the quota window resets. (Note: the SDK's retry-with-backoff silently honors the server's `Retry-After`, which looks like a hang, not an error — don't mistake that for a new bug if it recurs.)
- [x] Walked RUNBOOK Stage 3 (tools/RAG/guardrails/language routing) — both in mock mode (unaffected by the Groq quota, since `run_evals.py` hardcodes `PROVIDER=mock`) and via the core eval suite: 7/7 scenarios pass, including `router.language_switch`. Full Spanish/English switching walkthrough behaved exactly per the RUNBOOK's expected evidence.
- [x] Ran Stage 8 red-team suite: 5/5 scenarios pass (prompt injection, fabricated policy, privacy, SQL-injection-shaped tool input, guardrails-after-language-switch).
- [x] Reran Stage 9 scale check and Stage 10 SIP mocks (`demo_call.py`, `--transfer`, `ivr_menu_mock.py`) in the new venv — all clean, matches prior verification.
- [x] **Ran a security/release review** (ultrareview-style) against the codebase ahead of the required public deployment. Two P0s and three cheap-but-important P1s addressed so far (see below); remaining items (HTTP body-size/session limits, sanitized error responses, dependency pinning, "demo only" UI banner) are deferred to a later pass before the actual deploy.
- [x] **Fixed the two P0 findings in `talk_server.py`:**
  - `GET /token` used to mint a valid LiveKit token for *any* client-supplied `identity`/`room` with full publish/subscribe grants — no auth at all. Fixed: identity/room are now server-controlled (`role=caller|agent` maps to the two fixed demo participants; arbitrary values are rejected with 400), tokens now carry a 1-hour TTL, and an optional `TALK_ACCESS_KEY` env var gates the endpoint with a shared secret (unset = no gate, for local dev).
  - The HTTP handler served the entire `livekit/` directory as static files (would have exposed `talk_server.py` source now, and `livekit/.env` the moment one existed). Fixed: vendored the LiveKit client bundle into `web/vendor/livekit-client.esm.mjs` (no more loading from `/node_modules/...`) and scoped static serving to `web/` only. Verified with curl: `.env`/`.py` paths now 404, `role`-based token minting still works, the access-key gate correctly 401s on missing/wrong keys.
- [x] **Codex (running in parallel) implemented the other three "cheap and worth it" findings** as `pipeline/guardrails.py`, wired into `agent.py`'s tool loop: deterministic pre-LLM emergency detection (multilingual, with a fireplace/smoke-alarm-policy false-positive guard), a privacy check blocking other-guest info requests before the LLM ever sees them, a tool-round cap (max 6) that transfers to the front desk instead of spinning forever, and real booking validation (guest-count bounds, room-type allowlist, contact regex, requires explicit confirmation + prior availability check, cross-turn confirmation, duplicate-booking fingerprinting) plus an audit log with PII hashed out. Verified: full suite still green after both streams of changes landed — 16/16 unittest, smoke test, and evals now **14/14** (two new cases: `safety.fire_emergency`, `safety.fireplace_false_positive`).
- [ ] **Resume live-provider testing** once the Groq quota resets: Stage 2 re-walkthrough with the schema fix, then Stage 4 (local voice cascade, real STT) and Stage 6 (turn-taking/barge-in) which need a live provider for realistic responses.
- [ ] Run the full local LiveKit room demo (Stages 5–6): turn-taking, barge-in, endpoint-silence tuning.
- [ ] Inspect telemetry (`logs/voice-events.jsonl`) live during the LiveKit room demo; optionally add one new red-team eval case per the RUNBOOK's own suggestion.
- [ ] **Deployment work:**
  - [ ] Create a LiveKit Cloud project (free tier); swap `LIVEKIT_URL`/`LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` from local dev defaults to the cloud project's values.
  - [ ] Write a `Dockerfile` that installs `pipeline/requirements.txt` + `livekit/requirements.txt` (frontend no longer needs `npm install`/`node_modules` at runtime — `livekit-client` is now vendored into `web/vendor/`).
  - [ ] Set `TALK_ACCESS_KEY` as a Railway secret env var so the public `/token` endpoint isn't wide open (see security fixes above).
  - [ ] Fix `talk_server.py` to bind `0.0.0.0` and read Railway's `$PORT` instead of the hardcoded local host/port.
  - [ ] Set `PROVIDER`, the provider API key, and `TTS_BACKEND=provider` (or leave browser TTS) as Railway secret env vars — never commit real keys.
  - [ ] Deploy to Railway; confirm HTTPS (required for browser mic access) and that the deployed demo works end-to-end from a phone/other machine.
- [ ] Record the demo (booking flow, policy grounding with sources, English/Spanish switch, barge-in, telemetry panel).
- [ ] Final re-run of smoke test + unittest + evals before submitting; commit and push.

## Timeline

| Day | Focus |
|-----|-------|
| Sun (today) | Docker install, provider key, offline verification, text-mode walkthrough |
| Mon | Local LiveKit demo, telemetry, red-team, deployment setup (LiveKit Cloud + Railway) |
| Tue | Deploy verification, demo recording, final checks, submit (buffer before Wed due date) |
