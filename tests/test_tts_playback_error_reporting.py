"""_speak_with_tts_player must surface playback failures to the UI log, not
just print to the console. Before this fix, an engine exception (e.g. the
real 'No module named miniaudio' failure this session found and fixed)
vanished silently from the user's perspective — JARVIS asked to speak,
nothing came out, and no error appeared anywhere but the console.
"""
import asyncio
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_live():
    main = load_module("jarvis_main_tts_err", "main.py")
    live = object.__new__(main.JarvisLive)
    return main, live


def _ui_stub(log: list):
    return type("UIStub", (), {
        "muted": False,
        "set_state": lambda self, s: None,
        "write_log": lambda self, m: log.append(m),
    })()


class _FailingPlayer:
    def speak(self, text, on_start=None, on_done=None):
        if on_start:
            on_start()
        raise ModuleNotFoundError("No module named 'miniaudio'")


def test_tts_playback_error_is_written_to_ui_log():
    main, live = _new_live()
    log: list[str] = []
    live.ui = _ui_stub(log)
    live._speaking_lock = main.threading.Lock()
    live._is_speaking = False
    live._interrupted = False
    live._tts_player = _FailingPlayer()
    live._current_speech_text = None

    asyncio.run(live._speak_with_tts_player("hello there"))

    assert any("ERR:" in m and "playback" in m.lower() for m in log)
    assert live._is_speaking is False
    assert live._current_speech_text is None


class _WorkingPlayer:
    def speak(self, text, on_start=None, on_done=None):
        if on_start:
            on_start()
        if on_done:
            on_done()


def test_successful_playback_does_not_log_an_error():
    main, live = _new_live()
    log: list[str] = []
    live.ui = _ui_stub(log)
    live._speaking_lock = main.threading.Lock()
    live._is_speaking = False
    live._interrupted = False
    live._tts_player = _WorkingPlayer()
    live._current_speech_text = None

    asyncio.run(live._speak_with_tts_player("hello there"))

    assert log == []
