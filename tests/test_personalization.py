"""Phase 4 personalization system — memory/preferences_manager.py (the
new preferences.json store) and core/headless/personalization.py (the
merged settings API that also covers voice/api_keys.json and the
existing morning-briefing flag, without duplicating either).
"""
import pytest

from memory import preferences_manager as prefs
from memory import config_manager
from actions import voice_manager
from core.headless import personalization


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(prefs, "PREFS_FILE", tmp_path / "preferences.json")
    monkeypatch.setattr(prefs, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(config_manager, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(voice_manager, "CONFIG_PATH", tmp_path / "api_keys.json")


# ── preferences_manager.py ───────────────────────────────────────────

def test_load_preferences_returns_defaults_when_no_file_exists():
    loaded = prefs.load_preferences()
    assert loaded["business_identity"] == "executive"
    assert loaded["alert_sensitivity"] == "normal"
    assert prefs.PREFS_FILE.exists()   # written on first load, same pattern as the other config files


def test_save_preferences_persists_and_reloads():
    prefs.save_preferences({"theme": "light", "accent_color": "#ff0000"})
    reloaded = prefs.load_preferences()
    assert reloaded["theme"] == "light"
    assert reloaded["accent_color"] == "#ff0000"


def test_save_preferences_rejects_unknown_business_identity():
    with pytest.raises(ValueError, match="business_identity"):
        prefs.save_preferences({"business_identity": "not_a_real_business"})


def test_save_preferences_rejects_unknown_alert_sensitivity():
    with pytest.raises(ValueError, match="alert_sensitivity"):
        prefs.save_preferences({"alert_sensitivity": "screaming"})


def test_avatar_position_defaults_to_right():
    assert prefs.load_preferences()["avatar_position"] == "right"


def test_save_preferences_persists_avatar_position():
    prefs.save_preferences({"avatar_position": "left"})
    assert prefs.load_preferences()["avatar_position"] == "left"


def test_save_preferences_rejects_unknown_avatar_position():
    with pytest.raises(ValueError, match="avatar_position"):
        prefs.save_preferences({"avatar_position": "top"})


def test_voice_volume_is_clamped_to_valid_range():
    prefs.save_preferences({"voice_volume": 9.0})
    assert prefs.load_preferences()["voice_volume"] == 1.5
    prefs.save_preferences({"voice_volume": -5.0})
    assert prefs.load_preferences()["voice_volume"] == 0.0


def test_corrupt_preferences_file_is_backed_up_not_silently_discarded(tmp_path):
    prefs.PREFS_FILE.write_text("{not valid json", encoding="utf-8")
    loaded = prefs.load_preferences()
    assert loaded["business_identity"] == "executive"   # falls back to defaults
    backups = list(tmp_path.glob("preferences.corrupt-*.json"))
    assert len(backups) == 1


def test_partial_update_does_not_wipe_other_fields():
    prefs.save_preferences({"theme": "light"})
    prefs.save_preferences({"accent_color": "#00ff00"})
    reloaded = prefs.load_preferences()
    assert reloaded["theme"] == "light"
    assert reloaded["accent_color"] == "#00ff00"


# ── personalization.py — merged settings API ─────────────────────────

def test_get_all_settings_merges_all_three_sources():
    settings = personalization.get_all_settings()
    assert "business_identity" in settings          # preferences.json
    assert "voice_provider" in settings              # api_keys.json via voice_manager
    assert "startup_briefing_enabled" in settings    # api_keys.json via config_manager
    assert settings["available_voices"]              # derived, not stored


def test_update_settings_routes_voice_fields_to_voice_manager():
    personalization.update_settings({"voice_provider": "elevenlabs", "voice_speed": 1.3})
    voice_cfg = voice_manager.get_voice_provider_config()
    assert voice_cfg["provider"] == "elevenlabs"
    assert voice_cfg["speed"] == 1.3
    # And did NOT get written into preferences.json as a duplicate.
    assert "voice_provider" not in prefs.load_preferences()


def test_update_settings_routes_briefing_flag_to_config_manager():
    personalization.update_settings({"startup_briefing_enabled": False})
    assert config_manager.get_brief_enabled() is False
    assert "startup_briefing_enabled" not in prefs.load_preferences()


def test_update_settings_routes_appearance_fields_to_preferences():
    personalization.update_settings({"business_identity": "buildpro", "theme": "light"})
    p = prefs.load_preferences()
    assert p["business_identity"] == "buildpro"
    assert p["theme"] == "light"


def test_update_settings_handles_a_mixed_update_across_all_stores():
    result = personalization.update_settings({
        "business_identity": "ddf",
        "voice_speed": 1.1,
        "startup_briefing_enabled": False,
    })
    assert result["business_identity"] == "ddf"
    assert result["voice_speed"] == 1.1
    assert result["startup_briefing_enabled"] is False


def test_update_settings_rejects_invalid_values_without_partial_writes():
    personalization.update_settings({"theme": "light"})
    with pytest.raises(ValueError):
        personalization.update_settings({"business_identity": "not_real"})
    # theme change from before must survive an unrelated rejected update
    assert prefs.load_preferences()["theme"] == "light"
