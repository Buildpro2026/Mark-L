import json

from actions import github_integration as gh
from core.headless import config as _hc


def _isolate(monkeypatch, token="", repo=""):
    monkeypatch.setattr(_hc, "GITHUB_TOKEN", token or None)
    monkeypatch.setattr(_hc, "GITHUB_REPO", repo or None)


class _Resp:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self):
        return self._payload


def _fake_request(payload=None, status_code=200):
    def _f(method, url, headers=None, timeout=None, **kwargs):
        return _Resp(status_code, payload)
    return _f


# ── configuration ────────────────────────────────────────────────────────

def test_is_configured_false_without_token_or_repo(monkeypatch):
    _isolate(monkeypatch, token="", repo="")
    assert gh.is_configured() is False


def test_is_configured_false_with_only_token(monkeypatch):
    _isolate(monkeypatch, token="ghp_x", repo="")
    assert gh.is_configured() is False


def test_is_configured_true_with_both(monkeypatch):
    _isolate(monkeypatch, token="ghp_x", repo="buildpro2026/mark-l")
    assert gh.is_configured() is True


def test_verify_github_not_configured_short_circuits_without_network(monkeypatch):
    _isolate(monkeypatch, token="", repo="")
    r = gh.verify_github()
    assert r == {"configured": False, "verified": False, "status": "NOT_CONFIGURED"}


def test_not_configured_detail_names_the_exact_missing_env_vars(monkeypatch):
    _isolate(monkeypatch, token="", repo="")
    r = gh.get_repo()
    assert r["ok"] is False
    assert "GITHUB_TOKEN" in r["detail"] and "GITHUB_REPO" in r["detail"]


def test_verify_github_success(monkeypatch):
    _isolate(monkeypatch, token="ghp_x", repo="buildpro2026/mark-l")
    calls = []

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        calls.append((method, url, headers))
        return _Resp(200, {
            "full_name": "buildpro2026/mark-l", "private": True,
            "default_branch": "main", "open_issues_count": 3,
            "pushed_at": "2026-09-01T00:00:00Z", "html_url": "https://github.com/buildpro2026/mark-l",
        })

    monkeypatch.setattr(gh.requests, "request", fake_request)
    r = gh.verify_github()
    assert r["configured"] is True
    assert r["verified"] is True
    assert r["repo"]["full_name"] == "buildpro2026/mark-l"
    method, url, headers = calls[0]
    assert method == "GET"
    assert url.endswith("/repos/buildpro2026/mark-l")
    assert headers["Authorization"] == "Bearer ghp_x"


def test_verify_github_reports_401(monkeypatch):
    _isolate(monkeypatch, token="bad", repo="buildpro2026/mark-l")
    monkeypatch.setattr(gh.requests, "request", _fake_request({"message": "Bad credentials"}, 401))
    r = gh.verify_github()
    assert r["verified"] is False
    assert "401" in r["status"]


def test_request_wrapper_captures_network_exception(monkeypatch):
    _isolate(monkeypatch, token="ghp_x", repo="buildpro2026/mark-l")

    def raise_exc(*a, **k):
        raise ConnectionError("no network")

    monkeypatch.setattr(gh.requests, "request", raise_exc)
    r = gh.list_commits()
    assert r["ok"] is False
    assert r["state"] == "ERROR"
    assert r["results"] == []


# ── commits / branches / issues / PRs ────────────────────────────────────

def test_list_commits_success(monkeypatch):
    _isolate(monkeypatch, token="ghp_x", repo="buildpro2026/mark-l")
    payload = [{
        "sha": "abc123def4567890",
        "commit": {"message": "Fix bug\n\nDetails.", "author": {"name": "Lee", "date": "2026-09-01T00:00:00Z"}},
        "html_url": "https://github.com/x/y/commit/abc123",
    }]
    monkeypatch.setattr(gh.requests, "request", _fake_request(payload, 200))
    r = gh.list_commits(limit=5)
    assert r["ok"] is True
    assert r["results"][0]["sha"] == "abc123def456"
    assert r["results"][0]["message"] == "Fix bug"
    assert r["results"][0]["author"] == "Lee"


def test_list_commits_not_configured(monkeypatch):
    _isolate(monkeypatch, token="", repo="")
    r = gh.list_commits()
    assert r["ok"] is False
    assert r["state"] == "NOT_CONFIGURED"
    assert r["results"] == []


def test_list_branches_success(monkeypatch):
    _isolate(monkeypatch, token="ghp_x", repo="buildpro2026/mark-l")
    payload = [{"name": "main"}, {"name": "feature/jarvis-2"}]
    monkeypatch.setattr(gh.requests, "request", _fake_request(payload, 200))
    r = gh.list_branches()
    assert r["ok"] is True
    assert r["results"] == ["main", "feature/jarvis-2"]


def test_list_issues_excludes_pull_requests(monkeypatch):
    _isolate(monkeypatch, token="ghp_x", repo="buildpro2026/mark-l")
    payload = [
        {"number": 1, "title": "A real issue", "state": "open", "user": {"login": "lee"}, "html_url": "u1", "created_at": "t1"},
        {"number": 2, "title": "Actually a PR", "state": "open", "user": {"login": "lee"}, "html_url": "u2",
         "created_at": "t2", "pull_request": {"url": "..."}},
    ]
    monkeypatch.setattr(gh.requests, "request", _fake_request(payload, 200))
    r = gh.list_issues()
    assert r["ok"] is True
    assert len(r["results"]) == 1
    assert r["results"][0]["number"] == 1


def test_list_pull_requests_success(monkeypatch):
    _isolate(monkeypatch, token="ghp_x", repo="buildpro2026/mark-l")
    payload = [{
        "number": 5, "title": "Add feature", "state": "open", "user": {"login": "lee"},
        "head": {"ref": "feature/x"}, "base": {"ref": "main"}, "html_url": "u5", "created_at": "t5",
    }]
    monkeypatch.setattr(gh.requests, "request", _fake_request(payload, 200))
    r = gh.list_pull_requests()
    assert r["ok"] is True
    assert r["results"][0]["head"] == "feature/x"
    assert r["results"][0]["base"] == "main"


def test_get_repo_reports_error_honestly_not_fabricated(monkeypatch):
    _isolate(monkeypatch, token="ghp_x", repo="nonexistent/repo")
    monkeypatch.setattr(gh.requests, "request", _fake_request({"message": "Not Found"}, 404))
    r = gh.get_repo()
    assert r["ok"] is False
    assert r["state"] == "ERROR"
    assert r["status_code"] == 404
