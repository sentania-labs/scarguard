# Build context: repo root
# docker build -f services/backup/Dockerfile -t scarguard-backup .
#
# v1.14: SQLite online-backup sidecar.
# Reads backup config from scarguard.yml; backs up each SQLite DB to
# /data/backups/{db_name}/{YYYY-MM-DD}.db.gz on a configurable schedule.
# Subscribes to scarguard:backup:trigger for manual web-UI triggers and
# publishes scarguard:backup:status so the UI can surface backup state.
FROM docker.io/library/python:3.11-slim@sha256:233de06753d30d120b1a3ce359d8d3be8bda78524cd8f520c99883bfe33964cf

WORKDIR /app

RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

COPY services/backup/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

COPY services/backup/src/ ./src/
COPY shared/ ./shared/

ENV PYTHONPATH=/app/src:/app/shared
ENV PYTHONUNBUFFERED=1

# Non-root. Needs to read /config (ro) and read+write /data (rw).
# UID/GID must match the other services (web/detector/notifier/deterrent):
# those chown /data to uid 999 at startup; if this sidecar is a different
# uid it can't write /data/backups.
RUN addgroup --system --gid 999 scarguard \
    && adduser --system --uid 999 --ingroup scarguard scarguard
USER scarguard

CMD ["python3", "src/main.py"]
