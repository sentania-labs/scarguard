"""Render, validate and reload the Caddyfile from scarguard.yml.

This is the *active* Caddy config generator - config/Caddyfile.template is a
reference document only. config/caddy-entrypoint.sh calls it twice:

* ``generate CONFIG CADDYFILE`` at container start. Invalid tls values, a
  malformed scarguard.yml or a Caddyfile that ``caddy validate`` rejects fall
  back to the HTTP-only config, so the UI stays reachable to fix them.
* ``reload CONFIG CADDYFILE`` when scarguard.yml changes. Nothing is reloaded
  unless the new Caddyfile renders from valid values and passes
  ``caddy validate``. The previous file is kept as ``CADDYFILE.last-good``
  and put back if ``caddy reload`` fails, so the running proxy and the file
  on disk never drift apart. Exit status 1 means "kept the current config".

Every value interpolated into the Caddyfile is checked by shared/tls_safety.py
(the same rules the web service applies on save). ``system.config_api.enabled``
is ignored: the config-api service is an unauthenticated 501 scaffold, so all
traffic, including settings writes, goes to web.

Caddy's ``caddyfile`` linter expects tab indentation - keep the tabs.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

# In the caddy image tls_safety.py is copied next to this file.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tls_safety  # noqa: E402
import yaml  # noqa: E402

CADDY_BIN = os.environ.get("CADDY_BIN", "caddy")
CADDY_TIMEOUT_SECONDS = 60


class RenderError(ValueError):
    """scarguard.yml cannot be turned into a safe Caddyfile."""


def log(message: str) -> None:
    print(f"[caddy-entrypoint] {message}", file=sys.stderr, flush=True)


def load_config(path: str | Path) -> dict[str, Any]:
    """Read scarguard.yml. A missing file is an empty config; a malformed one
    raises :class:`RenderError`."""
    try:
        with open(path) as f:
            cfg = yaml.safe_load(f)
    except FileNotFoundError:
        return {}
    except (OSError, yaml.YAMLError) as exc:
        raise RenderError(f"cannot read {path}: {type(exc).__name__}") from exc
    if cfg is None:
        return {}
    if not isinstance(cfg, dict):
        raise RenderError(f"{path} must contain a YAML mapping")
    return cfg


def https_port_suffix(raw: str | None) -> str:
    port = (raw or "443").strip()
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        log(f"Ignoring invalid HTTPS_PORT {port!r}; using 443")
        port = "443"
    return "" if port == "443" else f":{int(port)}"


def _snippet(tls_active: bool) -> str:
    hsts_header = (
        '\t\tStrict-Transport-Security "max-age=31536000; includeSubDomains"\n'
        if tls_active else ""
    )
    return """(scarguard) {
\theader {
\t\tX-Frame-Options DENY
\t\tX-Content-Type-Options nosniff
\t\tReferrer-Policy strict-origin-when-cross-origin
\t\tPermissions-Policy "geolocation=(), camera=(), microphone=(), payment=()"
""" + hsts_header + """\t\tContent-Security-Policy "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'"
\t\tCross-Origin-Opener-Policy same-origin
\t\tCross-Origin-Resource-Policy same-origin
\t\t-Server
\t}
\t# Drop common bot-probe paths at the edge so they never reach FastAPI.
\t# 404 (not 403) is intentional - quieter, looks like the path doesn't
\t# exist so scanners are less likely to follow up with more probes.
\t@probes {
\t\tpath /.git/* /_ignition/* /aws*config.js /config.js
\t}
\trespond @probes 404
\treverse_proxy web:8080
}
"""


def render_http_only() -> str:
    """The safe fallback: plain HTTP on :80, everything proxied to web."""
    return f"""{_snippet(False)}
:80 {{
\timport scarguard
}}
"""


def _config_api_requested(cfg: dict[str, Any]) -> bool:
    system = cfg.get("system")
    config_api = system.get("config_api") if isinstance(system, dict) else None
    return isinstance(config_api, dict) and config_api.get("enabled") is True


def render(
    cfg: dict[str, Any],
    https_port: str | None = None,
    file_exists: Callable[[str], bool] = os.path.isfile,
) -> str:
    """Return the Caddyfile for *cfg*, or raise :class:`RenderError`."""
    if _config_api_requested(cfg):
        log(
            "Ignoring system.config_api.enabled: the config-api service is an "
            "unimplemented scaffold; web keeps handling configuration writes",
        )
    try:
        tls = tls_safety.validate_tls(cfg.get("tls"))
    except tls_safety.TLSValueError as exc:
        raise RenderError(str(exc)) from exc

    mode = tls["mode"]
    if mode == "auto":
        return f"""{_snippet(True)}
{tls["domain"]} {{
\timport scarguard
}}
"""
    if mode == "manual":
        missing = [
            f"{name} ({path})"
            for name, path in (("cert", tls["cert_path"]), ("key", tls["key_path"]))
            if not file_exists(path)
        ]
        if missing:
            raise RenderError(f"tls.mode=manual but missing: {', '.join(missing)}")
        suffix = https_port_suffix(https_port)
        return f"""{_snippet(True)}
:443 {{
\ttls {tls["cert_path"]} {tls["key_path"]}
\timport scarguard
}}

:80 {{
\tredir https://{{host}}{suffix}{{uri}} permanent
}}
"""
    return render_http_only()


def caddy_validate(path: str | Path) -> bool:
    """Run ``caddy validate`` on *path*. False on any failure."""
    try:
        result = subprocess.run(
            [CADDY_BIN, "validate", "--config", str(path), "--adapter", "caddyfile"],
            capture_output=True, text=True, timeout=CADDY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"caddy validate could not run: {type(exc).__name__}")
        return False
    if result.returncode != 0:
        log(f"caddy validate rejected the new Caddyfile: {result.stderr.strip()[-2000:]}")
        return False
    return True


def caddy_reload(path: str | Path) -> bool:
    try:
        result = subprocess.run(
            [CADDY_BIN, "reload", "--config", str(path), "--adapter", "caddyfile"],
            capture_output=True, text=True, timeout=CADDY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"caddy reload could not run: {type(exc).__name__}")
        return False
    if result.returncode != 0:
        log(f"caddy reload failed: {result.stderr.strip()[-2000:]}")
        return False
    return True


def _write_candidate(caddyfile: Path, body: str) -> Path:
    """Write *body* to a fsynced temporary file next to *caddyfile*."""
    fd, tmp = tempfile.mkstemp(dir=str(caddyfile.parent), prefix=".Caddyfile-", suffix=".candidate")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        os.unlink(tmp)
        raise
    return Path(tmp)


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def generate(config_path: str, caddyfile_path: str) -> int:
    """Write the startup Caddyfile. Always leaves a usable file; returns 0."""
    caddyfile = Path(caddyfile_path)
    try:
        body = render(load_config(config_path), os.environ.get("HTTPS_PORT"))
    except RenderError as exc:
        log(f"Refusing tls settings ({exc}) - starting HTTP-only so the UI stays reachable")
        body = render_http_only()
    candidate = _write_candidate(caddyfile, body)
    if body != render_http_only() and not caddy_validate(candidate):
        log("Generated Caddyfile failed validation - starting HTTP-only")
        _discard(candidate)
        candidate = _write_candidate(caddyfile, render_http_only())
    os.replace(candidate, caddyfile)
    log("Generated Caddyfile")
    return 0


def reload(config_path: str, caddyfile_path: str) -> int:
    """Validate and apply a changed scarguard.yml. 0 = applied/unchanged,
    1 = refused or failed, current config kept."""
    caddyfile = Path(caddyfile_path)
    last_good = caddyfile.with_name(caddyfile.name + ".last-good")
    try:
        body = render(load_config(config_path), os.environ.get("HTTPS_PORT"))
    except RenderError as exc:
        log(f"Refusing new tls settings ({exc}) - keeping the current Caddy config")
        return 1
    try:
        current = caddyfile.read_text()
    except FileNotFoundError:
        current = None
    if body == current:
        log("Caddyfile unchanged - no reload needed")
        return 0

    candidate = _write_candidate(caddyfile, body)
    if not caddy_validate(candidate):
        _discard(candidate)
        log("Keeping the current Caddy config")
        return 1

    if current is not None:
        os.replace(_write_candidate(caddyfile, current), last_good)
    os.replace(candidate, caddyfile)
    if caddy_reload(caddyfile):
        log("Caddy reloaded with the new config")
        return 0
    # Caddy keeps serving its previous config when a reload fails; put the
    # matching file back so a container restart starts from the same state.
    if current is not None:
        os.replace(_write_candidate(caddyfile, current), caddyfile)
        log("Restored the last-good Caddyfile")
    return 1


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[1] not in ("generate", "reload"):
        print("usage: caddy_config.py {generate|reload} CONFIG_PATH CADDYFILE", file=sys.stderr)
        return 2
    action, config_path, caddyfile_path = argv[1:]
    if action == "generate":
        return generate(config_path, caddyfile_path)
    return reload(config_path, caddyfile_path)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
