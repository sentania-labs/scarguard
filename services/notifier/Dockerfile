# Build context: repo root
# docker build -f services/notifier/Dockerfile -t scarguard-notifier .
FROM python:3.11-slim

WORKDIR /app

COPY services/notifier/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

COPY services/notifier/src/ ./src/
COPY shared/ ./shared/

ENV PYTHONPATH=/app/src:/app/shared
ENV PYTHONUNBUFFERED=1

# Run as non-root for defense in depth
RUN useradd -r -s /usr/sbin/nologin scarguard && \
    mkdir -p /data /config /var/lib/scarguard && \
    chown scarguard:scarguard /data /config /var/lib/scarguard
USER scarguard

CMD ["python", "src/main.py"]
