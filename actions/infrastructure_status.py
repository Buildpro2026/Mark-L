"""Deployment infrastructure visibility — Render (live), Oracle (planned).

Config lives in environment variables RENDER_API_KEY and RENDER_SERVICE_ID,
matching the pattern used by hubspot_integration.py / github_integration.py:
never hardcoded, read at call time. Read-only — this reports deploy/service
status for the Command Center's Infrastructure nucleus, it never triggers a
deploy, changes a service, or touches any other Render account resource.

Never exposes secrets: no API key, database URL, or other credential value
ever appears in a returned dict — only booleans/names of which environment
variables are set (matching core/headless/config.summarize()'s existing
"*_env_set" pattern) and non-secret service metadata (name, status,
timestamps, URLs).

Oracle has no integration built yet — get_oracle_status() always reports
UNCONFIGURED/planned rather than fabricating a live connection. When an
Oracle integration exists, it should follow this same module's shape
(is_configured/get_status), not invent a new one.
"""
from __future__ import annotations

from typing import Any

import requests

API_BASE = "https://api.render.com/v1"


def _api_key() -> str | None:
    from core.headless import config as _hc
    return _hc.RENDER_API_KEY or None


def _service_id() -> str | None:
    from core.headless import config as _hc
    return _hc.RENDER_SERVICE_ID or None


def is_render_configured() -> bool:
    return bool(_api_key() and _service_id())


def get_render_status() -> dict[str, Any]:
    """Live service + latest-deploy status from Render's own API. Never
    fabricates: any missing config or API failure is reported honestly,
    never silently reported as healthy."""
    api_key, service_id = _api_key(), _service_id()
    if not api_key or not service_id:
        missing = []
        if not api_key:
            missing.append("RENDER_API_KEY")
        if not service_id:
            missing.append("RENDER_SERVICE_ID")
        return {
            "configured": False, "state": "NOT_CONFIGURED",
            "detail": f"Render isn't configured — missing: {', '.join(missing)}.",
        }
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    try:
        svc_resp = requests.get(f"{API_BASE}/services/{service_id}", headers=headers, timeout=15)
    except Exception as exc:
        return {"configured": True, "state": "ERROR", "detail": str(exc)}
    if svc_resp.status_code >= 400:
        try:
            detail = svc_resp.json().get("message", svc_resp.text[:300])
        except Exception:
            detail = svc_resp.text[:300]
        return {"configured": True, "state": "ERROR", "status_code": svc_resp.status_code, "detail": detail}
    svc = svc_resp.json()

    deploy_summary: dict[str, Any] = {}
    try:
        dep_resp = requests.get(
            f"{API_BASE}/services/{service_id}/deploys", headers=headers,
            params={"limit": 1}, timeout=15,
        )
        if dep_resp.status_code < 400:
            deploys = dep_resp.json()
            if deploys:
                latest = (deploys[0] or {}).get("deploy", deploys[0])
                deploy_summary = {
                    "status": latest.get("status"),
                    "created_at": latest.get("createdAt"),
                    "finished_at": latest.get("finishedAt"),
                }
    except Exception:
        pass   # deploy history is supplementary — service status above is the real signal

    return {
        "configured": True, "state": "OK",
        "service": {
            "name": svc.get("name"),
            "type": svc.get("type"),
            "status": svc.get("suspended") and "suspended" or "active",
            "url": svc.get("serviceDetails", {}).get("url") if isinstance(svc.get("serviceDetails"), dict) else None,
            "region": svc.get("serviceDetails", {}).get("region") if isinstance(svc.get("serviceDetails"), dict) else None,
            "updated_at": svc.get("updatedAt"),
        },
        "latest_deploy": deploy_summary,
    }


def get_oracle_status() -> dict[str, Any]:
    """Oracle has no integration built yet — always planned/unconfigured,
    never a fabricated live connection."""
    return {
        "configured": False, "state": "PLANNED",
        "detail": "Oracle infrastructure integration is not built yet.",
    }


def get_infrastructure_overview() -> dict[str, Any]:
    """Combined status for the Command Center's Infrastructure nucleus —
    one call the UI/tool layer can use instead of stitching two together."""
    return {
        "render": get_render_status(),
        "oracle": get_oracle_status(),
    }
