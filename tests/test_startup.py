"""core/startup.py — the single-instance app lock (dead-PID-then-age
reclaim, mirroring actions/agent_orchestrator.py's already-tested
scheduler lock), the dashboard port pre-flight check, and the
secret-free startup config summary.

No real server, process, or persistent service is started by any test
here — acquire/release operate purely on a tmp_path lock file (isolated
by tests/conftest.py's autouse _isolate_app_instance_lock fixture), and
the port-conflict test binds a real localhost TCP socket only to prove
is_port_free() correctly reports it busy, then closes it immediately.
"""
import json
import os
import socket

import pytest

from core import startup


# ── single-instance app lock ────────────────────────────────────────────

def test_acquire_app_instance_lock_succeeds_when_free():
    acquired, holder = startup.acquire_app_instance_lock()
    assert acquired is True
    assert holder is None
    assert startup.APP_LOCK_PATH.exists()


def test_acquire_app_instance_lock_is_reentrant_for_the_same_process():
    assert startup.acquire_app_instance_lock()[0] is True
    assert startup.acquire_app_instance_lock()[0] is True   # same PID re-acquiring its own lock


def test_acquire_app_instance_lock_fails_when_a_live_process_holds_it(monkeypatch):
    assert startup.acquire_app_instance_lock()[0] is True
    monkeypatch.setattr(startup, "_pid_is_running", lambda pid: True)
    # Pretend a different, live PID actually wrote the lock.
    data = json.loads(startup.APP_LOCK_PATH.read_text(encoding="utf-8"))
    data["pid"] = data["pid"] + 1
    startup.APP_LOCK_PATH.write_text(json.dumps(data), encoding="utf-8")

    acquired, holder = startup.acquire_app_instance_lock()
    assert acquired is False
    assert holder["pid"] == data["pid"]


def test_stale_app_lock_from_a_dead_pid_is_reclaimed(monkeypatch):
    assert startup.acquire_app_instance_lock()[0] is True
    monkeypatch.setattr(startup, "_pid_is_running", lambda pid: False)   # simulate the old owner is gone
    acquired, holder = startup.acquire_app_instance_lock()
    assert acquired is True
    assert holder is None


def test_stale_app_lock_by_age_is_reclaimed_even_if_pid_check_would_say_alive(monkeypatch):
    assert startup.acquire_app_instance_lock()[0] is True
    monkeypatch.setattr(startup, "_pid_is_running", lambda pid: True)
    # Backdate the lock file well past the staleness window.
    import time as _time
    data = json.loads(startup.APP_LOCK_PATH.read_text(encoding="utf-8"))
    data["acquired_ts"] = _time.time() - startup._STALE_APP_LOCK_SECONDS - 10
    startup.APP_LOCK_PATH.write_text(json.dumps(data), encoding="utf-8")

    acquired, holder = startup.acquire_app_instance_lock()
    assert acquired is True   # age-based staleness wins regardless of "liveness"


def test_corrupt_app_lock_file_is_treated_as_stale_and_reclaimed():
    startup.APP_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    startup.APP_LOCK_PATH.write_text("not json{{{", encoding="utf-8")
    acquired, holder = startup.acquire_app_instance_lock()
    assert acquired is True
    assert holder is None


def test_release_app_instance_lock_removes_the_file_when_owned():
    startup.acquire_app_instance_lock()
    assert startup.APP_LOCK_PATH.exists()
    startup.release_app_instance_lock()
    assert not startup.APP_LOCK_PATH.exists()


def test_release_app_instance_lock_does_not_remove_a_lock_owned_by_another_pid():
    startup.acquire_app_instance_lock()
    data = json.loads(startup.APP_LOCK_PATH.read_text(encoding="utf-8"))
    data["pid"] = data["pid"] + 1   # pretend a different process owns it
    startup.APP_LOCK_PATH.write_text(json.dumps(data), encoding="utf-8")

    startup.release_app_instance_lock()
    assert startup.APP_LOCK_PATH.exists()   # untouched — not ours to remove


