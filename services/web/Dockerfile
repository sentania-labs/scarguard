# Build context: repo root
FROM python:3.11-slim

WORKDIR /app

COPY services/web/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY services/web/src/ ./src/
COPY shared/ ./shared/
COPY VERSION ./VERSION

# Build-time metadata injected by CI; exposed as env vars at runtime.
ARG GIT_COMMIT=unknown
ARG BUILD_DATE=unknown
ENV GIT_COMMIT=${GIT_COMMIT}
ENV BUILD_DATE=${BUILD_DATE}

ENV PYTHONPATH=/app/src:/app/shared
ENV PYTHONUNBUFFERED=1

# Uvicorn on 8080; override with ENV in compose if needed
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--app-dir", "src"]
