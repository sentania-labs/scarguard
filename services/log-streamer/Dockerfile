# Build context: repo root
# docker build -f services/log-streamer/Dockerfile -t scarguard-log-streamer .
#
# Lightweight sidecar that tails Docker container logs and publishes them
# to Redis.  Replaces the Docker socket mount previously needed by the
# web container for the admin logs tab.
# v0.13.4: authenticated Docker Hub pull via DOCKER_HUB_USERNAME/_API_KEY
# org secrets (docker/login-action step in build.yml) — per-user quota
# (5000/day) instead of the anonymous-IP quota (~100/6h).
FROM docker.io/library/python:3.11-slim@sha256:233de06753d30d120b1a3ce359d8d3be8bda78524cd8f520c99883bfe33964cf

WORKDIR /app

RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

COPY services/log-streamer/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

COPY services/log-streamer/src/ ./src/

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

CMD ["python3", "src/main.py"]
