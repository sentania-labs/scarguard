# Caddy reverse proxy with Python for config parsing.
# Reads tls settings from scarguard.yml and generates a Caddyfile.
FROM caddy:2-alpine@sha256:fce4f15aad23222c0ac78a1220adf63bae7b94355d5ea28eee53910624acedfa

RUN apk add --no-cache python3 py3-yaml

COPY config/caddy-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
