"""Two claims JARVIS used to make that were not true.

1. "Opened BuildPro in the command center" — said whether or not any
   Command Center window existed to open it in. apply_navigation() mutates
   server-side state unconditionally and broadcast_nav() returned None, so
   the tool had no way to tell a real navigation from a no-op and reported
   success either way.

2. The /ui voice silently failed over from free Gemini to metered Cartesia
   or ElevenLabs on any transient error. A rate limit should not start
   spending money.
"""
import asyncio

import pytest


# ══ NAVIGATION TELLS THE TRUTH ═══════════════════════════════════════════

class _FakeWS:
    def __init__(self, broken=False):
        self.broken = broken
        self.sent = []

    async def send_json(self, payload):
        if self.broken:
            raise RuntimeError("socket closed")
        self.sent.append(payload)


def _server():
    from dashboard.server import DashboardServer
    server = object.__new__(DashboardServer)
    server._3d_ws_clients = set()
    return server


def test_broadcast_reports_how_many_clients_actually_received_it():
    server = _server()
    live_a, live_b = _FakeWS(), _FakeWS()
    server._3d_ws_clients = {live_a, live_b}
    delivered = asyncio.run(server.broadcast_nav({"type": "navigate"}))
    assert delivered == 2
    assert live_a.sent and live_b.sent


def test_broadcast_reports_zero_when_no_command_center_is_open():
    server = _server()
    assert asyncio.run(server.broadcast_nav({"type": "navigate"})) == 0


def test_a_dead_socket_is_not_counted_as_delivered():
    server = _server()
    live, dead = _FakeWS(), _FakeWS(broken=True)
    server._3d_ws_clients = {live, dead}
    delivered = asyncio.run(server.broadcast_nav({"type": "navigate"}))
    assert delivered == 1
    assert dead not in server._3d_ws_clients, "a dead socket was kept"


def test_one_broken_socket_does_not_stop_delivery_to_the_others():
    server = _server()
    clients = [_FakeWS(broken=True)] + [_FakeWS() for _ in range(3)]
    server._3d_ws_clients = set(clients)
    assert asyncio.run(server.broadcast_nav({"type": "navigate"})) == 3


def test_the_viewer_count_reflects_connected_windows():
    server = _server()
    assert server.command_center_viewers == 0
    server._3d_ws_clients = {_FakeWS()}
    assert server.command_center_viewers == 1


def test_navigation_state_still_changes_and_is_readable():
    # The state mutation itself must be preserved — the fix is about what
    # JARVIS SAYS, not about refusing to navigate.
    from dashboard.server import DashboardServer
    server = object.__new__(DashboardServer)
    server._nucleus_id = "jarvis"
    server._nucleus_back_stack = []

    opened = server.apply_navigation("open", "buildpro")
    assert server._nucleus_id == "buildpro"
    assert opened["nucleus_id"] == "buildpro"

    back = server.apply_navigation("back", "")
    assert server._nucleus_id == "jarvis"
    assert back["action"] == "back"

    server.apply_navigation("open", "buildpro")
    home = server.apply_navigation("home", "")
    assert server._nucleus_id == "jarvis"
    assert home["action"] == "home"
    assert server._nucleus_back_stack == []


def test_voice_and_click_navigation_share_one_mechanism():
    # Both routes call apply_navigation; there is no second navigation
    # implementation for either to drift away from.
    import inspect
    from dashboard import server as server_module
    source = inspect.getsource(server_module)
    assert source.count("def apply_navigation") == 1


# ══ GEMINI ONLY ══════════════════════════════════════════════════════════

def test_the_ui_voice_never_falls_back_to_a_paid_provider(monkeypatch):
    from core.headless import ui
    from actions import gemini_tts, cartesia_tts, elevenlabs_tts

    monkeypatch.setattr(gemini_tts, "is_configured", lambda: True)
    monkeypatch.setattr(gemini_tts, "synthesize_speech",
                        lambda text, **k: {"ok": False, "detail": "rate limited"})

    def _paid(*a, **k):
        raise AssertionError("fell back to a paid voice provider")
    monkeypatch.setattr(cartesia_tts, "synthesize_speech", _paid)
    monkeypatch.setattr(elevenlabs_tts, "synthesize_speech", _paid)
    monkeypatch.setattr(cartesia_tts, "is_configured", lambda: True)
    monkeypatch.setattr(elevenlabs_tts, "is_configured", lambda: True)

    result = ui.synthesize_reply_audio("hello")
    assert result["ok"] is False
    assert result["provider"] == "gemini"
    assert result["paid_fallback_suppressed"] is True


