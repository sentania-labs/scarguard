FROM ubuntu:22.04@sha256:2edbbc5dc405e9612ba3584ce95480277e3eb374407b5505fe26f17df77c7dbc
# Note: Using 22.04 to match JetPack 6.x userspace (Ubuntu 22.04 Jammy)
ENV DEBIAN_FRONTEND=noninteractive

# Base deps + Docker CLI (for issuing commands to host Docker via socket mount)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    docker.io \
    git \
    jq \
    libicu70 \
    libssl3 \
    python3 \
    python3-pip \
    python3-venv \
    wget \
    lsb-release \
 && rm -rf /var/lib/apt/lists/*

# --- Trust internal CA ---
# Place your sentania Lab Root CA cert alongside this Dockerfile
# IMPORTANT: Must be PEM-encoded with .crt extension
COPY ["sentania Lab Root 2.crt", "/usr/local/share/ca-certificates/sentania-lab-root-2.crt"]
RUN update-ca-certificates

ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
ENV GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt
ENV CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

# Non-root user for runner (match host user UID if needed)
ARG RUNNER_UID=994
ARG GROUP_GID=994
RUN groupmod -g ${GROUP_GID} docker \
 && useradd -m -u ${RUNNER_UID} runner \
 && usermod -aG docker runner
WORKDIR /home/runner

# GitHub Actions runner (ARM64).
# Latest: https://github.com/actions/runner/releases
#
# THIS IS A FIRST-BOOT SEED ONLY. Do not expect bumping it to upgrade the
# running runner.
#
# docker-compose.runner.yml mounts the persistent `runner-data` volume over
# /home/runner, which is where this download lands. Docker populates a named
# volume from the image ONCE, when the volume is empty; after that the volume
# masks whatever the image contains. So a recreated container keeps using the
# runner already in the volume and never sees this version.
#
# That is fine, because the runner SELF-UPDATES: the volume here was seeded
# at 2.330.0 and had already pulled 2.332.0 into bin.2.332.0 on its own.
# Chasing the version from the Dockerfile would duplicate a mechanism that
# already works, and the only ways to force it are deleting the volume
# (which discards registration) or having the entrypoint migrate binaries
# into it (machinery for no gain).
#
# Bumped anyway so a NEW volume starts close to current instead of
# downloading a year-old runner and then immediately self-updating.
ENV RUNNER_VERSION=2.337.0
RUN curl -L -O https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-arm64-${RUNNER_VERSION}.tar.gz \
 && tar xzf actions-runner-linux-arm64-${RUNNER_VERSION}.tar.gz \
 && rm actions-runner-linux-arm64-${RUNNER_VERSION}.tar.gz \
 && ./bin/installdependencies.sh

# Entrypoint script handles runner registration and startup
COPY --chmod=0755 entrypoint.sh /opt/entrypoint.sh

USER runner
ENTRYPOINT ["/opt/entrypoint.sh"]
