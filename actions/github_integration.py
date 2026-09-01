"""GitHub development-visibility integration — read-only.

Config lives in environment variables GITHUB_TOKEN (a personal access
token with repo:read scope — repo scope for private repos) and GITHUB_REPO
("owner/name"), matching the pattern used by hubspot_integration.py and
buffer_integration.py: never hardcoded, read at call time.

Deliberately read-only. JARVIS can report on repository state (commits,
branches, issues, pull requests) for the Command Center's Development
nucleus and for conversation, but this module has no create/update/merge/
delete calls — any actual code change still goes through the normal
git/PR workflow a person drives, not a voice command. That is a scope
decision, not a missing feature: "authorized GitHub work" per the
Command Center spec means visibility plus using GitHub through the
existing developer tooling, not JARVIS pushing commits on his own
initiative.

Uses the GitHub REST API v3 (api.github.com) with Bearer auth. Never
fabricates data — every function either returns a real API result or an
honest NOT_CONFIGURED / ERROR state, and API-level errors (4xx/5xx) are
captured and reported rather than raised.
"""
from __future__ import annotations

from typing import Any

import requests

API_BASE = "https://api.github.com"


def _token() -> str | None:
    from core.headless import config as _hc
    return _hc.GITHUB_TOKEN or None


def _repo() -> str | None:
    from core.headless import config as _hc
    return _hc.GITHUB_REPO or None


def is_configured() -> bool:
    return bool(_token() and _repo())


def _request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    """Shared request wrapper — always returns a uniform dict, never
    raises. Distinguishes NOT_CONFIGURED (no token/repo) from ERROR
    (network failure or a 4xx/5xx from GitHub)."""
    token, repo = _token(), _repo()
    if not token or not repo:
        missing = []
        if not token:
            missing.append("GITHUB_TOKEN")
        if not repo:
            missing.append("GITHUB_REPO")
        return {
            "ok": False, "state": "NOT_CONFIGURED",
            "detail": f"GitHub isn't configured — missing: {', '.join(missing)}.",
        }
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        resp = requests.request(method, f"{API_BASE}{path}", headers=headers, timeout=15, **kwargs)
    except Exception as exc:
        return {"ok": False, "state": "ERROR", "detail": str(exc)}
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("message", resp.text[:300])
        except Exception:
            detail = resp.text[:300]
        return {"ok": False, "state": "ERROR", "status_code": resp.status_code, "detail": detail}
    try:
        data = resp.json()
    except Exception:
        data = {}
    return {"ok": True, "state": "OK", "status_code": resp.status_code, "data": data}


def verify_github() -> dict[str, Any]:
    """Live auth + repo-visibility check — the GitHub equivalent of
    hubspot_integration.verify_hubspot()."""
    if not is_configured():
        return {"configured": False, "verified": False, "status": "NOT_CONFIGURED"}
    result = _request("GET", f"/repos/{_repo()}")
    if result["ok"]:
        d = result["data"]
        return {
            "configured": True, "verified": True, "status": "VERIFIED",
            "repo": {
                "full_name": d.get("full_name"), "private": d.get("private"),
                "default_branch": d.get("default_branch"),
                "open_issues_count": d.get("open_issues_count"),
                "pushed_at": d.get("pushed_at"),
                "html_url": d.get("html_url"),
            },
        }
    detail = result.get("detail", "unknown error")
    code = result.get("status_code")
    return {
        "configured": True, "verified": False,
        "status": f"UNAVAILABLE:{code}" if code else f"UNAVAILABLE:{detail}",
        "detail": detail,
    }


def get_repo() -> dict[str, Any]:
    return _request("GET", f"/repos/{_repo()}")


def list_commits(branch: str | None = None, limit: int = 10) -> dict[str, Any]:
    params: dict[str, Any] = {"per_page": min(max(limit, 1), 100)}
    if branch:
        params["sha"] = branch
    result = _request("GET", f"/repos/{_repo()}/commits", params=params)
    if not result["ok"]:
        return {"ok": False, "state": result["state"], "detail": result.get("detail"), "results": []}
    commits = [
        {
            "sha": c.get("sha", "")[:12],
            "message": (c.get("commit", {}).get("message") or "").split("\n", 1)[0],
            "author": c.get("commit", {}).get("author", {}).get("name"),
            "date": c.get("commit", {}).get("author", {}).get("date"),
            "url": c.get("html_url"),
        }
        for c in result["data"]
    ]
    return {"ok": True, "state": "OK", "results": commits}


def list_branches(limit: int = 20) -> dict[str, Any]:
    result = _request("GET", f"/repos/{_repo()}/branches", params={"per_page": min(max(limit, 1), 100)})
    if not result["ok"]:
        return {"ok": False, "state": result["state"], "detail": result.get("detail"), "results": []}
    return {"ok": True, "state": "OK", "results": [b.get("name") for b in result["data"]]}


def list_issues(state: str = "open", limit: int = 20) -> dict[str, Any]:
    """GitHub's /issues endpoint includes pull requests — filtered out here
    since callers asking for "issues" mean actual issues, and
    list_pull_requests() below is the dedicated PR call."""
    result = _request("GET", f"/repos/{_repo()}/issues", params={"state": state, "per_page": min(max(limit, 1), 100)})
    if not result["ok"]:
        return {"ok": False, "state": result["state"], "detail": result.get("detail"), "results": []}
    issues = [
        {
            "number": i.get("number"), "title": i.get("title"),
            "state": i.get("state"), "user": (i.get("user") or {}).get("login"),
            "url": i.get("html_url"), "created_at": i.get("created_at"),
        }
        for i in result["data"] if "pull_request" not in i
    ]
    return {"ok": True, "state": "OK", "results": issues}


def list_pull_requests(state: str = "open", limit: int = 20) -> dict[str, Any]:
    result = _request("GET", f"/repos/{_repo()}/pulls", params={"state": state, "per_page": min(max(limit, 1), 100)})
    if not result["ok"]:
        return {"ok": False, "state": result["state"], "detail": result.get("detail"), "results": []}
    prs = [
        {
            "number": p.get("number"), "title": p.get("title"),
            "state": p.get("state"), "user": (p.get("user") or {}).get("login"),
            "head": (p.get("head") or {}).get("ref"), "base": (p.get("base") or {}).get("ref"),
            "url": p.get("html_url"), "created_at": p.get("created_at"),
        }
        for p in result["data"]
    ]
    return {"ok": True, "state": "OK", "results": prs}
