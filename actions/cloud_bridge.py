"""Local -> cloud bridge: lets the desktop JARVIS (main.py's voice loop)
ask the always-on Render deployment (core/headless_main.py) what it has
been doing, since the two run as separate processes with separate
SQLite-backed state (see core/headless/config.py's DATA_DIR docstring) —
the desktop app has no visibility into cloud-only background-worker
activity (scheduled agent runs, Gmail monitoring, etc.) that happened
while it wasn't running, until now.

One-directional by design: every call here originates from the desktop
and hits the cloud's existing bearer-token API (core/headless/*_api.py).
Nothing here lets the cloud reach back into this machine — the cloud
still cannot see or control the local desktop, matching the existing
architecture's boundary (headless auth.py's own docstring: no unrestricted
access, closed by default).

Never raises out to callers — every function returns a plain dict, with
a "_bridge_error" key on failure, so a tool tool_executor.py can always turn the
result into a spoken sentence instead of crashing the voice session over
a network hiccup.
"""
from __future__ import annotations

import os
from pathlib import Path

import requests

_BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv_value(name: str) -> str | None:
    """Same minimal .env convention as core/headless/config.py and
    scripts/render_deploy.py — kept local rather than importing
    core.headless.config so this module has no dependency on the cloud
    service's own package, just the env vars it also happens to read."""
    val = os.environ.get(name)
    if val:
        return val
    env_path = _BASE_DIR / ".env"
    if not env_path.is_file():
        return None
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == name:
                return value.strip().strip('"').strip("'") or None
    except Exception:
        pass
    return None


def _base_url() -> str:
    return (_load_dotenv_value("JARVIS_CLOUD_URL") or "https://jarvis-headless-core.onrender.com").rstrip("/")


def _token() -> str | None:
    return _load_dotenv_value("JARVIS_API_TOKEN")


def _get(path: str, timeout: float = 10.0) -> dict:
    token = _token()
    if not token:
        return {"_bridge_error": "JARVIS_API_TOKEN is not set locally — can't authenticate to the cloud instance."}
    try:
        r = requests.get(
            f"{_base_url()}{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except requests.RequestException as e:
        return {"_bridge_error": f"Could not reach the cloud JARVIS instance: {e}"}
    if r.status_code == 401:
        return {"_bridge_error": "Cloud instance rejected this token (401) — local JARVIS_API_TOKEN doesn't match what's configured on Render."}
    if r.status_code == 503:
        return {"_bridge_error": "Cloud instance has no JARVIS_API_TOKEN configured — every authenticated route is closed."}
    if not r.ok:
        return {"_bridge_error": f"Cloud instance returned HTTP {r.status_code}."}
    try:
        return r.json()
    except ValueError:
        return {"_bridge_error": "Cloud instance returned a non-JSON response."}


def _post(path: str, body: dict, timeout: float = 15.0) -> dict:
    token = _token()
    if not token:
        return {"_bridge_error": "JARVIS_API_TOKEN is not set locally — can't authenticate to the cloud instance."}
    try:
        r = requests.post(
            f"{_base_url()}{path}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
            timeout=timeout,
        )
    except requests.RequestException as e:
        return {"_bridge_error": f"Could not reach the cloud JARVIS instance: {e}"}
    if not r.ok:
        try:
            detail = r.json().get("detail", r.text[:200])
        except ValueError:
            detail = r.text[:200]
        return {"_bridge_error": f"Cloud instance returned HTTP {r.status_code}: {detail}"}
    try:
        return r.json()
    except ValueError:
        return {"_bridge_error": "Cloud instance returned a non-JSON response."}


def is_reachable() -> bool:
    try:
        r = requests.get(f"{_base_url()}/health", timeout=5.0)
        return r.ok
    except requests.RequestException:
        return False


def get_status() -> dict:
    return _get("/api/status")


def get_brief() -> dict:
    return _get("/api/brief")


def get_activity(limit: int = 10) -> dict:
    return _get(f"/api/activity?limit={int(limit)}")


def run_cloud_agent(agent_id: str, description: str) -> dict:
    """Assigns a task to a named agent on the CLOUD instance (not the local
    one — see actions/agent_orchestrator.py's local `orchestrator` for
    that). Same EXECUTE-level approval gate applies server-side; this
    can't bypass it, only reach the same POST /api/orchestrator/tasks the
    bearer-token API and the web UI already expose."""
    if not agent_id:
        return {"_bridge_error": "Which cloud agent? Give me an agent id."}
    return _post("/api/orchestrator/tasks", {"agent_id": agent_id, "description": description or "Task from local voice JARVIS"})
