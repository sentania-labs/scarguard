# Build context: repo root
# docker build -f services/notifier/Dockerfile -t scarguard-notifier .
FROM python:3.11-slim

WORKDIR /app

COPY services/notifier/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip wheel jaraco.context && \
    pip install --no-cache-dir -r requirements.txt

COPY services/notifier/src/ ./src/
COPY shared/ ./shared/

ENV PYTHONPATH=/app/src:/app/shared
ENV PYTHONUNBUFFERED=1

CMD ["python", "src/main.py"]
