# Narrow lifecycle boundary for hard detector GPU isolation.
FROM docker.io/library/python:3.11-slim@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89

WORKDIR /app

# Apply Debian security updates on top of the digest-pinned base, matching
# every other Python service Dockerfile here.  This file was the only one
# missing the step, so the image shipped the base's unpatched OS packages and
# the Trivy gate failed on them (util-linux CVE-2026-53612/53613/53614/53615).
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

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

# v1.15 (FDY-0558): Add non-root user matching the docker-compose uid 999
# so the container drops privileges before startup.  The /state volume mount
# must be writable by uid 999 at runtime; compose passes that in.
RUN addgroup --system --gid 999 scarguard \
    && adduser --system --uid 999 --ingroup scarguard scarguard

# v1.15 (FDY-0558): Entrypoint that migrates legacy root-owned /state
# directories from prior (pre-v1.15) controller installations.  On upgrade,
# the named volume ``scarguard-training-controller-state`` hides the
# image-layer ``/state`` directory and retains its original root ownership.
# This wrapper runs as root, chowns the volume mount, then exec's the
# non-root controller process.
COPY services/training-controller/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python3", "src/main.py"]
