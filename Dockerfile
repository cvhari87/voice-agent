FROM python:3.11-slim

# talk_server.py's actual import chain only needs requirements-server.txt
# (see that file for why) -- no system packages or compilation needed, so
# builds are fast and the image stays small. pipeline/requirements.txt and
# livekit/requirements.txt (with sounddevice/webrtcvad/numpy/python-dotenv
# for voice_loop.py's local-mic mode) are NOT installed here; they remain
# the RUNBOOK's documented local-dev preflight sets, unrelated to this image.
WORKDIR /app

COPY requirements-server.txt requirements-server.txt
RUN pip install --no-cache-dir -r requirements-server.txt

COPY pipeline pipeline
COPY livekit livekit
COPY knowledge knowledge

WORKDIR /app/livekit
CMD ["python3", "talk_server.py"]
