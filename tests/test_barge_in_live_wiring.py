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


# ── _should_forward_mic_audio: mic must stay open while JARVIS speaks ──

def test_mic_forwards_audio_while_jarvis_is_speaking():
    # This is the core barge-in fix: the old code silently dropped every mic
    # frame whenever _is_speaking was True, so JARVIS could never hear an
    # interruption. The new gate must not depend on _is_speaking at all.
    main, live = _new_live()
    live.ui = _ui_stub()
    live.ui.muted = False
    live._phone_active = False
    live._is_speaking = True
    assert live._should_forward_mic_audio() is True


def test_mic_still_respects_mute_and_phone_active():
    main, live = _new_live()
    live.ui = _ui_stub()
    live._phone_active = False
    live._is_speaking = False

    live.ui.muted = True
    assert live._should_forward_mic_audio() is False

    live.ui.muted = False
    live._phone_active = True
    assert live._should_forward_mic_audio() is False


# ── _build_config: server-side VAD must be configured so barge-in works ──

def test_build_config_enables_server_side_interruption_for_gemini():
    main, live = _new_live()
    config = live._build_config(voice_cfg={"provider": "gemini", "voice": "Charon", "speed": 1.0})
    rc = config.realtime_input_config
    assert rc is not None
    assert rc.activity_handling == main.types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS
    assert rc.automatic_activity_detection.start_of_speech_sensitivity == (
        main.types.StartSensitivity.START_SENSITIVITY_LOW
    )
    assert rc.automatic_activity_detection.prefix_padding_ms == main.BARGE_IN_PREFIX_PADDING_MS


def test_build_config_enables_server_side_interruption_for_local_too():
    # VAD governs the mic INPUT stream, independent of output modality —
    # local/ElevenLabs playback needs it just as much as Gemini audio does.
    main, live = _new_live()
    config = live._build_config(voice_cfg={"provider": "local", "voice": "af_heart", "speed": 1.0})
    assert config.realtime_input_config is not None
    assert config.realtime_input_config.automatic_activity_detection.prefix_padding_ms == (
        main.BARGE_IN_PREFIX_PADDING_MS
    )


# ── _looks_like_self_echo: cheap acoustic-echo false-positive guard ──

def test_self_echo_detects_heard_text_within_current_speech():
    main, live = _new_live()
    live._current_speech_text = "The weather today is sunny with a light breeze."
    assert live._looks_like_self_echo("the weather today") is True


def test_self_echo_allows_unrelated_speech_through():
    main, live = _new_live()
    live._current_speech_text = "The weather today is sunny with a light breeze."
    assert live._looks_like_self_echo("stop stop wait") is False


def test_self_echo_false_when_nothing_is_playing():
    main, live = _new_live()
    live._current_speech_text = None
    assert live._looks_like_self_echo("anything") is False


# ── _receive_audio: sc.interrupted (Gemini-native) must call interrupt() ──

class _FakeSC:
    def __init__(self, interrupted=False, input_text=None):
        self.interrupted = interrupted
        self.output_transcription = None
        self.input_transcription = (
            type("Tx", (), {"text": input_text})() if input_text else None
        )
        self.turn_complete = False


class _FakeResponse:
    def __init__(self, server_content):
        self.data = None
        self.server_content = server_content
        self.tool_call = None
        self.text = None


class _FakeSession:
    """Yields the given events once, then idles — mirrors the shape of the
    real google-genai Live session's async receive() well enough to exercise
    _receive_audio's own dispatch logic without a live connection."""

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
    live._voice_provider = "gemini"
    live._tts_player = None
    live._out_stream = None
    live._current_speech_text = None
    live._dashboard = None
    live._session_log = []
    live._asst_name = "JARVIS"
    live._pending_vision = None
    live._vision_cam_active = False
    live._vision_close_pending = False
    live._last_user_speech = 0.0
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


def test_receive_audio_triggers_interrupt_on_fresh_speech_during_local_playback():
    main, _ = _new_live()
    live = _live_for_receive_audio(main)
    live._voice_provider = "local"
    live._is_speaking = True
    live._current_speech_text = "I have added that to your calendar for tomorrow."

    calls = []
    live.interrupt = lambda: calls.append(1)

    live.session = _FakeSession([_FakeResponse(_FakeSC(input_text="wait no stop"))])
    asyncio.run(_run_receive_audio_briefly(live))

    assert calls == [1]


def test_receive_audio_suppresses_self_echo_during_local_playback():
    main, _ = _new_live()
    live = _live_for_receive_audio(main)
    live._voice_provider = "local"
    live._is_speaking = True
    live._current_speech_text = "I have added that to your calendar for tomorrow."

    calls = []
    live.interrupt = lambda: calls.append(1)

    # Near-duplicate of what JARVIS is currently saying — looks like acoustic
    # echo of his own voice, not a real interruption.
    live.session = _FakeSession([_FakeResponse(_FakeSC(input_text="added that to your calendar"))])
    asyncio.run(_run_receive_audio_briefly(live))

    assert calls == []


def test_receive_audio_does_not_self_trigger_on_gemini_audio_path():
    # Gemini-audio sessions rely on sc.interrupted (native VAD), not the
    # input-transcription heuristic — this must stay off for provider=gemini
    # so it can't double-trigger or misfire independent of the real signal.
    main, _ = _new_live()
    live = _live_for_receive_audio(main)
    live._voice_provider = "gemini"
    live._is_speaking = True

    calls = []
    live.interrupt = lambda: calls.append(1)

    live.session = _FakeSession([_FakeResponse(_FakeSC(input_text="wait no stop"))])
    asyncio.run(_run_receive_audio_briefly(live))

    assert calls == []
