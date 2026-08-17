"""memory/config_manager.py: schema defaults, round trips, atomic writes,
and corruption recovery. This file holds real credentials, so a silent
corruption-then-overwrite bug here is worse than in memory_manager.py — a
corrupted read used to fall back to DEFAULT_CONFIG (empty secrets) in
memory, and the next save_config() call would overwrite the real file with
those blanks, permanently losing every API key it held.
"""
import threading

import pytest

import memory.config_manager as cm


@pytest.fixture(autouse=True)
def _isolate_config_file(tmp_path, monkeypatch):
    fake_dir = tmp_path / "config"
    fake_file = fake_dir / "api_keys.json"
    monkeypatch.setattr(cm, "CONFIG_DIR", fake_dir)
    monkeypatch.setattr(cm, "CONFIG_FILE", fake_file)
    yield fake_file


# ── schema / defaults ────────────────────────────────────────────────────

def test_default_config_has_no_dead_keys():
    # These used to be in DEFAULT_CONFIG but are never read by any real
    # integration anywhere in the codebase — dead scaffolding that
    # misleadingly implied those integrations existed. (airtable_token was
    # in this list too until actions/airtable_integration.py was built and
    # wired in — it's a real, consumed key now, not dead scaffolding.)
    dead_keys = {
        "github_token", "vercel_token", "make_api_token",
        "google_credentials", "microsoft_credentials",
    }
    assert dead_keys.isdisjoint(cm.DEFAULT_CONFIG.keys())


def test_default_config_has_the_keys_real_integrations_actually_read():
    for key in ("gemini_api_key", "hubspot_token", "buffer_token", "airtable_token", "twilio"):
        assert key in cm.DEFAULT_CONFIG
    assert set(cm.DEFAULT_CONFIG["twilio"].keys()) == {"account_sid", "auth_token", "from_number"}


def test_load_api_keys_creates_the_file_on_first_call(_isolate_config_file):
    assert not _isolate_config_file.exists()
    data = cm.load_api_keys()
    assert data == cm.DEFAULT_CONFIG
    assert _isolate_config_file.exists()


# ── round trips ──────────────────────────────────────────────────────────

def test_save_and_load_round_trip():
    cm.save_config({"gemini_api_key": "test-key-123"})
    data = cm.load_api_keys()
    assert data["gemini_api_key"] == "test-key-123"


def test_save_config_preserves_unrelated_existing_keys():
    cm.save_config({"gemini_api_key": "key-1"})
    cm.save_config({"hubspot_token": "hs-1"})
    data = cm.load_api_keys()
    assert data["gemini_api_key"] == "key-1"
    assert data["hubspot_token"] == "hs-1"


def test_save_config_preserves_keys_outside_the_default_schema(_isolate_config_file):
    # e.g. "os_system" in the real repo's config/api_keys.json — extra keys
    # not in DEFAULT_CONFIG must survive a save, not get silently dropped.
    _isolate_config_file.parent.mkdir(parents=True, exist_ok=True)
    _isolate_config_file.write_text('{"os_system": "windows"}', encoding="utf-8")
    cm.save_config({"gemini_api_key": "key-1"})
    data = cm.load_api_keys()
    assert data["os_system"] == "windows"
    assert data["gemini_api_key"] == "key-1"


def test_save_credential_and_get_credential_round_trip():
    cm.save_credential("hubspot_token", "  hs-secret  ")
    assert cm.get_credential("hubspot_token") == "hs-secret"


def test_assistant_config_round_trip():
    cm.save_assistant_config("Friday", "Tony")
    assert cm.get_assistant_name() == "Friday"
    assert cm.get_user_name() == "Tony"


def test_is_configured_reflects_gemini_key_length():
    assert cm.is_configured() is False
    cm.save_api_keys("short")
    assert cm.is_configured() is False
    cm.save_api_keys("a-real-looking-gemini-key-value")
    assert cm.is_configured() is True


# ── proactive engine controls ─────────────────────────────────────────

def test_proactive_enabled_defaults_true():
    assert cm.get_proactive_enabled() is True


def test_proactive_enabled_round_trip():
    cm.save_proactive_enabled(False)
    assert cm.get_proactive_enabled() is False
    cm.save_proactive_enabled(True)
    assert cm.get_proactive_enabled() is True


def test_proactive_quiet_hours_default():
    assert cm.get_proactive_quiet_hours() == (22, 8)


def test_proactive_quiet_hours_round_trip():
    cm.save_proactive_quiet_hours(20, 9)
    assert cm.get_proactive_quiet_hours() == (20, 9)


def test_proactive_quiet_hours_can_be_disabled():
    cm.save_proactive_quiet_hours(None, None)
    assert cm.get_proactive_quiet_hours() is None


def test_proactive_quiet_hours_malformed_value_degrades_to_none(_isolate_config_file):
    _isolate_config_file.parent.mkdir(parents=True, exist_ok=True)
    _isolate_config_file.write_text('{"proactive_quiet_hours": "not-a-list"}', encoding="utf-8")
    assert cm.get_proactive_quiet_hours() is None


# ── atomic writes / corruption recovery ─────────────────────────────────

def test_write_leaves_no_temp_file_behind(_isolate_config_file):
    cm.save_config({"gemini_api_key": "key-1"})
    assert list(_isolate_config_file.parent.glob("api_keys.tmp-*")) == []
    assert _isolate_config_file.exists()


def test_corrupted_file_is_backed_up_not_silently_discarded(_isolate_config_file):
    _isolate_config_file.parent.mkdir(parents=True, exist_ok=True)
    _isolate_config_file.write_text("{not valid json", encoding="utf-8")

    data = cm.load_api_keys()

    assert data == cm.DEFAULT_CONFIG
    backups = list(_isolate_config_file.parent.glob("api_keys.corrupt-*.json"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{not valid json"


def test_a_save_after_corruption_does_not_silently_wipe_real_keys(_isolate_config_file):
    # The exact scenario the docstring warns about: corrupted read must not
    # let a subsequent save overwrite real secrets with blanks unnoticed —
    # the corrupted original is preserved as a backup, recoverable by hand.
    _isolate_config_file.parent.mkdir(parents=True, exist_ok=True)
    _isolate_config_file.write_text('{"gemini_api_key": "real-secret-that-got-corrup', encoding="utf-8")

    cm.save_config({"assistant_name": "Friday"})  # any save after the corrupted load

    backups = list(_isolate_config_file.parent.glob("api_keys.corrupt-*.json"))
    assert len(backups) == 1
    assert "real-secret-that-got-corrup" in backups[0].read_text(encoding="utf-8")


# ── concurrency ──────────────────────────────────────────────────────────

def test_concurrent_save_credential_calls_do_not_lose_writes():
    services = [f"service_{i}" for i in range(20)]
    threads = [threading.Thread(target=cm.save_credential, args=(s, s)) for s in services]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    data = cm.load_api_keys()
    for s in services:
        assert data[s] == s
