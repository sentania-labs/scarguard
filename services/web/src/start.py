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
    log.info("Starting HTTP on port 8080 (TLS handled by Caddy reverse proxy)")
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8080,
        log_config=None,
        log_level=log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",  # Safe: only Caddy can reach port 8080 (no host port binding)
    )


if __name__ == "__main__":
    main()
