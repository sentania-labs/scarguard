"""ScarGuard config-API service — startup script.

Starts uvicorn on HTTP port 8081.  Only active when the ``config-api``
compose profile is enabled.  Caddy routes config-write POSTs here;
the web service continues to serve everything else.
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
    log.info("Starting config-api on port 8081")
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8081,
        log_config=None,
        log_level=log_level.lower(),
    )


if __name__ == "__main__":
    main()
