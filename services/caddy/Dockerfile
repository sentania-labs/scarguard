# Caddy reverse proxy with Python for config parsing.
# Reads tls settings from scarguard.yml and generates a Caddyfile.
# v0.13.4: authenticated Docker Hub pull via DOCKER_HUB_USERNAME/_API_KEY
# org secrets (docker/login-action step in build.yml) — per-user quota
# (5000/day) instead of the anonymous-IP quota (~100/6h).
FROM docker.io/library/caddy:2-alpine@sha256:fce4f15aad23222c0ac78a1220adf63bae7b94355d5ea28eee53910624acedfa

RUN apk add --no-cache python3 py3-yaml

COPY config/caddy-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
