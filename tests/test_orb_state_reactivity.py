"""Real-time JARVIS orb state reactivity in the MAIN /ui interface.

Covers the priority-driven state arbitration layer added to the orb's
existing canvas IIFE in core/headless/ui_static/index.html: IDLE,
LISTENING, THINKING, TOOL, SPEAKING and ERROR are each driven by a real,
already-tested event (SpeechRecognition start/end, a genuine tool_start/
tool_end SSE frame from POST /ui/api/chat — see
tests/test_chat_tool_activity_stream.py for that transport's own coverage
— speechSynthesis start/end, an error the backend actually returned), never
a fake timer. This file guards the wiring, not the transport (already
covered elsewhere) or the canvas rendering (no JS runtime in this test
suite — the established pattern across this file's siblings is to assert
against the real served HTML/JS source, same as
test_ui_command_center.py's orb/TTS regression tests).

/3d is untouched by any of this — it is a completely separate interface
and none of these changes touch dashboard/ or dashboard_bridge.py.
"""
from fastapi.testclient import TestClient

from core.headless import config


def _html(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "test-ui-token-not-a-real-secret")
    from core.headless.app import create_app
    app = create_app(start_background_worker=False)
    client = TestClient(app, base_url="https://testserver")
    return client.get("/ui").text


# ── the priority engine itself ───────────────────────────────────────────

def test_priority_order_matches_the_required_ranking(monkeypatch):
    html = _html(monkeypatch)
    # OFFLINE -> ERROR -> SPEAKING -> TOOL -> SUCCESS -> THINKING ->
    # LISTENING -> (idle, implicit fallback when no reason is active).
    # OFFLINE leads even ERROR: nothing else means anything while the
    # backend itself is unreachable. SUCCESS sits right after TOOL since
    # it only ever fires the instant a real tool_end frame reports ok:true.
    assert 'const PRIORITY = ["offline", "error", "speaking", "tool", "success", "thinking", "listening"];' in html


def test_set_orb_reason_is_the_single_real_bridge_for_every_caller(monkeypatch):
    html = _html(monkeypatch)
    assert "window.setOrbReason = function setOrbReason(reason, active)" in html
    assert "reasons[reason] = !!active;" in html
    assert "applyPriority();" in html


def test_tool_and_error_are_real_orb_states_with_their_own_color_and_pace(monkeypatch):
    html = _html(monkeypatch)
    # Distinct from the pre-existing idle/listening/thinking/speaking so
    # each of the 6 required states reads differently at a glance.
    assert '"tool": "#ffb454"' in html or "tool: \"#ffb454\"" in html
    assert '"error": "#ff5c6c"' in html or "error: \"#ff5c6c\"" in html


# ── every one of the 6 required states is wired to a real event ─────────

def test_listening_is_driven_by_real_speech_recognition_events(monkeypatch):
    html = _html(monkeypatch)
    # Listening is now a function of the voice state machine, which only
    # ever changes state from a real SpeechRecognition event (onstart /
    # onresult / onend) — never a timer, never an optimistic guess.
    assert 'window.setOrbReason("listening", next === VoiceState.LISTENING);' in html
    assert "recognition.onresult = (event) => {" in html
    assert "if (voiceState !== VoiceState.LISTENING) setVoiceState(VoiceState.LISTENING);" in html


def test_thinking_is_driven_by_the_real_chat_turn_lifecycle(monkeypatch):
    html = _html(monkeypatch)
    assert "window.setOrbReason('thinking', true)" in html
    assert "window.setOrbReason('thinking', false)" in html


def test_tool_state_is_driven_by_the_real_tool_start_tool_end_sse_frames(monkeypatch):
    # This is the required connection: the orb must react to the same
    # tool_start/tool_end events the live tool-activity chips already
    # consume, not a second, duplicated detection of tool activity.
    html = _html(monkeypatch)
    assert "function addToolStartChip(name)" in html
    assert "function resolveToolChip(ok)" in html
    assert "if (window.setOrbReason) window.setOrbReason('tool', true);" in html
    assert "if (window.setOrbReason) window.setOrbReason('tool', false);" in html
    # Both handlers fire from the real SSE stream, not a separate poll/guess.
    assert "if (evt.type === 'tool_start') addToolStartChip(evt.name);" in html
    assert "else if (evt.type === 'tool_end') resolveToolChip(evt.ok);" in html


