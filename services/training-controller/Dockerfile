# Narrow lifecycle boundary for hard detector GPU isolation.
FROM docker.io/library/python:3.11-slim@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89

WORKDIR /app

# Apply Debian security updates on top of the digest-pinned base, matching
# every other Python service Dockerfile here.  This file was the only one
# missing the step, so the image shipped the base's unpatched OS packages and
# the Trivy gate failed on them (util-linux CVE-2026-53612/53613/53614/53615).
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

# The base image's setuptools/wheel carry fixable HIGH CVEs (Trivy-gated),
# so upgrade the build tooling before installing the controller's pinned YAML
# parser.
COPY services/training-controller/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r /tmp/requirements.lock \
    && python3 -m pip uninstall -y pip setuptools wheel

COPY services/training-controller/src/ ./src/

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

CMD ["python3", "src/main.py"]
