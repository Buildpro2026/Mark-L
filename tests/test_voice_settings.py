import importlib.util
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_voice_config_persists_provider_and_speed():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    ui = load_module("jarvis_ui", "ui.py")
    cfg = {"voice_provider": "gemini", "voice_name": "Charon", "voice_speed": 1.0}
    overlay = ui.VoiceSettingsOverlay(cfg)
    overlay._provider.setCurrentText("gemini")
    overlay._voice.setText("Charon")
    overlay._speed.setValue(140)
    overlay._save()
    assert overlay._provider.currentText() == "gemini"
    assert overlay._voice.text() == "Charon"
    assert overlay._speed.value() == 140
