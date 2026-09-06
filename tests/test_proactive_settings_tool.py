"""main.py's "proactive_settings" voice tool — status/enable/disable/
snooze/history wiring into actions/proactive.py + memory/config_manager.py.
No live network/DB calls needed: config functions and the activity trail
are monkeypatched.
"""
import asyncio
import importlib.util
from pathlib import Path

from actions import proactive as proactive_module
from memory import config_manager

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_live(name="jarvis_main_proactive_settings"):
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
    return type("FC", (), {"id": "call-1", "name": "proactive_settings", "args": args})()


def _run(coro):
    return asyncio.run(coro)


def _live(main):
    live = object.__new__(main.JarvisLive)
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    live._proactive = main.ProactiveEngine()
    return live


def test_tool_declared_for_gemini():
    main, _ = _new_live()
    names = [t["name"] for t in main.TOOL_DECLARATIONS]
    assert "proactive_settings" in names


# ── status ────────────────────────────────────────────────────────────

def test_status_reports_enabled_and_quiet_hours(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(config_manager, "get_proactive_enabled", lambda: True)
    monkeypatch.setattr(config_manager, "get_proactive_quiet_hours", lambda: (22, 8))

    response = _run(live._execute_tool(_make_fc(action="status")))
    result = response.response["result"]
    assert "enabled" in result.lower()
    assert "22:00" in result and "8:00" in result


def test_status_reports_disabled(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(config_manager, "get_proactive_enabled", lambda: False)
    monkeypatch.setattr(config_manager, "get_proactive_quiet_hours", lambda: None)

    response = _run(live._execute_tool(_make_fc(action="status")))
    assert "disabled" in response.response["result"].lower()


def test_status_reports_currently_snoozed(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(config_manager, "get_proactive_enabled", lambda: True)
    monkeypatch.setattr(config_manager, "get_proactive_quiet_hours", lambda: None)
    live._proactive.snooze(1800)

    response = _run(live._execute_tool(_make_fc(action="status")))
    assert "snoozed" in response.response["result"].lower()


# ── enable / disable ──────────────────────────────────────────────────

def test_enable_calls_save_with_true(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    captured = {}
    monkeypatch.setattr(config_manager, "save_proactive_enabled", lambda v: captured.__setitem__("v", v))

    response = _run(live._execute_tool(_make_fc(action="enable")))
    assert captured["v"] is True
    assert "enabled" in response.response["result"].lower()


def test_disable_calls_save_with_false(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    captured = {}
    monkeypatch.setattr(config_manager, "save_proactive_enabled", lambda v: captured.__setitem__("v", v))

    response = _run(live._execute_tool(_make_fc(action="disable")))
    assert captured["v"] is False
    assert "disabled" in response.response["result"].lower()


# ── snooze ────────────────────────────────────────────────────────────

def test_snooze_default_duration_is_60_minutes():
    main, _ = _new_live()
    live = _live(main)

    response = _run(live._execute_tool(_make_fc(action="snooze")))
    assert live._proactive.is_snoozed() is True
    assert "60 minute" in response.response["result"]


def test_snooze_forwards_custom_minutes():
    main, _ = _new_live()
    live = _live(main)

    _run(live._execute_tool(_make_fc(action="snooze", minutes=15)))
    remaining = live._proactive.snoozed_remaining_secs()
    assert 890 <= remaining <= 900   # ~15 minutes in seconds, allowing for test runtime


# ── history ───────────────────────────────────────────────────────────

def test_history_reports_none_recorded_when_empty(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(proactive_module, "get_recent_triggers", lambda limit: [])

    response = _run(live._execute_tool(_make_fc(action="history")))
    assert "no proactive check-ins recorded" in response.response["result"].lower()


def test_history_summarizes_entries(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(proactive_module, "get_recent_triggers", lambda limit: [
        {"triggered_ts": 1780000000.0, "focus_area": "wellbeing_checkin"},
    ])

    response = _run(live._execute_tool(_make_fc(action="history")))
    assert "wellbeing_checkin" in response.response["result"]


def test_history_forwards_limit(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    captured = {}

    def _fake_history(limit):
        captured["limit"] = limit
        return []

    monkeypatch.setattr(proactive_module, "get_recent_triggers", _fake_history)
    _run(live._execute_tool(_make_fc(action="history", limit=3)))
    assert captured["limit"] == 3


def test_unknown_proactive_settings_action_reports_clearly():
    main, _ = _new_live()
    live = _live(main)

    response = _run(live._execute_tool(_make_fc(action="something_else")))
    assert "unknown proactive_settings action" in response.response["result"].lower()
