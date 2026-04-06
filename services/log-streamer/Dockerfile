# Build context: repo root
# docker build -f services/log-streamer/Dockerfile -t scarguard-log-streamer .
#
# Lightweight sidecar that tails Docker container logs and publishes them
# to Redis.  Replaces the Docker socket mount previously needed by the
# web container for the admin logs tab.
FROM python:3.11-slim@sha256:9358444059ed78e2975ada2c189f1c1a3144a5dab6f35bff8c981afb38946634

WORKDIR /app

COPY services/log-streamer/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

COPY services/log-streamer/src/ ./src/

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

CMD ["python3", "src/main.py"]
