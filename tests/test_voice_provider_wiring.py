import importlib.util
import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── actions/voice_manager.py: build_tts_player() provider -> engine mapping ──

def test_build_tts_player_returns_none_for_gemini():
    vm = load_module("jarvis_voice_manager", "actions/voice_manager.py")
    assert vm.build_tts_player({"provider": "gemini", "voice": "Charon", "speed": 1.0}) is None


def test_build_tts_player_maps_local_to_kokoro_and_drops_gemini_voice_name(monkeypatch, tmp_path):
    vm = load_module("jarvis_voice_manager", "actions/voice_manager.py")

    fake_cfg_file = tmp_path / "api_keys.json"
    fake_cfg_file.write_text(json.dumps({"elevenlabs_api_key": "secret-key"}), encoding="utf-8")
    monkeypatch.setattr(vm, "CONFIG_PATH", fake_cfg_file)

    captured = {}

    def fake_create_tts_player(config):
        captured.update(config)
        return "STUB_PLAYER"

    monkeypatch.setattr(vm, "create_tts_player", fake_create_tts_player)

    # "Charon" is a Gemini prebuilt voice name — must not be forwarded to Kokoro.
    result = vm.build_tts_player({"provider": "local", "voice": "Charon", "speed": 1.2})

    assert result == "STUB_PLAYER"
    assert captured["tts_engine"] == "kokoro"
    assert "tts_voice" not in captured
    assert captured["tts_speed"] == 1.2
    assert captured["elevenlabs_api_key"] == "secret-key"


def test_build_tts_player_maps_elevenlabs_and_keeps_custom_voice_id(monkeypatch, tmp_path):
    vm = load_module("jarvis_voice_manager", "actions/voice_manager.py")

    fake_cfg_file = tmp_path / "api_keys.json"
    fake_cfg_file.write_text(json.dumps({"elevenlabs_api_key": "secret-key"}), encoding="utf-8")
    monkeypatch.setattr(vm, "CONFIG_PATH", fake_cfg_file)

    captured = {}
    monkeypatch.setattr(vm, "create_tts_player", lambda config: captured.update(config) or "STUB_PLAYER")

    result = vm.build_tts_player({"provider": "elevenlabs", "voice": "pNInz6obpgDQGcFmaJgB", "speed": 1.0})

    assert result == "STUB_PLAYER"
    assert captured["tts_engine"] == "elevenlabs"
    assert captured["tts_voice"] == "pNInz6obpgDQGcFmaJgB"
    assert captured["elevenlabs_api_key"] == "secret-key"


# ── main.py: _build_config() switches response_modalities per provider ──

def _new_live():
    main = load_module("jarvis_main_voice", "main.py")
    live = object.__new__(main.JarvisLive)
    return main, live


def test_build_config_uses_audio_modality_and_speech_config_for_gemini():
    main, live = _new_live()
    config = live._build_config(voice_cfg={"provider": "gemini", "voice": "Charon", "speed": 1.0})
    assert config.response_modalities == ["AUDIO"]
    assert config.speech_config is not None
    assert live._voice_provider == "gemini"


def test_build_config_uses_text_modality_and_no_speech_config_for_local():
    main, live = _new_live()
    config = live._build_config(voice_cfg={"provider": "local", "voice": "af_heart", "speed": 1.0})
    assert config.response_modalities == ["TEXT"]
    assert config.speech_config is None
    assert live._voice_provider == "local"


def test_build_config_uses_text_modality_for_elevenlabs():
    main, live = _new_live()
    config = live._build_config(voice_cfg={"provider": "elevenlabs", "voice": "Rachel", "speed": 1.0})
    assert config.response_modalities == ["TEXT"]
    assert live._voice_provider == "elevenlabs"


# ── main.py: interrupt() must stop an active non-Gemini TTS player too ──

def test_interrupt_stops_active_tts_player():
    main, live = _new_live()
    live._interrupted = False
    live.audio_in_queue = None
    live._turn_done_event = None
    live.ui = type("UIStub", (), {"muted": False, "set_state": lambda self, s: None, "write_log": lambda self, m: None})()
    live._speaking_lock = main.threading.Lock()
    live._is_speaking = True

    stopped = {"called": False}
    live._tts_player = type("PlayerStub", (), {"stop": lambda self: stopped.__setitem__("called", True)})()

    live.interrupt()

    assert stopped["called"] is True
    assert live._is_speaking is False


def test_interrupt_without_tts_player_still_works():
    # Gemini-only sessions never set _tts_player — interrupt() must not require it.
    main, live = _new_live()
    live._interrupted = False
    live.audio_in_queue = None
    live._turn_done_event = None
    live.ui = type("UIStub", (), {"muted": False, "set_state": lambda self, s: None, "write_log": lambda self, m: None})()
    live._speaking_lock = main.threading.Lock()
    live._is_speaking = True

    live.interrupt()  # no live._tts_player attribute at all

    assert live._interrupted is True
    assert live._is_speaking is False


# ── ui.py: raw config file keys map correctly into the overlay's shape ──

def test_voice_cfg_from_raw_maps_file_keys_to_overlay_keys():
    ui = load_module("jarvis_ui_voice", "ui.py")
    raw = {
        "gemini_api_key": "unrelated",
        "voice_provider": "elevenlabs",
        "voice_name": "Rachel",
        "voice_speed": 1.4,
        "elevenlabs_api_key": "secret-key",
    }
    cfg = ui._voice_cfg_from_raw(raw)
    assert cfg == {
        "provider": "elevenlabs",
        "voice": "Rachel",
        "speed": 1.4,
        "elevenlabs_api_key": "secret-key",
    }


def test_voice_cfg_from_raw_defaults_when_file_empty():
    ui = load_module("jarvis_ui_voice", "ui.py")
    assert ui._voice_cfg_from_raw({}) == {
        "provider": "gemini",
        "voice": "Charon",
        "speed": 1.0,
        "elevenlabs_api_key": "",
    }