def test_check_single_instance_warns_but_never_blocks_when_another_instance_is_live(monkeypatch, capsys):
    startup.acquire_app_instance_lock()
    monkeypatch.setattr(startup, "_pid_is_running", lambda pid: True)
    data = json.loads(startup.APP_LOCK_PATH.read_text(encoding="utf-8"))
    data["pid"] = data["pid"] + 1
    startup.APP_LOCK_PATH.write_text(json.dumps(data), encoding="utf-8")

    result = startup.check_single_instance()   # must never raise or exit
    assert result is False
    out = capsys.readouterr().out
    assert "another JARVIS instance" in out


def test_check_single_instance_is_quiet_when_free(capsys):
    result = startup.check_single_instance()
    assert result is True
    out = capsys.readouterr().out
    assert "WARNING" not in out


def test_graceful_release_all_locks_removes_the_app_lock_when_owned():
    startup.acquire_app_instance_lock()
    startup.graceful_release_all_locks()
    assert not startup.APP_LOCK_PATH.exists()


def test_graceful_release_all_locks_is_safe_to_call_when_nothing_is_held():
    assert not startup.APP_LOCK_PATH.exists()
    startup.graceful_release_all_locks()   # must not raise
    assert not startup.APP_LOCK_PATH.exists()


def test_graceful_release_all_locks_also_releases_the_scheduler_lock(monkeypatch, tmp_path):
    from actions import agent_orchestrator as ao
    monkeypatch.setattr(ao, "LOCK_PATH", tmp_path / "scheduler_test.lock")
    assert ao.acquire_scheduler_lock() is True
    assert ao.LOCK_PATH.exists()

    startup.graceful_release_all_locks()
    assert not ao.LOCK_PATH.exists()


# ── port pre-flight check ───────────────────────────────────────────────

def test_is_port_free_reports_true_for_an_unbound_port():
    # Bind a probe socket only to find an ephemeral free port, then close
    # it immediately before the real check — the check itself never holds
    # a socket of its own.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()
    assert startup.is_port_free(free_port, host="127.0.0.1") is True


def test_is_port_free_reports_false_for_a_bound_port():
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    bound_port = holder.getsockname()[1]
    try:
        assert startup.is_port_free(bound_port, host="127.0.0.1") is False
    finally:
        holder.close()


# ── startup config summary ──────────────────────────────────────────────
# 2026-09-02 reliability audit finding: this used to check ONLY
# config/api_keys.json and require "gemini_api_key" — wrong on both counts
# for the real Render deployment (env-var configured, Ollama is the
# required LLM provider; see core/startup.py's comment above
# REQUIRED_CONFIG_KEYS). These tests now monkeypatch the actual env-backed
# config attributes _is_key_present() reads, the same source every
# integration module reads at call time, so they exercise the real
# production code path instead of the stale file-only one.

def _clear_all_config_env(monkeypatch):
    """Zeroes every attribute _is_key_present() can see, so a test starts
    from a known "nothing configured" baseline regardless of what's
    actually set in this sandbox's environment/.env."""
    from core.headless import config as headless_config
    for attr in (
        "OLLAMA_API_KEY", "GEMINI_API_KEY", "HUBSPOT_TOKEN", "BUFFER_TOKEN",
        "AIRTABLE_TOKEN", "CARTESIA_API_KEY",
        "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
    ):
        monkeypatch.setattr(headless_config, attr, None, raising=False)


def test_summarize_startup_config_reports_missing_required_and_optional_keys(tmp_path, monkeypatch):
    _clear_all_config_env(monkeypatch)
    cfg = tmp_path / "api_keys.json"
    cfg.write_text(json.dumps({}), encoding="utf-8")
    summary = startup.summarize_startup_config(cfg)
    assert summary["config_readable"] is True
    assert "ollama_api_key" in summary["required_missing"]
    assert set(summary["optional_missing"]) == set(startup.OPTIONAL_CONFIG_KEYS)


