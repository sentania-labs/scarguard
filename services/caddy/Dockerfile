# Caddy reverse proxy with Python for config parsing.
# Reads tls settings from scarguard.yml and generates a Caddyfile.
FROM caddy:2-alpine

RUN apk add --no-cache python3 py3-yaml

COPY config/caddy-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
