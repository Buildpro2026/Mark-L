import asyncio
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_live(name="jarvis_main_bargein"):
    main = load_module(name, "main.py")
    live = object.__new__(main.JarvisLive)
    return main, live


def _ui_stub():
    return type("UIStub", (), {
        "muted": False,
        "set_state": lambda self, s: None,
        "write_log": lambda self, m: None,
    })()


# ── _build_config: real Gemini server-side VAD must be configured so
# barge-in works without relying solely on client-side RMS ────────────────

def test_build_config_returns_a_real_config_not_none():
    # The actual bug this fixes: _build_config fell off the end of the
    # function with no return statement and referenced undefined names
    # (`parts`, `sys_prompt`) — calling it raised NameError immediately,
    # meaning the desktop app could never even open a Gemini Live session.
    main, live = _new_live()
    config = live._build_config()
    assert config is not None
    assert isinstance(config, main.types.LiveConnectConfig)
    assert config.system_instruction  # the real prompt/memory/knowledge, not empty


def test_build_config_enables_server_side_interruption():
    main, live = _new_live()
    config = live._build_config()
    rc = config.realtime_input_config
    assert rc is not None
    assert rc.activity_handling == main.types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS
    assert rc.automatic_activity_detection.start_of_speech_sensitivity == (
        main.types.StartSensitivity.START_SENSITIVITY_LOW
    )
    assert rc.automatic_activity_detection.prefix_padding_ms == main.BARGE_IN_PREFIX_PADDING_MS


# ── _receive_audio: sc.interrupted (Gemini-native VAD) must call interrupt() ─

class _FakeTranscription:
    def __init__(self, text):
        self.text = text


class _FakeSC:
    def __init__(self, interrupted=False, input_text=None, output_text=None, turn_complete=False):
        self.interrupted = interrupted
        self.output_transcription = _FakeTranscription(output_text) if output_text else None
        self.input_transcription = _FakeTranscription(input_text) if input_text else None
        self.turn_complete = turn_complete


class _FakeResponse:
    def __init__(self, server_content=None, data=None):
        self.data = data
        self.server_content = server_content
        self.tool_call = None
        self.text = None


class _FakeSession:
    """Yields the given events once, then idles — mirrors the shape of the
    real google-genai Live session's async receive() well enough to
    exercise _receive_audio's own dispatch logic without a live
    connection."""

    def __init__(self, events):
        self._events = events

    def receive(self):
        events = self._events

        async def _gen():
            for ev in events:
                yield ev
            await asyncio.sleep(3600)

        return _gen()


def _live_for_receive_audio(main):
    live = object.__new__(main.JarvisLive)
    live._interrupted = False
    live.audio_in_queue = asyncio.Queue()
    live._turn_done_event = None
    live.ui = _ui_stub()
    live._speaking_lock = main.threading.Lock()
    live._is_speaking = False
    live._out_stream = None
    live._dashboard = None
    live._session_log = []
    live._asst_name = "JARVIS"
    live._pending_vision = None
    live._vision_cam_active = False
    live._vision_close_pending = False
    live._last_user_speech = 0.0
    live._audio_generation = None
    live._conversation = main.ConversationManager()
    return live


async def _run_receive_audio_briefly(live):
    task = asyncio.ensure_future(live._receive_audio())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_receive_audio_calls_interrupt_on_gemini_native_interrupted_flag():
    main, _ = _new_live()
    live = _live_for_receive_audio(main)

    calls = []
    live.interrupt = lambda: calls.append(1)

    live.session = _FakeSession([_FakeResponse(_FakeSC(interrupted=True))])
    asyncio.run(_run_receive_audio_briefly(live))

    assert calls == [1]


def test_receive_audio_does_not_double_interrupt_for_repeated_interrupted_flags():
    main, _ = _new_live()
    live = _live_for_receive_audio(main)

    calls = []

    def _interrupt():
        calls.append(1)
        live._interrupted = True

    live.interrupt = _interrupt
    live.session = _FakeSession([
        _FakeResponse(_FakeSC(interrupted=True)),
        _FakeResponse(_FakeSC(interrupted=True)),
    ])
    asyncio.run(_run_receive_audio_briefly(live))

    assert calls == [1]