def test_summarize_startup_config_reports_present_keys_from_env(tmp_path, monkeypatch):
    """The real production path: Render sets environment variables, not
    config/api_keys.json (which doesn't exist on Render's disk-less free
    plan — see render.yaml). A key configured only via env must be
    reported present even when the file is empty."""
    _clear_all_config_env(monkeypatch)
    from core.headless import config as headless_config
    monkeypatch.setattr(headless_config, "OLLAMA_API_KEY", "real-ollama-key", raising=False)
    monkeypatch.setattr(headless_config, "BUFFER_TOKEN", "real-buffer-token", raising=False)
    cfg = tmp_path / "api_keys.json"
    cfg.write_text(json.dumps({}), encoding="utf-8")
    summary = startup.summarize_startup_config(cfg)
    assert summary["required_missing"] == []
    assert "buffer_token" not in summary["optional_missing"]
    assert "hubspot_token" in summary["optional_missing"]


def test_summarize_startup_config_reports_present_keys_from_file(tmp_path, monkeypatch):
    """Local-dev fallback: no env vars set, but api_keys.json has the key."""
    _clear_all_config_env(monkeypatch)
    cfg = tmp_path / "api_keys.json"
    cfg.write_text(json.dumps({"ollama_api_key": "x", "twilio": {"auth_token": "y"}}), encoding="utf-8")
    summary = startup.summarize_startup_config(cfg)
    assert summary["required_missing"] == []
    assert "twilio" not in summary["optional_missing"]
    assert "hubspot_token" in summary["optional_missing"]


def test_summarize_startup_config_handles_missing_file_without_raising(tmp_path, monkeypatch):
    _clear_all_config_env(monkeypatch)
    summary = startup.summarize_startup_config(tmp_path / "does_not_exist.json")
    assert summary["required_missing"] == ["ollama_api_key"]


def test_summarize_startup_config_handles_corrupt_json_without_raising(tmp_path, monkeypatch):
    _clear_all_config_env(monkeypatch)
    cfg = tmp_path / "api_keys.json"
    cfg.write_text("not json{{{", encoding="utf-8")
    summary = startup.summarize_startup_config(cfg)
    assert summary["config_readable"] is False
    assert summary["required_missing"] == ["ollama_api_key"]


def test_print_startup_banner_never_prints_a_config_value(tmp_path, monkeypatch, capsys):
    _clear_all_config_env(monkeypatch)
    from core.headless import config as headless_config
    secret_value = "sk-super-secret-value-should-never-appear"
    monkeypatch.setattr(headless_config, "OLLAMA_API_KEY", secret_value, raising=False)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    monkeypatch.setattr(startup, "BASE_DIR", tmp_path)
    startup.print_startup_banner()
    out = capsys.readouterr().out
    assert secret_value not in out
    assert "Required configuration present" in out


def test_print_startup_banner_reports_required_missing_when_ollama_unset(tmp_path, monkeypatch, capsys):
    """Guards the exact bug Lee reported: a Render deploy with real secrets
    set as env vars, and no config/api_keys.json on disk, must not report
    everything as missing just because the file is absent."""
    _clear_all_config_env(monkeypatch)
    from core.headless import config as headless_config
    monkeypatch.setattr(headless_config, "OLLAMA_API_KEY", "real-key", raising=False)
    monkeypatch.setattr(headless_config, "HUBSPOT_TOKEN", "real-hubspot-token", raising=False)
    monkeypatch.setattr(startup, "BASE_DIR", tmp_path)  # config/api_keys.json under here does not exist
    startup.print_startup_banner()
    out = capsys.readouterr().out
    assert "Required configuration present" in out
    assert "hubspot_token" not in out.split("Optional integrations not configured")[-1]
