"""actions/cartesia_calls.py — outbound conversational phone calls through
the Cartesia Line agent. Previously had zero direct unit coverage (only
exercised incidentally via test_approval_notifier.py/test_urgent_escalation.py,
neither of which touches place_call's number-validation or the live HTTP
paths). This module places a REAL call that costs real money, so every
branch — configured/not, bad number, HTTP failure, network exception —
needs to be proven honest, not just plausible.

Never makes a live Cartesia call: every `requests` call here is monkeypatched.
"""
from actions import cartesia_calls as cc
from core.headless import config as hc


def _configure(monkeypatch, api_key="key123", agent_id="agent123", phone_id="phone123", owner_phone=None):
    monkeypatch.setattr(hc, "CARTESIA_API_KEY", api_key)
    monkeypatch.setattr(hc, "CARTESIA_AGENT_ID", agent_id)
    monkeypatch.setattr(hc, "CARTESIA_PHONE_NUMBER_ID", phone_id)
    monkeypatch.setattr(hc, "CARTESIA_API_VERSION", "2026-03-01")
    monkeypatch.setattr(hc, "JARVIS_OWNER_PHONE", owner_phone)


# ── is_configured / get_status ──────────────────────────────

def test_is_configured_false_with_nothing_set(monkeypatch):
    _configure(monkeypatch, api_key=None, agent_id=None, phone_id=None)
    assert cc.is_configured() is False


def test_is_configured_false_with_partial_credentials(monkeypatch):
    _configure(monkeypatch, api_key="key123", agent_id=None, phone_id="phone123")
    assert cc.is_configured() is False


def test_is_configured_true_with_all_three(monkeypatch):
    _configure(monkeypatch)
    assert cc.is_configured() is True


def test_get_status_not_configured_names_the_exact_missing_vars(monkeypatch):
    _configure(monkeypatch, api_key=None, agent_id=None, phone_id="phone123")
    s = cc.get_status()
    assert s["state"] == "NOT_CONFIGURED"
    assert s["missing"] == ["CARTESIA_API_KEY", "CARTESIA_AGENT_ID"]
    assert "CARTESIA_API_KEY" in s["detail"] and "CARTESIA_AGENT_ID" in s["detail"]


def test_get_status_configured_reports_agent_and_owner_phone(monkeypatch):
    _configure(monkeypatch, owner_phone="+13125550100")
    s = cc.get_status()
    assert s["state"] == "CONFIGURED"
    assert s["agent_id"] == "agent123"
    assert s["owner_phone_set"] is True


def test_get_status_configured_but_no_owner_phone(monkeypatch):
    _configure(monkeypatch, owner_phone=None)
    s = cc.get_status()
    assert s["owner_phone_set"] is False


# ── check_connection ────────────────────────────────────

def test_check_connection_not_configured_short_circuits_without_network_call(monkeypatch):
    _configure(monkeypatch, api_key=None, agent_id=None, phone_id=None)
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call the network")))
    assert cc.check_connection()["state"] == "NOT_CONFIGURED"


def test_check_connection_connected_on_http_200(monkeypatch):
    _configure(monkeypatch)
    import requests

    class _Resp:
        status_code = 200
        ok = True
        def json(self): return {"id": "agent123", "name": "JARVIS"}

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    s = cc.check_connection()
    assert s["state"] == "CONNECTED"
    assert s["agent"]["id"] == "agent123"


def test_check_connection_unauthorized_on_401(monkeypatch):
    _configure(monkeypatch)
    import requests

    class _Resp:
        status_code = 401
        text = "unauthorized"
        def json(self): return {}

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    assert cc.check_connection()["state"] == "UNAUTHORIZED"


def test_check_connection_unauthorized_on_403(monkeypatch):
    _configure(monkeypatch)
    import requests

    class _Resp:
        status_code = 403
        text = "forbidden"
        def json(self): return {}

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    assert cc.check_connection()["state"] == "UNAUTHORIZED"


def test_check_connection_error_on_404_names_the_agent_id(monkeypatch):
    _configure(monkeypatch)
    import requests

    class _Resp:
        status_code = 404
        text = "not found"
        def json(self): return {}

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    s = cc.check_connection()
    assert s["state"] == "ERROR"
    assert "agent123" in s["detail"]


def test_check_connection_error_on_generic_http_failure(monkeypatch):
    _configure(monkeypatch)
    import requests

    class _Resp:
        status_code = 500
        ok = False
        text = "server error"
        def json(self): return {}

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    assert cc.check_connection()["state"] == "ERROR"


def test_check_connection_error_on_network_exception(monkeypatch):
    _configure(monkeypatch)
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(requests.RequestException("no network")))
    s = cc.check_connection()
    assert s["state"] == "ERROR"
    assert "unreachable" in s["detail"].lower()


# ── place_call: never fake success, never call with a bad number ─────

