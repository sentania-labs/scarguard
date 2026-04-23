"""ScarGuard web service — startup script.

Starts uvicorn on HTTP port 8080.  TLS termination is handled by the Caddy
reverse proxy container — see the tls section in scarguard.yml.
"""

import logging
import os
import sys

import uvicorn
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("start")

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/scarguard.yml")


def _load_cfg() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
        return cfg if isinstance(cfg, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.warning("Could not read %s: %s", CONFIG_PATH, exc)
        return {}


def main() -> None:
    cfg = _load_cfg()
    log_level = cfg.get("system", {}).get("log_level", "INFO")
    # v1.14: trust X-Forwarded-* headers only from the Docker bridge
    # ranges that Caddy actually lives on. Pre-v1.14 was "*" which was
    # safe under the assumption "only Caddy can reach :8080" — but that
    # assumption breaks the moment someone adds a port binding for
    # debugging or runs a second ingress. Override via env if your Docker
    # network uses non-default subnets.
    trusted_proxies = os.environ.get(
        "SCARGUARD_TRUSTED_PROXIES",
        "127.0.0.1,172.16.0.0/12,10.0.0.0/8,192.168.0.0/16",
    )
    proxy_count = len([p for p in trusted_proxies.split(",") if p.strip()])
    log.info(
        "Starting HTTP on port 8080 (TLS handled by Caddy; %d trusted proxy range(s) configured)",
        proxy_count,
    )
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8080,
        log_config=None,
        log_level=log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips=trusted_proxies,
    )


if __name__ == "__main__":
    main()
