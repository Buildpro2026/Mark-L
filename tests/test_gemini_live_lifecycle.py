"""Gemini Live session lifecycle: connect/reconnect status accuracy, and
tool_call event dispatch through _receive_audio() -> _execute_tool() ->
send_tool_response(). Cancellation/interrupt and barge-in are already
covered by test_barge_in.py / test_barge_in_live_wiring.py /
test_voice_provider_wiring.py; connection-failure classification and
bounded backoff are covered by test_connection_error_reporting.py. This
file fills the two gaps those don't touch: post-session HUD state accuracy,
and the tool-call round trip.

No live external calls are made anywhere in this file — every Gemini Live
session object is a local fake.
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


def _new_live(name="jarvis_main_live_lifecycle"):
    main = load_module(name, "main.py")
    live = object.__new__(main.JarvisLive)
    return main, live


def _ui_stub(log: list | None = None):
    log = log if log is not None else []
    return type("UIStub", (), {
        "muted": False,
        "set_state": lambda self, s: log.append(s),
        "write_log": lambda self, m: None,
    })()


# ── _post_session_state: HUD accuracy after a run()-loop iteration ──────

def test_post_session_state_is_reconnecting_after_a_real_error():
    main, _ = _new_live()
    assert main._post_session_state(had_error=True) == "RECONNECTING"


def test_post_session_state_is_sleeping_after_a_clean_disconnect():
    main, _ = _new_live()
    assert main._post_session_state(had_error=False) == "SLEEPING"


# ── tool_call event dispatch: response.tool_call -> _execute_tool -> send_tool_response ──

class _FakeFunctionCall:
    def __init__(self, id_, name, args=None):
        self.id = id_
        self.name = name
        self.args = args or {}


class _FakeToolCall:
    def __init__(self, function_calls):
        self.function_calls = function_calls


class _FakeSC:
    def __init__(self):
        self.interrupted = False
        self.output_transcription = None
        self.input_transcription = None
        self.turn_complete = False


class _ToolCallResponse:
    def __init__(self, function_calls):
        self.data = None
        self.server_content = None
        self.tool_call = _FakeToolCall(function_calls)
        self.text = None


class _FakeSession:
    """Yields the given events once, then idles; records every
    send_tool_response() call so the test can assert on it."""

    def __init__(self, events):
        self._events = events
        self.tool_responses: list = []

    def receive(self):
        events = self._events

        async def _gen():
            for ev in events:
                yield ev
            await asyncio.sleep(3600)

        return _gen()

    async def send_tool_response(self, function_responses):
        self.tool_responses.append(function_responses)


def _live_for_receive_audio(main, ui_log=None):
    live = object.__new__(main.JarvisLive)
    live._interrupted = False
    live.audio_in_queue = asyncio.Queue()
    live._turn_done_event = None
    live.ui = _ui_stub(ui_log)
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


def test_tool_call_event_dispatches_through_execute_tool_and_sends_a_response():
    # Uses an unrecognized tool name deliberately — this exercises the full
    # real plumbing (response.tool_call -> _execute_tool -> FunctionResponse
    # -> send_tool_response) with zero side effects, since unknown-tool is
    # a safe, harmless branch in _execute_tool's dispatcher.
    main, _ = _new_live()
    live = _live_for_receive_audio(main)

    fc = _FakeFunctionCall(id_="call-1", name="not_a_real_tool", args={})
    session = _FakeSession([_ToolCallResponse([fc])])
    live.session = session

    asyncio.run(_run_receive_audio_briefly(live))

    assert len(session.tool_responses) == 1
    responses = session.tool_responses[0]
    assert len(responses) == 1
    assert responses[0].name == "not_a_real_tool"
    assert "Unknown tool" in responses[0].response["result"]


def test_tool_call_sets_thinking_state_before_dispatch():
    main, _ = _new_live()
    ui_log: list = []
    live = _live_for_receive_audio(main, ui_log)

    fc = _FakeFunctionCall(id_="call-2", name="not_a_real_tool", args={})
    live.session = _FakeSession([_ToolCallResponse([fc])])

    asyncio.run(_run_receive_audio_briefly(live))

    assert "THINKING" in ui_log


def test_multiple_function_calls_in_one_tool_call_all_get_responses():
    main, _ = _new_live()
    live = _live_for_receive_audio(main)

    fcs = [
        _FakeFunctionCall(id_="call-a", name="not_a_real_tool_a", args={}),
        _FakeFunctionCall(id_="call-b", name="not_a_real_tool_b", args={}),
    ]
    session = _FakeSession([_ToolCallResponse(fcs)])
    live.session = session

    asyncio.run(_run_receive_audio_briefly(live))

    assert len(session.tool_responses) == 1
    names = {r.name for r in session.tool_responses[0]}
    assert names == {"not_a_real_tool_a", "not_a_real_tool_b"}
