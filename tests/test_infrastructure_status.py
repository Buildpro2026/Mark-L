import json

from actions import infrastructure_status as infra
from core.headless import config as _hc


def _isolate(monkeypatch, api_key="", service_id=""):
    monkeypatch.setattr(_hc, "RENDER_API_KEY", api_key or None)
    monkeypatch.setattr(_hc, "RENDER_SERVICE_ID", service_id or None)


class _Resp:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self):
        return self._payload


# ── configuration ────────────────────────────────────────────────────────

def test_is_render_configured_false_without_key_or_service_id(monkeypatch):
    _isolate(monkeypatch, api_key="", service_id="")
    assert infra.is_render_configured() is False


def test_is_render_configured_true_with_both(monkeypatch):
    _isolate(monkeypatch, api_key="rnd_x", service_id="srv-123")
    assert infra.is_render_configured() is True


def test_get_render_status_not_configured_short_circuits_without_network(monkeypatch):
    _isolate(monkeypatch, api_key="", service_id="")
    r = infra.get_render_status()
    assert r["configured"] is False
    assert r["state"] == "NOT_CONFIGURED"
    assert "RENDER_API_KEY" in r["detail"] and "RENDER_SERVICE_ID" in r["detail"]


def test_get_render_status_success(monkeypatch):
    _isolate(monkeypatch, api_key="rnd_x", service_id="srv-123")
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append((url, headers))
        if url.endswith("/deploys"):
            return _Resp(200, [{"deploy": {"status": "live", "createdAt": "t1", "finishedAt": "t2"}}])
        return _Resp(200, {
            "name": "jarvis-headless-core", "type": "web_service", "suspended": "not_suspended",
            "serviceDetails": {"url": "https://jarvis-headless-core.onrender.com", "region": "oregon"},
            "updatedAt": "2026-09-01T00:00:00Z",
        })

    monkeypatch.setattr(infra.requests, "get", fake_get)
    r = infra.get_render_status()
    assert r["configured"] is True
    assert r["state"] == "OK"
    assert r["service"]["name"] == "jarvis-headless-core"
    assert r["service"]["url"] == "https://jarvis-headless-core.onrender.com"
    assert r["latest_deploy"]["status"] == "live"
    first_call_headers = calls[0][1]
    assert first_call_headers["Authorization"] == "Bearer rnd_x"


def test_get_render_status_reports_api_error_honestly(monkeypatch):
    _isolate(monkeypatch, api_key="bad", service_id="srv-123")
    monkeypatch.setattr(infra.requests, "get", lambda *a, **k: _Resp(401, {"message": "Unauthorized"}))
    r = infra.get_render_status()
    assert r["configured"] is True
    assert r["state"] == "ERROR"
    assert r["status_code"] == 401


def test_get_render_status_captures_network_exception(monkeypatch):
    _isolate(monkeypatch, api_key="rnd_x", service_id="srv-123")

    def raise_exc(*a, **k):
        raise ConnectionError("no network")

    monkeypatch.setattr(infra.requests, "get", raise_exc)
    r = infra.get_render_status()
    assert r["configured"] is True
    assert r["state"] == "ERROR"


def test_get_render_status_never_exposes_the_api_key(monkeypatch):
    _isolate(monkeypatch, api_key="rnd_super_secret_value", service_id="srv-123")

    def fake_get(url, headers=None, params=None, timeout=None):
        return _Resp(200, {"name": "svc", "type": "web_service", "suspended": "not_suspended",
                            "serviceDetails": {}, "updatedAt": "t"})

    monkeypatch.setattr(infra.requests, "get", fake_get)
    r = infra.get_render_status()
    assert "rnd_super_secret_value" not in json.dumps(r)


# ── Oracle: always honest planned/unconfigured, never fabricated ────────

def test_oracle_always_reports_planned_not_a_fake_connection():
    r = infra.get_oracle_status()
    assert r["configured"] is False
    assert r["state"] == "PLANNED"


# ── combined overview ─────────────────────────────────────────────────

def test_overview_combines_both_services(monkeypatch):
    _isolate(monkeypatch, api_key="", service_id="")
    r = infra.get_infrastructure_overview()
    assert r["render"]["state"] == "NOT_CONFIGURED"
    assert r["oracle"]["state"] == "PLANNED"
