import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_interrupt_sets_listening_and_clears_audio_state():
    main = load_module("jarvis_main", "main.py")
    live = object.__new__(main.JarvisLive)
    live._interrupted = False
    live.audio_in_queue = None
    live._turn_done_event = None
    live.ui = type("UIStub", (), {"muted": False, "set_state": lambda self, s: None, "write_log": lambda self, m: None})()
    live._speaking_lock = main.threading.Lock()
    live._is_speaking = True

    live.interrupt()

    assert live._interrupted is True
    assert live._is_speaking is False


def test_interrupt_aborts_the_live_output_stream_immediately():
    # This is the actual audio/TTS output layer fix: interrupt() must reach
    # the real sounddevice output stream, not just flip Python-side flags —
    # and it must call abort() (discards in-flight audio), never stop()
    # (which waits for buffered audio to finish draining first).
    main = load_module("jarvis_main_abort", "main.py")
    live = object.__new__(main.JarvisLive)
    live._interrupted = False
    live.audio_in_queue = None
    live._turn_done_event = None
    live.ui = type("UIStub", (), {"muted": False, "set_state": lambda self, s: None, "write_log": lambda self, m: None})()
    live._speaking_lock = main.threading.Lock()
    live._is_speaking = True

    calls = []
    live._out_stream = type("StreamStub", (), {
        "abort": lambda self: calls.append("abort"),
        "stop": lambda self: calls.append("stop"),
    })()

    live.interrupt()

    assert calls == ["abort"]   # abort only — never the draining stop()


def test_interrupt_still_works_without_an_out_stream():
    # Before a session has connected (or between sessions) there is no
    # output stream yet — interrupt() must not crash in that case.
    main = load_module("jarvis_main_no_stream", "main.py")
    live = object.__new__(main.JarvisLive)
    live._interrupted = False
    live.audio_in_queue = None
    live._turn_done_event = None
    live.ui = type("UIStub", (), {"muted": False, "set_state": lambda self, s: None, "write_log": lambda self, m: None})()
    live._speaking_lock = main.threading.Lock()
    live._is_speaking = True

    live.interrupt()   # no live._out_stream attribute at all

    assert live._interrupted is True
    assert live._is_speaking is False