def test_a_working_gemini_still_returns_audio(monkeypatch):
    from core.headless import ui
    from actions import gemini_tts

    monkeypatch.setattr(gemini_tts, "is_configured", lambda: True)
    monkeypatch.setattr(gemini_tts, "synthesize_speech",
                        lambda text, **k: {"ok": True, "audio_base64": "QUJD",
                                           "mime_type": "audio/wav"})
    result = ui.synthesize_reply_audio("hello")
    assert result == {"configured": True, "ok": True, "audio_base64": "QUJD",
                      "mime_type": "audio/wav", "provider": "gemini", "voice": "Charon"}


def test_the_selected_voice_is_actually_used_not_just_the_default(monkeypatch):
    # The voice dropdown's whole point: passing a different voice_id must
    # reach gemini_tts.synthesize_speech, not be silently ignored in favor
    # of DEFAULT_VOICE.
    from core.headless import ui
    from actions import gemini_tts

    seen = {}
    monkeypatch.setattr(gemini_tts, "is_configured", lambda: True)
    def _fake_synth(text, voice_id=None):
        seen["voice_id"] = voice_id
        return {"ok": True, "audio_base64": "QUJD", "mime_type": "audio/wav", "voice": voice_id}
    monkeypatch.setattr(gemini_tts, "synthesize_speech", _fake_synth)

    result = ui.synthesize_reply_audio("hello", voice_id="Puck")
    assert seen["voice_id"] == "Puck"
    assert result["voice"] == "Puck"


def test_the_tts_endpoint_uses_lees_stored_voice_selection(monkeypatch):
    # This is the actual bug report: the dropdown persisted a choice that
    # /ui/api/tts/speak never read, so every reply spoke in DEFAULT_VOICE
    # regardless of what Settings said was selected.
    from core.headless import ui
    from actions import gemini_tts, voice_manager

    monkeypatch.setattr(voice_manager, "get_voice_provider_config",
                        lambda: {"provider": "gemini", "voice": "Kore", "speed": 1.0})
    monkeypatch.setattr(gemini_tts, "is_configured", lambda: True)
    seen = {}
    def _fake_synth(text, voice_id=None):
        seen["voice_id"] = voice_id
        return {"ok": True, "audio_base64": "QUJD", "mime_type": "audio/wav", "voice": voice_id}
    monkeypatch.setattr(gemini_tts, "synthesize_speech", _fake_synth)

    result = ui.ui_tts_speak(ui.SpeakRequest(text="hello"))
    assert seen["voice_id"] == "Kore"
    assert result["voice"] == "Kore"


def test_the_tts_endpoint_ignores_a_non_gemini_stored_provider(monkeypatch):
    # If the stored provider isn't gemini (local/elevenlabs), there is no
    # gemini voice_id to honor — synthesize_reply_audio must fall through
    # to its own DEFAULT_VOICE rather than passing a foreign voice name
    # (e.g. an ElevenLabs voice id) straight into the Gemini API call.
    from core.headless import ui
    from actions import gemini_tts, voice_manager

    monkeypatch.setattr(voice_manager, "get_voice_provider_config",
                        lambda: {"provider": "elevenlabs", "voice": "Rachel", "speed": 1.0})
    monkeypatch.setattr(gemini_tts, "is_configured", lambda: True)
    seen = {}
    def _fake_synth(text, voice_id=None):
        seen["voice_id"] = voice_id
        return {"ok": True, "audio_base64": "QUJD", "mime_type": "audio/wav", "voice": voice_id or gemini_tts.DEFAULT_VOICE}
    monkeypatch.setattr(gemini_tts, "synthesize_speech", _fake_synth)

    result = ui.ui_tts_speak(ui.SpeakRequest(text="hello"))
    assert seen["voice_id"] is None
    assert result["voice"] == gemini_tts.DEFAULT_VOICE


def test_no_gemini_key_reports_unconfigured_rather_than_paying(monkeypatch):
    from core.headless import ui
    from actions import gemini_tts, cartesia_tts

    monkeypatch.setattr(gemini_tts, "is_configured", lambda: False)
    monkeypatch.setattr(cartesia_tts, "is_configured", lambda: True)
    monkeypatch.setattr(cartesia_tts, "synthesize_speech",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("paid provider used")))

    result = ui.synthesize_reply_audio("hello")
    assert result["configured"] is False
    assert "GEMINI_API_KEY" in result["detail"]


def test_the_configured_voice_is_charon():
    from actions import gemini_tts
    assert gemini_tts.DEFAULT_VOICE == "Charon"


def test_the_ui_voice_path_no_longer_imports_paid_providers():
    # Checked as code, not as prose: the comment in that function explains
    # WHY Cartesia and ElevenLabs were removed, so a bare substring search
    # would match the explanation and prove nothing.
    import ast, inspect, textwrap
    from core.headless import ui

    tree = ast.parse(textwrap.dedent(inspect.getsource(ui.synthesize_reply_audio)))
    imported = {alias.name for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert "cartesia_tts" not in imported
    assert "elevenlabs_tts" not in imported
    assert imported == {"gemini_tts"}