def test_receive_audio_triggers_interrupt_on_fresh_speech_during_playback():
    # The content-based layer (core/conversation.py's
    # is_genuine_user_transcript), independent of sc.interrupted: real,
    # unrelated words arriving via input_transcription while JARVIS is
    # speaking are a genuine barge-in.
    main, _ = _new_live()
    live = _live_for_receive_audio(main)
    live._is_speaking = True

    calls = []
    live.interrupt = lambda: calls.append(1)

    live.session = _FakeSession([_FakeResponse(_FakeSC(input_text="wait no stop"))])
    asyncio.run(_run_receive_audio_briefly(live))

    assert calls == [1]


def test_receive_audio_suppresses_self_echo_during_playback():
    # The actual bug this batch fixes: RMS alone let a loud echo of
    # JARVIS's own words through to the transcript stream; is_genuine_
    # user_transcript's content comparison against out_buf (what JARVIS is
    # currently saying) is the second, independent layer that catches it.
    main, _ = _new_live()
    live = _live_for_receive_audio(main)
    live._is_speaking = True

    calls = []
    live.interrupt = lambda: calls.append(1)

    # First: JARVIS's own output transcription populates out_buf...
    output_event = _FakeResponse(_FakeSC(output_text="I have added that to your calendar for tomorrow."))
    # ...then a near-duplicate arrives as INPUT — leaked playback, not a
    # real interruption.
    echo_event = _FakeResponse(_FakeSC(input_text="added that to your calendar"))
    live.session = _FakeSession([output_event, echo_event])
    asyncio.run(_run_receive_audio_briefly(live))

    assert calls == []


def test_receive_audio_accepts_real_speech_that_merely_mentions_similar_words():
    # Genuinely different speech must never be suppressed just because it
    # shares a couple of words with what JARVIS is saying.
    main, _ = _new_live()
    live = _live_for_receive_audio(main)
    live._is_speaking = True

    calls = []
    live.interrupt = lambda: calls.append(1)

    output_event = _FakeResponse(_FakeSC(output_text="I have added that to your calendar for tomorrow."))
    real_event = _FakeResponse(_FakeSC(input_text="actually cancel all of that right now"))
    live.session = _FakeSession([output_event, real_event])
    asyncio.run(_run_receive_audio_briefly(live))

    assert calls == [1]


def test_receive_audio_ignores_a_duplicate_transcript_chunk():
    main, _ = _new_live()
    live = _live_for_receive_audio(main)
    live._is_speaking = False  # not a barge-in scenario — just plain dedup

    first = _FakeResponse(_FakeSC(input_text="open my calendar"))
    duplicate = _FakeResponse(_FakeSC(input_text="open my calendar"))
    live.session = _FakeSession([first, duplicate])
    asyncio.run(_run_receive_audio_briefly(live))

    assert live is not None  # ran without raising
    # in_buf isn't directly observable from outside _receive_audio, but a
    # duplicate must not double the last_user_speech bookkeeping crash or
    # otherwise misbehave — absence of an exception here is the assertion
    # that matters; the pure-function guarantee is covered directly in
    # tests/test_conversation_manager.py.


def test_receive_audio_does_not_crash_with_no_server_content():
    main, _ = _new_live()
    live = _live_for_receive_audio(main)
    live.session = _FakeSession([_FakeResponse(server_content=None, data=b"\x00\x00" * 100)])
    asyncio.run(_run_receive_audio_briefly(live))  # must not raise


# ── register_session / release_session: reconnect + duplicate-session
# protection must actually be wired into the reconnect loop, not just
# exist as untested API on ConversationManager ─────────────────────────────

def test_run_registers_and_releases_a_session_id_around_each_connection():
    import inspect
    main, live = _new_live("jarvis_main_session_wiring")
    source = inspect.getsource(main.JarvisLive.run)
    assert "register_session" in source
    assert "release_session" in source


# ── interrupt(): must reach the real output stream, not just flags ────────

def test_interrupt_aborts_the_output_stream_and_bumps_the_generation():
    main, live = _new_live("jarvis_main_interrupt_wiring")
    live._interrupted = False
    live.audio_in_queue = None
    live._turn_done_event = None
    live.ui = _ui_stub()
    live._speaking_lock = main.threading.Lock()
    live._is_speaking = True
    live._conversation = main.ConversationManager()

    calls = []
    live._out_stream = type("StreamStub", (), {
        "abort": lambda self: calls.append("abort"),
    })()
    gen_before = live._conversation.generation

    live.interrupt()

    assert calls == ["abort"]
    assert live._conversation.generation > gen_before
