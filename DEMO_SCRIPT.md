# Demo Recording Script

Deployed URL: https://voice-agent-production-8dbe.up.railway.app
Access key (enter when prompted on page load): see `TALK_ACCESS_KEY` in Railway's Variables tab — never commit the real value here.

**Authenticate before you start recording, not after.** Open the URL and enter
the access key first — the key prompt is a plain browser `prompt()` (not a
password-masked field), so if it's on screen during a take, the key is
visible in the recording. Get past that prompt, *then* start your screen
recording. Also don't switch over to Railway's Variables tab mid-recording —
that's a much bigger exposure than the prompt itself.

## 1. Connect

- (Before recording) open the URL, enter the access key when prompted.
- Once past the prompt, start screen recording. Keep the transcript, sources,
  and telemetry/pipeline panels visible in frame throughout — that's the
  evidence the demo needs to show, not just the audio.
- Click **Start call** once (grants mic permission).
- Wait for both "Caller Demo" and "Aurora Agent" to show as joined.

## 2. Booking flow (core functionality)

Say: *"I need a room from August 12 to August 14 for two guests."*
- Expect: available rooms listed, sourced from `check_availability` (not invented).

Say: *"Book it for [your name] at [an email or phone]."*
- Expect: a confirmation with a booking ID once you confirm.
- If asked to choose a room type first, pick one, then confirm.

## 3. Policy grounding (RAG, with visible sources)

Say: *"What is the cancellation policy?"*
- Expect: a specific answer (not generic), and the **sources panel** shows
  `hotel_policies.md#Cancellation`. Keep that panel in frame — it's the proof
  the answer is grounded, not hallucinated.

Optionally also ask about pet policy or parking for a second grounded example.

## 4. Language switch (English → Spanish → English)

Say: *"Please speak Spanish."*
- Expect: agent confirms in Spanish.

Say (in Spanish): *"¿Cuál es la política de mascotas?"*
- Expect: grounded Spanish answer, sources panel shows the pet-policy source.

Say: *"Switch back to English."*
- Expect: agent confirms in English.

Say: *"¡Gracias!"*
- Expect: this must **not** flip the session back to Spanish — it's a courtesy
  phrase, not an explicit language request. Next answer stays in English.

## 5. Barge-in (interrupt mid-response)

Ask a longer question (e.g., cancellation policy again, or list room options).
While the agent is mid-response, start talking over it.
- Expect: playback stops, and the transcript/events panel shows
  **"Caller interrupted agent playback"**. Point the camera/cursor at this —
  it's real, driven by your mic audio, not scripted.

## 6. Wrap up

Say: *"Goodbye."*
- Expect: a proper closing line and the call ends cleanly (hangup, not a
  transfer).

## 7. Telemetry callout (can do this last, narrated over the UI)

Point out the pipeline/timing panel updating per turn (STT/LLM/tools/TTS
timings), and mention that every turn is also logged server-side to a
structured JSONL trace (`logs/voice-events.jsonl`) with tool calls, grounding
sources, and guardrail decisions — used for the eval suite and debugging, not
shown in the recording itself.

## Known caveats to mention (or just be aware of, don't dwell)

- Groq's own TTS voice may fail in production (preview/ToS limitation on
  Groq's side) — the app automatically falls back to the browser's built-in
  voice, so this is expected, not a bug, if it happens.
- If a response feels slightly generic on a short input like "Goodbye" alone,
  that's live-model variability, not a functional issue — the important thing
  is the *action* (hangup/transfer/booking) resolves correctly, which has been
  verified separately.
