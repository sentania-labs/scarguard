"""ScarGuard web service — startup script.

Reads scarguard.yml at launch to determine whether SSL is enabled, then starts
uvicorn accordingly:

  - ssl.enabled = false (default)  → HTTP only on port 8080
  - ssl.enabled = true, https_only = false → HTTP on 8080 + HTTPS on 8443
  - ssl.enabled = true, https_only = true  → HTTPS only on 8443

If SSL is enabled but the cert/key files are missing, falls back to HTTP with a
warning.  Requires the cert/key to be mounted into the container at the paths
configured in scarguard.yml (default: /certs/cert.pem, /certs/key.pem).

Optional: set ssl.keyfile_password in scarguard.yml if the private key is
passphrase-protected.  Leave unset (or empty) for unencrypted keys.
"""

import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("start")

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/scarguard.yml")


def _load_ssl_cfg() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("ssl", {}) if isinstance(cfg, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.warning("Could not read %s: %s — SSL disabled", CONFIG_PATH, exc)
        return {}


def _normalize_ssl_cfg(ssl_cfg: dict) -> dict:
    """Return a canonical dict for comparison (strip empty password, sort keys)."""
    canonical = {
        "enabled": bool(ssl_cfg.get("enabled", False)),
        "cert_path": ssl_cfg.get("cert_path", "/certs/cert.pem"),
        "key_path": ssl_cfg.get("key_path", "/certs/key.pem"),
        "https_only": bool(ssl_cfg.get("https_only", False)),
    }
    kp = ssl_cfg.get("keyfile_password")
    if isinstance(kp, str) and kp:
        canonical["keyfile_password"] = kp
    return canonical


def _ssl_config_watcher(initial_ssl: dict, poll_interval: int = 5) -> None:
    """Background thread that exits the process when SSL config changes.

    Docker ``restart: unless-stopped`` will bring the container back up with the
    new SSL settings applied.
    """
    current = _normalize_ssl_cfg(initial_ssl)
    consecutive_errors = 0
    while True:
        time.sleep(poll_interval)
        try:
            fresh = _normalize_ssl_cfg(_load_ssl_cfg())
            consecutive_errors = 0
        except Exception as exc:
            consecutive_errors += 1
            if consecutive_errors % 12 == 1:  # log every ~60s of failures
                log.warning(
                    "SSL config watcher: cannot read config (%d consecutive failures): %s",
                    consecutive_errors,
                    exc,
                )
            continue
        if fresh != current:
            log.info(
                "SSL configuration changed — restarting web service to apply new settings"
            )
            # Brief delay so any in-flight HTTP response can finish.
            time.sleep(1)
            os.kill(os.getpid(), signal.SIGTERM)
            return


def _uvicorn_cfg(**extra: Any) -> dict[str, Any]:
    return {
        "app": "main:app",
        "host": "0.0.0.0",
        "log_config": None,
        **extra,
    }


def main() -> None:
    ssl_cfg = _load_ssl_cfg()

    # Watch for SSL config changes and restart if needed.
    watcher = threading.Thread(
        target=_ssl_config_watcher,
        args=(ssl_cfg,),
        name="ssl-config-watcher",
        daemon=True,
    )
    watcher.start()

    ssl_enabled: bool = bool(ssl_cfg.get("enabled", False))
    cert_path: str = ssl_cfg.get("cert_path", "/certs/cert.pem")
    key_path: str = ssl_cfg.get("key_path", "/certs/key.pem")
    https_only: bool = bool(ssl_cfg.get("https_only", False))
    # Absent or empty string → None (uvicorn treats None as "no password").
    # Uses explicit string check rather than truthiness to avoid coercing
    # unusual values (e.g. integer 0) silently.
    _kp = ssl_cfg.get("keyfile_password")
    keyfile_password: str | None = str(_kp) if isinstance(_kp, str) and _kp else None

    if ssl_enabled:
        cert_ok = Path(cert_path).exists()
        key_ok = Path(key_path).exists()
        if not cert_ok or not key_ok:
            log.warning(
                "SSL enabled in config but cert/key not found "
                "(cert=%s exists=%s, key=%s exists=%s) — falling back to HTTP",
                cert_path, cert_ok, key_path, key_ok,
            )
            ssl_enabled = False

    if ssl_enabled:
        log.info(
            "Starting with SSL: cert=%s key=%s https_only=%s password_protected=%s",
            cert_path, key_path, https_only, keyfile_password is not None,
        )
        if not https_only:
            # Start plain HTTP in a background daemon thread.
            # NOTE: when the main HTTPS server exits (SIGTERM), the daemon thread
            # is killed abruptly — in-flight HTTP requests on port 8080 are not
            # gracefully drained.  This is acceptable for an internal home-network
            # UI; both servers share the same FastAPI app instance and no
            # in-memory state is written to from either server.
            t = threading.Thread(
                target=uvicorn.run,
                kwargs=_uvicorn_cfg(port=8080),
                name="http-server",
                daemon=True,
            )
            t.start()
            log.info("HTTP listener started on port 8080")

        # HTTPS in the main thread (blocks until shutdown).
        https_kwargs: dict[str, Any] = dict(
            port=8443,
            ssl_certfile=cert_path,
            ssl_keyfile=key_path,
        )
        if keyfile_password is not None:
            https_kwargs["ssl_keyfile_password"] = keyfile_password
        uvicorn.run(**_uvicorn_cfg(**https_kwargs))
    else:
        log.info("Starting HTTP on port 8080 (SSL disabled)")
        uvicorn.run(**_uvicorn_cfg(port=8080))


if __name__ == "__main__":
    main()
