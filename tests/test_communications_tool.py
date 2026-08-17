import asyncio
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_live(name="jarvis_main_comms"):
    main = load_module(name, "main.py")
    live = object.__new__(main.JarvisLive)
    return main, live


def _ui_stub():
    return type("UIStub", (), {
        "muted": False,
        "set_state": lambda self, s: None,
        "write_log": lambda self, m: None,
    })()


def _make_fc(**args):
    return type("FC", (), {"id": "call-1", "name": "communications", "args": args})()


def _run(coro):
    return asyncio.run(coro)


def _isolate_twilio(monkeypatch, main, tmp_path, cfg=None):
    cfg_path = tmp_path / "api_keys.json"
    cfg_path.write_text(json.dumps({"twilio": cfg or {}}), encoding="utf-8")
    monkeypatch.setattr(main.twilio, "API_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(main.twilio, "DB_PATH", tmp_path / "test_comms.db")


def test_tool_declared_for_gemini():
    main, _ = _new_live()
    names = [t["name"] for t in main.TOOL_DECLARATIONS]
    assert "communications" in names


def test_status_action_reports_not_configured_by_default(monkeypatch, tmp_path):
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    _isolate_twilio(monkeypatch, main, tmp_path, {})

    response = _run(live._execute_tool(_make_fc(action="status")))
    assert "NOT_CONFIGURED" in response.response["result"]


def test_send_sms_without_recipient_or_body_is_refused(monkeypatch, tmp_path):
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    _isolate_twilio(monkeypatch, main, tmp_path, {"account_sid": "AC1", "auth_token": "t", "from_number": "+15551234567"})

    response = _run(live._execute_tool(_make_fc(action="send_sms")))
    assert "recipient" in response.response["result"].lower()


def test_send_sms_not_configured_never_claims_success(monkeypatch, tmp_path):
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    _isolate_twilio(monkeypatch, main, tmp_path, {})

    response = _run(live._execute_tool(_make_fc(action="send_sms", to="+15559876543", body="hi")))
    result = response.response["result"].lower()
    assert "couldn't send" in result
    assert "not_configured" in result


def test_call_without_recipient_asks_who(monkeypatch, tmp_path):
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    _isolate_twilio(monkeypatch, main, tmp_path, {"account_sid": "AC1", "auth_token": "t", "from_number": "+15551234567"})

    response = _run(live._execute_tool(_make_fc(action="call")))
    assert "who" in response.response["result"].lower()


def test_send_sms_success_reports_sent(monkeypatch, tmp_path):
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    _isolate_twilio(monkeypatch, main, tmp_path, {"account_sid": "AC1", "auth_token": "t", "from_number": "+15551234567"})

    import requests
    class _Resp:
        status_code = 201
        def json(self): return {"sid": "SM1", "status": "queued"}
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())

    response = _run(live._execute_tool(_make_fc(action="send_sms", to="+15559876543", body="On my way")))
    assert "sent" in response.response["result"].lower()


def test_lookup_contact_resolves_a_literal_number(monkeypatch, tmp_path):
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    _isolate_twilio(monkeypatch, main, tmp_path, {})

    response = _run(live._execute_tool(_make_fc(action="lookup_contact", to="+15559876543")))
    assert "+15559876543" in response.response["result"]


def test_missed_calls_reports_none_when_empty(monkeypatch, tmp_path):
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    _isolate_twilio(monkeypatch, main, tmp_path, {})

    response = _run(live._execute_tool(_make_fc(action="missed_calls")))
    assert "no missed calls" in response.response["result"].lower()
