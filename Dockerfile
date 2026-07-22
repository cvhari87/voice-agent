FROM python:3.11-slim

# portaudio19-dev + gcc: needed to build sounddevice/webrtcvad from
# pipeline/requirements.txt. Only voice_loop.py (local-mic mode) actually
# uses them; talk_server.py never imports them, but installing the
# requirements files as-is (matching the RUNBOOK preflight) is simpler and
# less likely to drift than hand-maintaining a deploy-only subset.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    portaudio19-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pipeline/requirements.txt pipeline/requirements.txt
COPY livekit/requirements.txt livekit/requirements.txt
RUN pip install --no-cache-dir -r pipeline/requirements.txt \
    && pip install --no-cache-dir -r livekit/requirements.txt

COPY pipeline pipeline
COPY livekit livekit
COPY knowledge knowledge

WORKDIR /app/livekit
CMD ["python3", "talk_server.py"]