def test_speaking_is_driven_by_real_speech_synthesis_events(monkeypatch):
    html = _html(monkeypatch)
    # Both real output paths — neural audio and the browser voice — drive
    # the speaking state from their own playback events.
    assert 'utterance.onstart = () => { _speaking = true; window.setOrbReason("speaking", true); onSpeakingStarted(); };' in html
    assert 'currentUtterance = null; window.setOrbReason("speaking", false)' in html
    assert 'audio.onplay = () => { _speaking = true; window.setOrbReason("speaking", true); onSpeakingStarted(); };' in html


def test_error_is_driven_by_a_real_failure_not_a_fake_timer(monkeypatch):
    # appendError only ever runs when sendMessage's catch block, or a real
    # streamed {"type": "error"} SSE frame, actually fired — see
    # sendMessage()'s try/catch and streamSSE() above it. The clear-after
    # timeout below is bounded display of a real, already-happened event,
    # not simulated activity.
    html = _html(monkeypatch)
    assert "window.setOrbReason('error', true);" in html
    assert "window.setOrbReason('error', false)" in html


def test_idle_is_the_implicit_fallback_when_no_reason_is_active(monkeypatch):
    html = _html(monkeypatch)
    assert "function applyPriority() {" in html
    assert "setOrbState(winner || \"idle\");" in html


# ── multi-tool and regression coverage ───────────────────────────────────

def test_multiple_sequential_tool_calls_never_leave_the_orb_stuck(monkeypatch):
    # Tool calls run strictly sequentially server-side (see
    # core/headless/ui.py's provider loops and
    # test_chat_tool_activity_stream.py's multi-tool ordering test); the
    # orb's 'tool' reason must be cleared on every tool_end regardless of
    # whether a visible chip still exists for it, or a second/third tool in
    # the same turn would inherit a state the first tool never released.
    html = _html(monkeypatch)
    assert "if (window.setOrbReason) window.setOrbReason('tool', false);" in html


def test_a_tool_call_outranks_the_generic_thinking_state_while_it_runs(monkeypatch):
    # Real product requirement: once a tool actually starts, the orb must
    # show that specific activity, not keep showing generic "thinking."
    html = _html(monkeypatch)
    priority_line = 'const PRIORITY = ["offline", "error", "speaking", "tool", "success", "thinking", "listening"];'
    assert priority_line in html
    assert html.index('"tool"') < html.index('"thinking"', html.index(priority_line))


def test_an_error_outranks_a_reply_already_being_spoken(monkeypatch):
    # The one deliberate deviation from a naive FIFO: ERROR must win even
    # over SPEAKING, per the explicit product requirement.
    html = _html(monkeypatch)
    priority_line = 'const PRIORITY = ["offline", "error", "speaking", "tool", "success", "thinking", "listening"];'
    assert priority_line in html
    assert html.index('"error"', html.index(priority_line)) < html.index('"speaking"', html.index(priority_line))


def test_ordinary_chat_with_no_voice_or_tools_still_reaches_idle(monkeypatch):
    # Regression: a plain text turn (no mic, no tool call) must still clear
    # 'thinking' once the reply lands, so the orb settles back to idle
    # rather than being left showing "Thinking..." forever.
    html = _html(monkeypatch)
    assert "if (window.setOrbReason) window.setOrbReason('thinking', false);" in html


def test_the_regex_tested_speaking_fallback_timer_still_clears_via_the_reason_system(monkeypatch):
    # The pre-existing "speak once, then silent" fix (see
    # test_ui_command_center.py::test_orb_tts_does_not_have_the_speak_once_then_silent_bug)
    # must still hold: the visual fallback pulse used when voice replies are
    # off now clears through setOrbReason instead of forcing raw idle, so it
    # can't leave the 'speaking' reason flag stuck true after the display
    # already moved on.
    html = _html(monkeypatch)
    assert 'window._orbSpeakingFallback = setTimeout(() => { if (currentState === "speaking") window.setOrbReason("speaking", false); }, 2500);' in html


def test_3d_command_center_route_is_unaffected(monkeypatch):
    # This whole phase is /ui-only. /3d has its own separate auth (see
    # dashboard/server.py's _3d_auth, untouched here) — a bare request
    # without it is correctly refused, same as before this phase. The
    # point of this test is that the route still exists and still enforces
    # its own auth rather than, say, 404ing or silently inheriting the /ui
    # session — not to exercise its content.
    monkeypatch.setattr(config, "API_TOKEN", "test-ui-token-not-a-real-secret")
    from core.headless.app import create_app
    app = create_app(start_background_worker=False)
    client = TestClient(app, base_url="https://testserver")
    r = client.get("/3d")
    assert r.status_code == 401