def test_place_call_refuses_when_not_configured(monkeypatch):
    _configure(monkeypatch, api_key=None, agent_id=None, phone_id=None)
    r = cc.place_call("+13125550100", "test reason")
    assert r["ok"] is False
    assert r["state"] == "NOT_CONFIGURED"


def test_place_call_falls_back_to_owner_phone_when_no_number_given(monkeypatch):
    _configure(monkeypatch, owner_phone="+13125550100")
    import requests

    class _Resp:
        status_code = 201
        content = b"{}"
        ok = True
        def json(self): return {"calls": [{"agent_call_id": "call_1"}]}

    captured = {}
    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["payload"] = json
        return _Resp()
    monkeypatch.setattr(requests, "post", _fake_post)

    r = cc.place_call("", "checking in")
    assert r["ok"] is True
    assert r["to_number"] == "+13125550100"
    assert captured["payload"]["outbound_calls"][0]["to_number"] == "+13125550100"


def test_place_call_no_recipient_and_no_owner_phone_is_an_honest_failure(monkeypatch):
    _configure(monkeypatch, owner_phone=None)
    r = cc.place_call("", "test reason")
    assert r["ok"] is False
    assert r["state"] == "NO_RECIPIENT"


def test_place_call_rejects_non_e164_number_without_calling_the_network(monkeypatch):
    _configure(monkeypatch)
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call the network")))
    r = cc.place_call("555-1234", "test reason")
    assert r["ok"] is False
    assert r["state"] == "BAD_NUMBER"


def test_place_call_success_returns_agent_call_id_and_reason(monkeypatch):
    _configure(monkeypatch)
    import requests

    class _Resp:
        status_code = 201
        content = b"{}"
        ok = True
        def json(self): return {"calls": [{"agent_call_id": "call_42"}]}

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
    r = cc.place_call("+13125550199", "the Henderson contract came back signed")
    assert r["ok"] is True
    assert r["state"] == "CALLING"
    assert r["agent_call_id"] == "call_42"
    assert r["reason"] == "the Henderson contract came back signed"


def test_place_call_passes_reason_and_extra_metadata_through(monkeypatch):
    _configure(monkeypatch)
    import requests
    captured = {}

    class _Resp:
        status_code = 201
        content = b"{}"
        ok = True
        def json(self): return {"calls": [{"agent_call_id": "call_1"}]}

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["payload"] = json
        return _Resp()
    monkeypatch.setattr(requests, "post", _fake_post)

    cc.place_call("+13125550199", "urgent", metadata={"task_id": "abc"})
    meta = captured["payload"]["outbound_calls"][0]["metadata"]
    assert meta["reason"] == "urgent"
    assert meta["task_id"] == "abc"


def test_place_call_unauthorized_on_401(monkeypatch):
    _configure(monkeypatch)
    import requests

    class _Resp:
        status_code = 401
        content = b"{}"
        ok = False
        text = "unauthorized"
        def json(self): return {}

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
    r = cc.place_call("+13125550199", "test")
    assert r["ok"] is False
    assert r["state"] == "UNAUTHORIZED"


def test_place_call_error_on_generic_http_failure(monkeypatch):
    _configure(monkeypatch)
    import requests

    class _Resp:
        status_code = 500
        content = b"{}"
        ok = False
        text = "server error"
        def json(self): return {}

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
    r = cc.place_call("+13125550199", "test")
    assert r["ok"] is False
    assert r["state"] == "ERROR"


def test_place_call_network_exception_is_captured_not_raised(monkeypatch):
    _configure(monkeypatch)
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: (_ for _ in ()).throw(requests.RequestException("timeout")))
    r = cc.place_call("+13125550199", "test")
    assert r["ok"] is False
    assert r["state"] == "ERROR"


# ── get_history ──────────────────────────────────────

def test_get_history_not_configured_without_api_key(monkeypatch):
    _configure(monkeypatch, api_key=None, agent_id=None, phone_id=None)
    r = cc.get_history()
    assert r["ok"] is False
    assert r["state"] == "NOT_CONFIGURED"


def test_get_history_success_returns_calls(monkeypatch):
    _configure(monkeypatch)
    import requests

    class _Resp:
        ok = True
        content = b"{}"
        def json(self): return {"data": [{"agent_call_id": "call_1", "status": "completed"}]}

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    r = cc.get_history(limit=5)
    assert r["ok"] is True
    assert r["calls"][0]["agent_call_id"] == "call_1"


def test_get_history_error_on_http_failure(monkeypatch):
    _configure(monkeypatch)
    import requests

    class _Resp:
        ok = False
        status_code = 500
        text = "server error"

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    r = cc.get_history()
    assert r["ok"] is False
    assert r["state"] == "ERROR"


def test_get_history_network_exception_is_captured_not_raised(monkeypatch):
    _configure(monkeypatch)
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(requests.RequestException("no network")))
    r = cc.get_history()
    assert r["ok"] is False
    assert r["state"] == "ERROR"
