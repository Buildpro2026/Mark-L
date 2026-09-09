import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ui_stub():
    return type("UIStub", (), {
        "muted": False,
        "set_state": lambda self, s: None,
        "write_log": lambda self, m: None,
    })()


def _live_for_interrupt(name):
    # A real ConversationManager, not a stub — interrupt() calls
    # barge_in() on it as its very first step (see main.py), so any test
    # exercising interrupt() needs the real thing here, not a mock of it.
    main = load_module(name, "main.py")
    live = object.__new__(main.JarvisLive)
    live._interrupted = False
    live.audio_in_queue = None
    live._turn_done_event = None
    live.ui = _ui_stub()
    live._speaking_lock = main.threading.Lock()
    live._is_speaking = True
    live._conversation = main.ConversationManager()
    return main, live


def test_interrupt_sets_listening_and_clears_audio_state():
    main, live = _live_for_interrupt("jarvis_main")

    live.interrupt()

    assert live._interrupted is True
    assert live._is_speaking is False
    # The manager itself must reflect the interruption, not just the bare
    # flags: barge_in() claims a fresh USER_TURN, then set_speaking(False)
    # (called at the end of interrupt()) moves it on to LISTENING.
    assert live._conversation.state in (main._cv.USER_TURN, main._cv.LISTENING)


def test_interrupt_aborts_the_live_output_stream_immediately():
    # This is the actual audio/TTS output layer fix: interrupt() must reach
    # the real sounddevice output stream, not just flip Python-side flags —
    # and it must call abort() (discards in-flight audio), never stop()
    # (which waits for buffered audio to finish draining first).
    main, live = _live_for_interrupt("jarvis_main_abort")

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
    main, live = _live_for_interrupt("jarvis_main_no_stream")
    # no live._out_stream attribute at all

    live.interrupt()

    assert live._interrupted is True
    assert live._is_speaking is False


def test_interrupt_bumps_the_conversation_generation():
    # barge_in() must invalidate whatever generation was speaking — this is
    # what makes the old response's audio/tool-results provably stale
    # rather than merely "probably done by now".
    main, live = _live_for_interrupt("jarvis_main_generation")
    gen_before = live._conversation.generation

    live.interrupt()

    assert live._conversation.generation > gen_before
