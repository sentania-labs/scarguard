# Caddy reverse proxy with Python for config parsing.
# Reads tls settings from scarguard.yml and generates a Caddyfile.
# v0.13.4: authenticated Docker Hub pull via DOCKER_HUB_USERNAME/_API_KEY
# org secrets (docker/login-action step in build.yml) - per-user quota
# (5000/day) instead of the anonymous-IP quota (~100/6h).
# v1.14.2: runs as non-root uid 999 (matches the scarguard user on the
# other services). The upstream caddy:2-alpine image already ships
# /usr/bin/caddy with cap_net_bind_service=ep, so the non-root user can
# still bind :80/:443 (combined with `cap_add: NET_BIND_SERVICE` in
# docker-compose.yml to keep the cap in the bounding set after
# cap_drop: ALL). We don't add a named user - gid 999 is already taken
# by Alpine's `ping` group. The process just runs as the numeric uid/gid.
FROM docker.io/library/caddy:2-alpine@sha256:d8542f48d34a9cf4e4c11a478865229840e87e4c96ea3f439101f31a5d35f75f

RUN apk add --no-cache python3 py3-yaml \
    && chown -R 999:999 /etc/caddy /data

COPY --chown=999:999 config/caddy-entrypoint.sh /entrypoint.sh
# Caddyfile renderer/validator and the tls value rules it shares with web.
COPY --chown=999:999 config/caddy_config.py shared/tls_safety.py /usr/local/lib/scarguard/
RUN chmod +x /entrypoint.sh

USER 999:999

ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
