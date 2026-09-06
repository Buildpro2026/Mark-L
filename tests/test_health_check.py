"""Tests for scripts/health_check.py — a non-destructive environment check.

These tests exercise the check functions directly. They never make network
calls and never assert on the presence/absence of real credentials (that's
environment-dependent), only that each check function runs without raising
and returns well-formed CheckResult objects.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import health_check as hc  # noqa: E402


def test_check_python_version_returns_result():
    r = hc.check_python_version()
    assert r.name == "python_version"
    assert isinstance(r.ok, bool)
    assert r.detail


def test_check_modules_covers_required_list():
    results = hc.check_modules()
    names = {r.name for r in results}
    assert names == {f"module:{m}" for m in hc.REQUIRED_MODULES}
    for r in results:
        assert isinstance(r.ok, bool)


def test_check_api_keys_config_reports_gemini_and_optional_keys(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "api_keys.json").write_text(
        '{"gemini_api_key": "x", "hubspot_token": "y"}', encoding="utf-8"
    )
    monkeypatch.setattr(hc, "BASE_DIR", tmp_path)
    results = {r.name: r for r in hc.check_api_keys_config()}
    assert results["gemini_api_key"].ok is True
    assert results["optional:hubspot_token"].ok is True
    assert results["optional:twilio"].ok is False
    assert results["optional:buffer_token"].ok is False


def test_check_api_keys_config_missing_file_fails_cleanly(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "BASE_DIR", tmp_path)
    results = hc.check_api_keys_config()
    assert len(results) == 1
    assert results[0].ok is False
    assert "does not exist" in results[0].detail


def test_check_api_keys_config_invalid_json_fails_cleanly(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "api_keys.json").write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(hc, "BASE_DIR", tmp_path)
    results = hc.check_api_keys_config()
    assert results[0].ok is False
    assert "invalid JSON" in results[0].detail


def test_check_memory_file_missing_is_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "BASE_DIR", tmp_path)
    r = hc.check_memory_file()
    assert r.ok is True
    assert "does not exist yet" in r.detail


def test_check_memory_file_invalid_json_fails(tmp_path, monkeypatch):
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    (mem_dir / "long_term.json").write_text("{bad", encoding="utf-8")
    monkeypatch.setattr(hc, "BASE_DIR", tmp_path)
    r = hc.check_memory_file()
    assert r.ok is False


def test_check_data_db_missing_is_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "BASE_DIR", tmp_path)
    r = hc.check_data_db()
    assert r.ok is True
    assert "does not exist yet" in r.detail


def test_check_voice_provider_deps_reports_miniaudio_and_kokoro_torch():
    results = {r.name: r for r in hc.check_voice_provider_deps()}
    assert set(results) == {"voice:elevenlabs_playback", "voice:local_kokoro"}
    # This session installed miniaudio to fix the real ElevenLabs/EdgeTTS
    # playback bug — confirm the health check actually detects it.
    assert results["voice:elevenlabs_playback"].ok is True
    for r in results.values():
        assert isinstance(r.ok, bool)
        assert r.detail


def test_run_health_check_against_real_repo_never_raises():
    """Runs every check against the actual repo (read-only) to make sure
    nothing crashes — the strongest guarantee we can give without depending
    on any particular machine's credential state."""
    results = hc.run_health_check()
    assert len(results) > 0
    for r in results:
        assert isinstance(r.ok, bool)
        assert r.detail
