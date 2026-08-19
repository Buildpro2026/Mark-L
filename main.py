import platform as _platform
import subprocess as _subprocess

# ── Force UTF-8 stdout/stderr on Windows ──────────────────────────────────────
# Reproduced live during the Phase 2 migration audit: actions/web_search.py's
# own debug print (an emoji in "[WebSearch] 🔍 mode=...") raised
# UnicodeEncodeError under Windows' default console code page (cp1252), which
# can't encode most emoji — and this codebase's print-based debug logging
# uses emoji throughout main.py and actions/*, not just that one line. A
# crash in a print() statement takes down whatever tool call triggered it
# just as surely as a crash in real logic. Reconfiguring here, once, at
# startup, protects every print/log call in the process instead of stripping
# emoji from each one individually. No-op (and harmless) on non-Windows,
# where the default encoding is already UTF-8.
if _platform.system() == "Windows":
    import sys as _sys
    try:
        _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        _sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ── Nuclear: force CREATE_NO_WINDOW on EVERY subprocess call on Windows ───────
# This patches Popen itself, so no per-file flag is needed anywhere.
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)   # drop any stale/shared STARTUPINFO
            super().__init__(args, **kw)

    _subprocess.Popen = _Popen
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import re
import threading
import time
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

import sounddevice as sd
from google import genai
from google.genai import types
from ui import JarvisUI
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
    save_session_summary, pop_last_session,
)

from actions.file_processor import file_processor
from actions.flight_finder     import flight_finder
from actions.open_app          import open_app
from actions.weather_report    import weather_action
from actions.send_message      import send_message
from actions.reminder          import reminder
from actions.computer_settings import computer_settings
from actions.screen_processor  import _capture_camera, _capture_screen
from actions.youtube_video     import youtube_video
from actions.desktop           import desktop_control
from actions.browser_control   import browser_control
from actions.file_controller   import file_controller
from actions.code_helper       import code_helper
from actions.dev_agent         import dev_agent
from actions.web_search        import web_search as web_search_action
from actions.computer_control  import computer_control
from actions.game_updater      import game_updater
from actions.system_monitor    import SystemMonitor, get_system_status
from actions.proactive         import ProactiveEngine, get_recent_triggers as get_proactive_history
from actions.background_monitor import (
    add_monitor, remove_monitor, list_monitors, check_all as monitor_check_all,
)
from actions.web_search        import _news as _fetch_news_sync
from actions.voice_manager     import get_voice_provider_config, build_tts_player
from actions.voice_navigation  import parse_navigation_command
from actions.strategic_objective import (
    format_objective_for_prompt, get_objective_status, log_revenue,
)
from actions.agent_orchestrator import orchestrator as agent_orchestrator
from actions import agent_orchestrator as agent_scheduler_lock   # module itself, for the single-instance lock functions (distinct from the `orchestrator` singleton imported above)
from actions import twilio_integration as twilio
from actions import gmail_integration
from actions import calendar_integration
from actions import airtable_integration
from actions import hubspot_integration
from actions import buffer_integration
from actions import buildpro_data
from actions import buildpro_matching
from actions import google_auth
from actions import business_intelligence as biz_intel
from actions import opportunity_engine as opp_engine
from actions import decision_engine
from memory.config_manager     import (
    get_brief_enabled, get_proactive_enabled, save_proactive_enabled,
    get_proactive_quiet_hours,
)
from core.startup import (
    print_startup_banner, check_single_instance, graceful_release_all_locks,
)
from core.headless.context import ToolContext
from core.headless.tool_executor import ToolExecutor, UnknownToolError


def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"
LIVE_MODEL          = "models/gemini-2.5-flash-native-audio-preview-12-2025"
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024

# Barge-in tuning: how much sustained speech Gemini's server-side VAD requires
# before it commits to "the user started talking" and interrupts playback.
# Lower = faster real interruptions but more false positives from acoustic
# echo of JARVIS's own voice bleeding into the mic; higher = the opposite.
# 200ms was too sensitive in practice — the always-on mic (see
# _should_forward_mic_audio) picked up acoustic echo/ambient noise while
# JARVIS was speaking often enough to self-trigger sc.interrupted on
# nearly every response, silently killing both the audio and the response
# text before the user could ever hear or see it (confirmed by reproducing
# this exact sequence against the live _receive_audio()/interrupt() code).
BARGE_IN_PREFIX_PADDING_MS = 700

def _get_api_key() -> str:
    """Raises a RuntimeError containing "API key not valid" (rather than a
    raw KeyError/FileNotFoundError) on a missing config file or missing/empty
    key, so run()'s existing error handling — which already recognizes that
    phrase and prompts reconfiguration instead of retrying forever — catches
    this case too. Before this fix, a missing gemini_api_key silently caused
    an infinite 3-second reconnect loop with no guidance to the user.

    J3 Part 2: GEMINI_API_KEY env var takes precedence over
    config/api_keys.json (core/headless/config.py) — a cloud deploy sets
    the env var and never needs the JSON file at all; local desktop use,
    which has never set that env var, keeps reading the file exactly as
    before."""
    from core.headless import config as _hc
    if _hc.GEMINI_API_KEY:
        return _hc.GEMINI_API_KEY
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            key = json.load(f).get("gemini_api_key", "")
    except Exception:
        key = ""
    if not key:
        raise RuntimeError("API key not valid: gemini_api_key is missing from config/api_keys.json")
    return key


def _resolve_input_device() -> tuple[int, str]:
    """Resolve a usable microphone input device index.

    Windows' reported default input device has been observed stuck at -1
    (invalid) here even while real microphones are enumerable via PortAudio
    (see 2026-08-09 recovery diagnosis) — sd.InputStream(...) with no
    device= inherits that -1 and raises PortAudioError immediately, which
    tears down the whole Gemini Live session since _listen_audio runs
    inside run()'s TaskGroup.

    Device metadata alone isn't trustworthy here: several WDM-KS entries
    report input channels but fail to actually open (PortAudioError -9996),
    sd.check_input_settings() reports "Invalid sample rate" for devices that
    open fine in practice, and at least one ("PC Speaker", a WDM-KS pin left
    over from a phantom/disconnected device per Windows Device Manager)
    opens without error yet never delivers a single audio callback. So each
    candidate is verified by actually opening a throwaway InputStream at the
    app's real settings and confirming a real callback arrives within a
    short timeout.

    NOTE: this deliberately does NOT reject a device for producing all-zero
    samples during that window — the real Microphone Array on this machine
    (Intel Smart Sound Technology) runs through driver-level noise
    suppression that can legitimately output true digital silence during a
    quiet moment, which an earlier version of this probe misread as "fake."
    Whether the callback fires at all is what actually distinguishes a real
    device from a dead pin like "PC Speaker", which never fires one.

    NOTE: this machine also has VB-Audio Voicemeeter installed, which
    registers ~70 virtual routing devices (Voicemeeter Out A1-A5/B1-B3,
    In 1-5, etc. — duplicated across several host APIs) that also report
    input channels. Trying all of them costs real per-device open/close
    overhead and previously stretched startup to minutes. None of them are
    an actual microphone, so they're only tried as a last resort — the OS
    default and anything actually named "microphone"/"mic" go first and
    resolve almost immediately in the normal case.
    """
    devices = sd.query_devices()
    default_idx = sd.default.device[0]

    _NON_MIC_HINTS = ("stereo mix", "what u hear", "loopback", "pc speaker")

    def _is_priority(i, d):
        name = d["name"].lower()
        return i == default_idx or "microphone" in name or "mic" in name

    def _rank(item):
        i, d = item
        name = d["name"].lower()
        is_default = (i == default_idx)
        is_named_mic = "microphone" in name or "mic" in name
        is_non_mic_hint = any(hint in name for hint in _NON_MIC_HINTS)
        return (not is_default, not is_named_mic, is_non_mic_hint)

    input_capable = [
        (i, d) for i, d in enumerate(devices)
        if d.get("max_input_channels", 0) > 0
    ]
    if not input_capable:
        raise RuntimeError("No usable microphone input device found (0 input-capable devices enumerated).")

    priority = sorted((c for c in input_capable if _is_priority(*c)), key=_rank)
    fallback = sorted((c for c in input_capable if not _is_priority(*c)), key=_rank)

    def _probe(candidates: list) -> tuple[int, str] | None:
        for i, d in candidates:
            got_audio = threading.Event()

            def _probe_callback(indata, frames, time_info, status, _evt=got_audio):
                _evt.set()

            try:
                # Must probe in callback (non-blocking) mode, matching how
                # _listen_audio actually uses the stream — some WDM-KS
                # devices reject the blocking API entirely ("Blocking API
                # not supported yet") while opening fine in callback mode.
                with sd.InputStream(
                    samplerate=SEND_SAMPLE_RATE, channels=CHANNELS,
                    dtype="int16", blocksize=CHUNK_SIZE, device=i,
                    callback=_probe_callback,
                ):
                    got_audio.wait(timeout=0.5)
            except Exception:
                continue
            if got_audio.is_set():
                return i, d["name"]
        return None

    found = _probe(priority) or _probe(fallback)
    if found is not None:
        return found

    raise RuntimeError(
        f"No microphone could actually be opened ({len(input_capable)} input-capable "
        "device(s) enumerated, all failed to open or deliver audio)."
    )


def _resolve_output_device() -> tuple[int, str]:
    """Resolve a usable speaker/headphone output device index.

    Same class of bug as _resolve_input_device, mirrored on playback:
    Windows' default OUTPUT device has been observed both wrong (an
    HDMI-connected TV) and outright invalid (-1) at different times on this
    machine — never trust it blindly.

    2026-08-11 update: VB-Audio Voicemeeter (and the TV) are no longer
    present in this machine's device list at all — the ~30 Voicemeeter
    virtual devices this function used to fall back on for blocking-mode
    output are simply gone now. That fallback would silently stop working
    the moment Voicemeeter was removed, which is exactly what happened
    (confirmed live: sd.default.device[1] == -1, zero Voicemeeter devices
    enumerated, and every remaining real "Speakers"/"Headphones" device is
    a raw WDM-KS pin that rejects the *blocking* API outright — "Invalid
    device" [-9996] — the same way it rejected blocking-mode input before
    _listen_audio() was switched to a callback). The actual fix is on the
    playback side, not here: _open_output_stream()/_play_audio() now use a
    callback-driven stream (see _AudioSink below) instead of blocking
    stream.write() calls, so _probe() below tests candidates in callback
    mode too — confirmed live that real "Speakers"/"Headphones" devices
    open and deliver callbacks fine that way. TV/HDMI/sound-mapper devices
    are still excluded outright even though none are present right now, in
    case one reappears (e.g. the TV gets reconnected).
    """
    devices = sd.query_devices()
    default_idx = sd.default.device[1]

    # "Sound Mapper"/"Primary Sound Driver" are meta-devices that just mirror
    # whatever Windows' actual default is — not a distinct choice, so they'd
    # silently redirect right back to the TV. Treated the same as TV/HDMI.
    _AVOID_HINTS = ("tv", "hdmi", "display audio", "sound mapper", "primary sound")

    def _is_avoid(name: str) -> bool:
        return any(hint in name for hint in _AVOID_HINTS)

    def _tier(item) -> int:
        i, d = item
        name = d["name"].lower()
        if _is_avoid(name):
            return 2   # TV/HDMI/display audio — only as an absolute last resort
        if "speaker" in name or "headphone" in name:
            return 0   # real physical output, most likely to be heard
        return 1        # everything else (default meta-device, Voicemeeter, etc.)

    def _rank(item):
        i, d = item
        name = d["name"].lower()
        # Among Voicemeeter's virtual buses, "Voicemeeter Input" (unqualified)
        # is the conventional main bus most Voicemeeter setups route to real
        # speakers by default — AUX/In2-5/VAIO3 are typically secondary buses
        # that may not be routed anywhere, so prefer the main one first.
        is_secondary_voicemeeter_bus = "voicemeeter" in name and "voicemeeter input" not in name
        return (_tier(item), is_secondary_voicemeeter_bus, i != default_idx)

    output_capable = [
        (i, d) for i, d in enumerate(devices)
        if d.get("max_output_channels", 0) > 0
    ]
    if not output_capable:
        raise RuntimeError("No usable speaker/headphone output device found (0 output-capable devices enumerated).")

    ranked = sorted(output_capable, key=_rank)
    preferred = [c for c in ranked if _tier(c) < 2]
    last_resort = [c for c in ranked if _tier(c) == 2]

    def _silence_callback(outdata, frames, time_info, status):
        outdata[:] = b"\x00" * len(outdata)

    def _probe(candidates: list) -> tuple[int, str] | None:
        for i, d in candidates:
            try:
                # Callback mode, matching how _open_output_stream actually
                # uses the device now — blocking-mode probing here rejected
                # real WDM-KS Speakers/Headphones devices that work fine in
                # callback mode (confirmed live), which is what caused
                # every real output device to look unusable.
                with sd.RawOutputStream(
                    samplerate=RECEIVE_SAMPLE_RATE, channels=CHANNELS,
                    dtype="int16", blocksize=CHUNK_SIZE, device=i,
                    callback=_silence_callback,
                ):
                    pass
            except Exception:
                continue
            return i, d["name"]
        return None

    found = _probe(preferred) or _probe(last_resort)
    if found is not None:
        return found

    raise RuntimeError(
        f"No speaker/headphone output device could actually be opened ({len(output_capable)} "
        "output-capable device(s) enumerated, all failed to open)."
    )


class _AudioSink:
    """Thread-safe byte buffer feeding a callback-mode sd.RawOutputStream.

    _play_audio() (asyncio) calls write() to hand off audio bytes; the
    PortAudio callback (a *different*, non-asyncio thread) calls read() to
    pull exactly the number of bytes it needs for that block, padding with
    silence on underrun rather than blocking — a callback must never block
    or PortAudio glitches/drops out. This replaces the old design where
    _play_audio wrote directly to the stream via a blocking stream.write()
    call in a thread pool, which real WDM-KS Speakers/Headphones devices on
    this machine reject outright ("Invalid device" [-9996]); those same
    devices work fine in callback mode (confirmed live), hence this buffer.
    """

    def __init__(self):
        self._buf = bytearray()
        self._lock = threading.Lock()

    def write(self, data: bytes) -> None:
        with self._lock:
            self._buf.extend(data)

    def read(self, n: int) -> bytes:
        with self._lock:
            chunk = bytes(self._buf[:n])
            del self._buf[:n]
        if len(chunk) < n:
            chunk += b"\x00" * (n - len(chunk))
        return chunk

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JARVIS, Tony Stark's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

class _VoiceSettingsChanged(Exception):
    """Internal signal raised to force a session reconnect after the user
    changes the voice provider/voice/speed, so the change applies live
    instead of waiting for the next app restart or network reconnect."""


_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

def _clean_transcript(text: str) -> str:
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()


def _classify_connection_error(err: BaseException, prev_backoff: int = 3) -> tuple[str, int]:
    """Turn a raw exception from the Live-session connect/run loop into a
    short, English, user-facing log line and the backoff (seconds) to wait
    before reconnecting.

    Pure/stateless (no self, no I/O) so it's unit-testable without a running
    event loop or live session — see tests/test_connection_error_reporting.py.

    Four cases, in priority order:
      1. Audio device errors (raised by _resolve_input_device /
         _resolve_output_device when no usable mic/speaker is found) get a
         specific, actionable message. Previously these fell through to the
         generic branch below with NO ui.write_log call at all — the app
         just retried silently forever with zero visible explanation.
      2. Gemini quota/rate-limit errors (429 / RESOURCE_EXHAUSTED) get an
         escalating backoff capped at 120s and a message that names the
         cause. Previously these fell through to the generic branch, which
         retried every flat 3s against an endpoint that was already
         rejecting requests — hammering a rate-limited API is the wrong
         response and only prolongs the outage.
      3. Network/timeout errors get an escalating backoff (capped at 60s)
         and a plain-English message. Previously this message was
         hardcoded in Turkish regardless of the user's configured language
         — a leftover dev-local string.
      4. Anything else gets a generic but still visible message instead of
         silence.
    """
    err_str = str(err)
    lower = err_str.lower()

    if "microphone" in lower or "speaker/headphone" in lower:
        return (
            f"ERR: Audio device problem — {err_str} "
            "Check Windows Sound settings and reconnect your microphone/speakers.",
            3,
        )

    is_quota_err = (
        "429" in err_str
        or "resource_exhausted" in lower
        or "quota" in lower
    )
    if is_quota_err:
        backoff = min(max(prev_backoff * 2, 30), 120)
        return (
            f"ERR: Gemini API quota/rate limit hit (429) — backing off {backoff}s "
            "before retrying. Check your Gemini API plan/quota if this keeps happening.",
            backoff,
        )

    is_net_err = any(k in err_str for k in (
        "TimeoutError", "timed out", "getaddrinfo", "CancelledError",
        "ConnectionRefusedError", "OSError", "Cannot connect",
    ))
    if is_net_err:
        backoff = min(prev_backoff * 2, 60)
        return (
            f"NET: Could not connect — retrying in {backoff}s. (Check your internet connection / VPN.)",
            backoff,
        )

    return (
        f"ERR: JARVIS lost connection — retrying in 3s. ({type(err).__name__})",
        3,
    )


def _post_session_state(had_error: bool) -> str:
    """Which HUD state to show after a run()-loop iteration ends:
    'RECONNECTING' if it ended because of a real error (about to retry
    with backoff), or 'SLEEPING' for a normal/clean disconnect. Without
    this distinction the HUD looked identical either way — a user glancing
    at the orb (rather than the scrolling log panel) had no way to tell
    "waiting to retry after a failure" from "nothing is wrong." Pure
    function so it's unit-testable without driving the full run() loop."""
    return "RECONNECTING" if had_error else "SLEEPING"

from core.headless.tool_registry import TOOL_DECLARATIONS, SESSION_ONLY_TOOLS  # noqa: F401 (SESSION_ONLY_TOOLS used in _execute_tool)

# --- Plugin system ---


class JarvisLive:

    def __init__(self, ui: JarvisUI):
        self.ui             = ui
        self._asst_name     = "JARVIS"   # updated each session from config
        self.session              = None
        self.audio_in_queue       = None
        self.out_queue            = None
        self._loop                = None
        self._is_speaking         = False
        self._speaking_lock       = threading.Lock()
        self._phone_active        = False   # True while phone mic is streaming; pauses PC mic
        self._pending_vision       = None    # (img_bytes, mime_type, question, angle) to inject after tool response
        self._vision_cam_active    = False   # True if camera was opened for vision → auto-close after response
        self._vision_close_pending = False   # True after vision injected; next turn_complete closes camera
        self._vision_last_time     = 0.0     # monotonic time of last screen_process call (cooldown guard)
        self._vision_busy          = False   # True while a vision capture/inject cycle is in flight
        self._interrupted          = False   # True while draining audio after user interrupt
        self._voice_provider       = "gemini"  # active provider for the current session
        self._tts_player           = None      # non-Gemini TTSPlayer (core.tts), else None
        self._voice_reload_pending = False     # set True to force a reconnect on voice settings save
        self._out_stream           = None      # live sd.RawOutputStream (callback mode) — interrupt() aborts this directly
        self._out_sink             = None      # _AudioSink feeding the above stream's callback — interrupt() clears this
        self._current_speech_text  = None      # text currently being spoken by _tts_player (self-echo check)
        self.ui.on_text_command   = self._on_text_command
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_interrupt      = self.interrupt
        self.ui.on_voice_settings_changed = self._on_voice_settings_changed
        self._turn_done_event: asyncio.Event | None = None
        self._dashboard     = None
        self._briefing_sent    = False          # morning briefing fires once per process
        self._sys_monitor      = SystemMonitor()  # persistent cooldown state
        self._proactive        = ProactiveEngine()
        self._last_user_speech = time.monotonic()  # updated on every user utterance
        self._session_log: list[str] = []          # conversation turns for end-of-session summary

    def _make_remote_key(self):
        """Called from Qt main thread when user presses Remote Control."""
        if self._dashboard is None:
            self.ui.write_log(
                "SYS: Dashboard unavailable. "
                "Run: pip install fastapi \"uvicorn[standard]\" cryptography"
            )
            return None
        key    = self._dashboard.new_key()
        url    = self._dashboard.get_url()
        manual = self._dashboard.get_manual_url()
        return url, key, f"{url}/auto-login?key={key}", manual

    def _on_voice_settings_changed(self, voice_cfg: dict):
        """Called from the Qt thread when the user saves new voice settings.
        Flags a reconnect so the change takes effect on the live session
        instead of only after the next app restart."""
        self._voice_reload_pending = True
        self.ui.write_log("SYS: Voice settings changed — reconnecting to apply...")

    def _on_text_command(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def _broadcast_orb_state(self, state: str) -> None:
        """Best-effort push of JARVIS's current state (idle/listening/
        thinking/speaking/interrupted) to the 3D command center orb. Never
        blocks or raises — this is cosmetic, not core functionality, and
        must not affect the voice loop if the dashboard is down.
        Safe to call from any thread: run_coroutine_threadsafe doesn't block
        even when called from the loop's own thread."""
        dashboard = getattr(self, "_dashboard", None)
        loop = getattr(self, "_loop", None)
        if not dashboard or not loop:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                dashboard.broadcast_nav({"type": "jarvis_state", "state": state}),
                loop,
            )
        except Exception:
            pass

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self.ui.set_state("SPEAKING")
            self._broadcast_orb_state("speaking")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")
            self._broadcast_orb_state("listening")

    def interrupt(self) -> None:
        """Stop JARVIS mid-speech immediately and switch back to listening.

        Reaches every real audio output layer, not just internal flags:
        aborts the live sounddevice output stream (Gemini-audio path — see
        _play_audio) and stops the active TTSPlayer (local/ElevenLabs path —
        see _speak_with_tts_player), both of which discard already-buffered
        audio instead of waiting for it to finish playing.
        """
        self._interrupted = True
        self._broadcast_orb_state("interrupted")
        self._turn_done_event = self._turn_done_event or asyncio.Event()
        if self._turn_done_event:
            self._turn_done_event.clear()

        out_stream = getattr(self, "_out_stream", None)
        if out_stream is not None:
            try:
                out_stream.abort()   # immediate — does NOT wait for buffered audio to drain
            except Exception:
                pass
        out_sink = getattr(self, "_out_sink", None)
        if out_sink is not None:
            out_sink.clear()   # discard anything already handed to the sink but not yet played

        q = self.audio_in_queue
        if q:
            drained = 0
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except Exception:
                    break
            if drained:
                print(f"[JARVIS] ✋ Interrupted — {drained} audio chunks discarded")
        player = getattr(self, "_tts_player", None)
        if player:
            try:
                player.stop()
            except Exception:
                pass
        self.set_speaking(False)
        if getattr(self, "_loop", None) and getattr(self, "session", None):
            self.ui.set_state("LISTENING")
        self.ui.write_log("SYS: Interrupted — listening...")

    def speak(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"Sir, {tool_name} encountered an error. {short}")

    @property
    def _tool_executor(self) -> ToolExecutor:
        """Lazy, cached ToolExecutor wrapping this instance's ui/speak/
        proactive engine. A property (not a plain __init__ attribute)
        because a number of existing tests build JarvisLive via
        object.__new__() and set only a few attributes by hand, bypassing
        __init__ entirely — this still works for them since it reads
        whatever's on `self` at first access instead of requiring __init__
        to have run. Cached after first access so repeated tool calls on
        the same instance share one ToolContext (and therefore the same
        ProactiveEngine, if one was already set on self._proactive)."""
        cached = self.__dict__.get("_tool_executor_cache")
        if cached is None:
            ctx = ToolContext(ui=self.ui, speak=self.speak, proactive=getattr(self, "_proactive", None))
            cached = ToolExecutor(ctx)
            self.__dict__["_tool_executor_cache"] = cached
        return cached

    def _build_config(self, voice_cfg: dict | None = None) -> types.LiveConnectConfig:
        from datetime import datetime

        # Load customization from config
        try:
            _cfg = json.loads(open(API_CONFIG_PATH, encoding="utf-8").read())
            self._asst_name = (_cfg.get("assistant_name") or "JARVIS").strip()
            _user_name = (_cfg.get("user_name") or "").strip()
        except Exception:
            self._asst_name = "JARVIS"
            _user_name = ""

        voice_cfg = voice_cfg or get_voice_provider_config()
        voice_name = voice_cfg.get("voice") or "Charon"
        provider   = (voice_cfg.get("provider") or "gemini").lower()
        self._voice_provider = provider

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        # Identity injection — overrides any hardcoded name in prompt.txt
        _addr = (f"ADDRESS: Always call the user '{_user_name}'."
                 if _user_name
                 else "ADDRESS: When speaking Turkish → always say \"efendim\". "
                      "When speaking English → say \"sir\". Never mix languages.")
        identity_ctx = (
            f"[IDENTITY]\n"
            f"Your name is {self._asst_name}. "
            f"Always refer to yourself as {self._asst_name}.\n"
            f"{_addr}\n\n"
        )

        parts = [time_ctx, identity_ctx, format_objective_for_prompt()]
        if mem_str:
            parts.append(mem_str)
        parts.append(sys_prompt)

        live_kwargs = dict(
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": TOOL_DECLARATIONS}],
            session_resumption=types.SessionResumptionConfig(),
            # Server-side VAD on the mic stream, which now stays open the whole
            # time JARVIS is speaking (see _listen_audio) — this is what makes
            # real barge-in possible instead of a custom VAD implementation.
            # LOW start-sensitivity + prefix padding require a bit of sustained
            # speech before committing, to avoid JARVIS's own audio bleeding
            # into the mic (acoustic echo) triggering a self-interrupt.
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
                    prefix_padding_ms=BARGE_IN_PREFIX_PADDING_MS,
                ),
                activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
            ),
        )

        if provider == "gemini":
            # Gemini Live speaks its own audio directly — original behaviour, unchanged.
            live_kwargs["response_modalities"] = ["AUDIO"]
            live_kwargs["output_audio_transcription"] = {}
            live_kwargs["speech_config"] = types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=voice_name
                    )
                )
            )
        else:
            # ElevenLabs / Local (Kokoro): Gemini only reasons in text; the
            # resulting text is spoken by self._tts_player (see run()/_receive_audio()).
            live_kwargs["response_modalities"] = ["TEXT"]

        return types.LiveConnectConfig(**live_kwargs)

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})

        print(f"[JARVIS] 🔧 {name}  {args}")
        self.ui.set_state("THINKING")
        self._broadcast_orb_state("thinking")

        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        loop   = asyncio.get_event_loop()
        result = "Done."

        try:
            if name == "screen_process":
                import time as _t_mod
                _now = _t_mod.monotonic()
                _cooldown = 4.0  # seconds — covers echo window after speaking ends
                if self._vision_busy or (_now - self._vision_last_time) < _cooldown:
                    _wait = max(0, _cooldown - (_now - self._vision_last_time))
                    print(f"[Vision] ⏳ Cooldown active ({_wait:.1f}s remaining) — ignoring duplicate call")
                    result = "Vision is still processing the previous request. I will not call this again."
                else:
                    self._vision_busy      = True
                    self._vision_last_time = _now
                    angle     = args.get("angle", "screen").lower()
                    user_text = args.get("text", "What do you see?")
                    if angle == "camera":
                        img_b, mime_t = await loop.run_in_executor(None, _capture_camera)
                        self.ui.start_camera_stream()
                        self._vision_cam_active = True
                        print(f"[Vision] 📷 Camera: {len(img_b):,} bytes")
                        _stall = "camera"
                    else:
                        img_b, mime_t = await loop.run_in_executor(None, _capture_screen)
                        print(f"[Vision] 🖥️  Screen: {len(img_b):,} bytes")
                        _stall = "screen"
                    self._pending_vision = (img_b, mime_t, user_text, angle)
                    result = (
                        f"[VISION_ACTIVE] {_stall.capitalize()} captured. "
                        f"Immediately say ONE short natural sentence in the user's own language, "
                        f"telling them you are looking at their {_stall} right now. "
                        f"Do NOT describe or guess content — the actual image arrives in the NEXT message."
                    )

            elif name == "close_camera":
                self.ui.stop_camera_stream()
                result = "Camera closed."
            elif name == "navigate_command_center":
                parsed = parse_navigation_command(args.get("action"), args.get("target"))
                if parsed.get("error"):
                    result = parsed["error"]
                elif self._dashboard is None:
                    result = "The command center dashboard isn't running, so I can't navigate it right now."
                else:
                    event = self._dashboard.apply_navigation(
                        parsed["action"], parsed.get("nucleus_id") or ""
                    )
                    await self._dashboard.broadcast_nav(event)
                    if parsed["action"] == "status":
                        result = f"You're currently looking at the {event['name']} nucleus."
                    elif parsed["action"] == "home":
                        result = "Returning to the central command center."
                    elif parsed["action"] == "back":
                        result = f"Going back to {event['name']}."
                    else:
                        result = f"Opening the {event['name']} nucleus."
            elif name == "shutdown_jarvis":
                self.ui.write_log("SYS: Shutdown requested.")
                async def _do_shutdown():
                    await self._save_session_summary()
                    if self.session:
                        try:
                            await self.session.send_client_content(
                                turns={"parts": [{"text": "Say a brief natural goodbye to the user."}]},
                                turn_complete=True,
                            )
                        except Exception:
                            pass
                    await asyncio.sleep(1.5)
                    # Release the app-instance and agent-scheduler locks
                    # before the hard exit below — os._exit() bypasses all
                    # normal Python cleanup (no finally blocks, no atexit),
                    # so this is the only chance to leave the lock files
                    # clean for the next launch rather than relying solely
                    # on their dead-PID stale-reclaim logic.
                    graceful_release_all_locks()
                    import os as _os
                    _os._exit(0)
                asyncio.create_task(_do_shutdown())
            else:
                result = await self._tool_executor.execute(name, args)

        except UnknownToolError as e:
            result = str(e)
        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        print(f"[JARVIS] 📤 {name} → {str(result)[:80]}")
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result}
        )

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            await self.session.send_realtime_input(media=msg)

    def _should_forward_mic_audio(self) -> bool:
        """Whether _listen_audio's callback should forward the current mic
        frame to Gemini. Deliberately does NOT check _is_speaking — real
        barge-in requires the mic to stay open while JARVIS talks; self-echo
        is handled by server-side VAD tuning and _looks_like_self_echo
        instead of blanket-muting the mic during playback."""
        return not self.ui.muted and not self._phone_active

    async def _listen_audio(self):
        print("[JARVIS] 🎤 Mic started")
        loop = asyncio.get_event_loop()

        try:
            device_index, device_name = _resolve_input_device()
        except Exception as e:
            print(f"[JARVIS] ❌ Mic: {e}")
            raise
        print(f"[JARVIS] 🎤 Selected input device: [{device_index}] {device_name}")

        def callback(indata, frames, time_info, status):
            if self._should_forward_mic_audio():
                data = indata.tobytes()
                loop.call_soon_threadsafe(
                    self.out_queue.put_nowait,
                    {"data": data, "mime_type": "audio/pcm"}
                )

        try:
            with sd.InputStream(
                samplerate=SEND_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                device=device_index,
                callback=callback,
            ):
                print("[JARVIS] 🎤 Mic stream open")
                while True:
                    await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[JARVIS] ❌ Mic: {e}")
            raise

    async def _receive_audio(self):
        print("[JARVIS] 👂 Recv started")
        out_buf, in_buf = [], []
        _diag_awaiting_first_audio = True   # DIAGNOSTIC: temporary latency instrumentation

        try:
            while True:
                async for response in self.session.receive():

                    if response.data:
                        if _diag_awaiting_first_audio:
                            print(f"[DIAG] first response audio chunk: +{time.monotonic() - self._last_user_speech:.3f}s after last input transcript")
                            _diag_awaiting_first_audio = False
                        if self._interrupted:
                            pass  # discard: interrupted
                        else:
                            if self._turn_done_event and self._turn_done_event.is_set():
                                self._turn_done_event.clear()
                            # Split into ~50 ms chunks so interrupt() stops audio within 50 ms
                            # (24000 Hz × 2 bytes/sample × 0.05 s = 2400 bytes per slice)
                            _audio_data = response.data
                            _SLICE = 2400
                            for _i in range(0, len(_audio_data), _SLICE):
                                self.audio_in_queue.put_nowait(_audio_data[_i : _i + _SLICE])

                    if response.server_content:
                        sc = response.server_content

                        if sc.interrupted:
                            # Gemini's own server-side VAD detected the user talking
                            # over JARVIS (see realtime_input_config in _build_config)
                            # — this is the primary barge-in signal for the Gemini
                            # audio path. Route through the same interrupt() used by
                            # the manual ESC/button path so every layer stops together.
                            self.interrupt()

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            if txt and txt != (out_buf[-1] if out_buf else ""):
                                out_buf.append(txt)

                        # TEXT-modality reply (ElevenLabs/Local voice providers — see
                        # _build_config): no audio output, so there is no
                        # output_transcription. The model's answer arrives as plain
                        # text parts on the response instead.
                        elif self._voice_provider != "gemini" and response.text:
                            txt = _clean_transcript(response.text)
                            if txt and txt != (out_buf[-1] if out_buf else ""):
                                out_buf.append(txt)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                if not in_buf:
                                    print(f"[DIAG] first input transcript chunk at {time.monotonic():.3f}: {txt!r}")
                                in_buf.append(txt)
                                self._last_user_speech = time.monotonic()

                                # Local/ElevenLabs playback has no server-side
                                # "interrupted" signal to hook (Gemini's own text
                                # generation for the turn is long finished by the
                                # time local TTS is still speaking it) — use fresh
                                # transcribed user speech arriving mid-playback as
                                # the interrupt trigger instead.
                                with self._speaking_lock:
                                    speaking = self._is_speaking
                                if (
                                    self._voice_provider != "gemini"
                                    and speaking
                                    and not self._looks_like_self_echo(txt)
                                ):
                                    self.interrupt()

                        if sc.turn_complete:
                            print(f"[DIAG] turn_complete at {time.monotonic():.3f}")
                            _diag_awaiting_first_audio = True   # DIAGNOSTIC
                            if self._turn_done_event:
                                self._turn_done_event.set()

                            # If this turn_complete ends an interrupted response, clear the
                            # flag and skip all further processing for that turn.
                            if self._interrupted:
                                self._interrupted = False
                                in_buf  = []
                                out_buf = []
                                continue

                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                self.ui.write_log(f"You: {full_in}")
                                self._session_log.append(f"User: {full_in}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "user",
                                        "text": full_in,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            in_buf = []

                            full_out = " ".join(out_buf).strip()
                            if full_out:
                                self.ui.write_log(f"{self._asst_name}: {full_out}")
                                self._session_log.append(f"{self._asst_name}: {full_out}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "jarvis",
                                        "text": full_out,
                                        "ts": datetime.now().isoformat(),
                                    }))
                                if self._voice_provider != "gemini" and self._tts_player:
                                    # Gemini answered in text only (see _build_config) —
                                    # speak it with the selected non-Gemini engine.
                                    asyncio.create_task(self._speak_with_tts_player(full_out))
                            out_buf = []

                            # Vision injection: model finished tool-response turn → now send the image
                            if self._pending_vision and self.session:
                                import base64 as _b64
                                img_b, mime_t, question, angle = self._pending_vision
                                self._pending_vision = None
                                b64 = _b64.b64encode(img_b).decode("ascii")
                                print(f"[Vision] 📤 {len(img_b):,} bytes (angle={angle}) → main session")
                                await self.session.send_client_content(
                                    turns={"parts": [
                                        {"inline_data": {"mime_type": mime_t, "data": b64}},
                                        {"text": question},
                                    ]},
                                    turn_complete=True,
                                )
                                # Mark next turn_complete behaviour depending on angle
                                if self._vision_cam_active:
                                    # Camera: keep busy until JARVIS finishes speaking the answer
                                    self._vision_cam_active    = False
                                    self._vision_close_pending = True
                                else:
                                    # Screen-only: no camera to close; release busy flag now
                                    self._vision_busy = False
                            elif self._vision_close_pending:
                                # This turn_complete IS the vision answer — close camera + release busy flag
                                self._vision_close_pending = False
                                self._vision_busy = False
                                async def _cam_close():
                                    await asyncio.sleep(2.0)
                                    self.ui.stop_camera_stream()
                                asyncio.create_task(_cam_close())

                    if response.tool_call:
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            print(f"[JARVIS] 📞 {fc.name}")
                            fr = await self._execute_tool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses
                        )
        except Exception as e:
            print(f"[JARVIS] ❌ Recv: {e}")
            traceback.print_exc()
            raise

    def _looks_like_self_echo(self, heard_text: str) -> bool:
        """Cheap heuristic to cut down false barge-in triggers from acoustic
        echo of JARVIS's own local/ElevenLabs playback being picked back up
        by the mic. This is NOT real echo cancellation — it just skips an
        interrupt trigger when the transcribed snippet is a near-duplicate of
        what JARVIS is currently saying, which is what plain speaker→mic
        bleed usually transcribes as."""
        said = (self._current_speech_text or "").lower()
        heard = heard_text.lower().strip()
        if not said or not heard:
            return False
        return heard in said

    async def _speak_with_tts_player(self, text: str):
        """Speak `text` through the active non-Gemini engine (ElevenLabs/Local).
        Drives _is_speaking via on_start/on_done, same as _play_audio does for
        the Gemini-audio path, so both UI state and the mid-playback interrupt
        trigger in _receive_audio see consistent speaking state."""
        player = self._tts_player
        if not player or self._interrupted:
            return
        self._current_speech_text = text
        try:
            await asyncio.to_thread(
                player.speak,
                text,
                on_start=lambda: self.set_speaking(True),
                on_done=lambda: self.set_speaking(False),
            )
        except Exception as e:
            print(f"[TTS] Playback error: {e}")
            self.ui.write_log(f"ERR: Voice playback failed — {e}")
            self.set_speaking(False)
        finally:
            self._current_speech_text = None

    def _open_output_stream(self) -> tuple[sd.RawOutputStream, "_AudioSink"]:
        """Callback-mode output stream: real "Speakers"/"Headphones" WDM-KS
        devices on this machine reject the blocking API outright ("Invalid
        device" -9996) but work fine in callback mode (confirmed live) —
        see _AudioSink and _resolve_output_device's docstring. _play_audio()
        below only ever calls sink.write(); the callback here is what
        actually feeds PortAudio, on its own thread."""
        device_index, device_name = _resolve_output_device()
        print(f"[JARVIS] 🔊 Selected output device: [{device_index}] {device_name}")
        sink = _AudioSink()

        def _callback(outdata, frames, time_info, status):
            outdata[:] = sink.read(len(outdata))

        stream = sd.RawOutputStream(
            samplerate=RECEIVE_SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=CHUNK_SIZE,
            device=device_index,
            callback=_callback,
        )
        stream.start()
        self._out_stream = stream
        self._out_sink = sink
        return stream, sink

    async def _play_audio(self):
        print("[JARVIS] 🔊 Play started")
        stream, sink = self._open_output_stream()

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=0.1
                    )
                except asyncio.TimeoutError:
                    # Non-Gemini providers own speaking-state via _speak_with_tts_player
                    # (on_start/on_done) instead — Gemini never sends audio data for
                    # them to begin with, so this branch would otherwise flip
                    # _is_speaking off while local/ElevenLabs playback is still in flight.
                    if (
                        self._voice_provider == "gemini"
                        and self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                    continue

                if self._interrupted:
                    self._interrupted = False
                    self.set_speaking(False)
                    # interrupt() already called stream.abort() to kill
                    # in-flight audio immediately — an aborted PortAudio
                    # stream can't just resume, so open a fresh one before
                    # the next write, same as the old blocking-mode design
                    # did after catching a PortAudioError from stream.write().
                    try:
                        stream.close()
                    except Exception:
                        pass
                    stream, sink = self._open_output_stream()
                    continue

                self.set_speaking(True)

                # Batch all immediately-available chunks into one sink.write()
                # call. Unlike the old blocking design this doesn't bound how
                # much can be in flight when interrupt() aborts — sink.clear()
                # (called by interrupt()) handles that instead.
                batch = bytearray(chunk)
                while len(batch) < 4800:   # 4800 bytes ≈ 100 ms at 24 kHz / 16-bit mono
                    try:
                        batch.extend(self.audio_in_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                sink.write(bytes(batch))
        except (RuntimeError, asyncio.CancelledError):
            pass   # executor/loop shutting down — exit cleanly
        except Exception as e:
            print(f"[JARVIS] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
            self._out_stream = None
            self._out_sink = None

    # ── Morning briefing ────────────────────────────────────────────────────────

    async def _send_startup_briefing(self) -> None:
        """
        Two-phase briefing optimized for speed:
          Phase 1 — instant greeting (no tools) → speech starts in <1s
          Phase 2 — news pre-fetched in a background thread while Phase 1 plays,
                    delivered as ready text (no Gemini tool-call round-trip) and
                    shown on the UI content panel. Waits for turn_complete event
                    instead of a fixed sleep so there is no unnecessary gap.
        """
        memory   = load_memory()
        identity = memory.get("identity", {})

        def _val(k: str) -> str:
            e = identity.get(k, {})
            return (e.get("value", "") if isinstance(e, dict) else str(e)).strip()

        lang = _val("language")
        name = _val("name")
        time_str = datetime.now().strftime("%H:%M")

        # Start fetching news immediately — runs in parallel while phase 1 plays
        loop = asyncio.get_event_loop()
        news_future = loop.run_in_executor(None, _fetch_news_sync, "top world news today")

        await asyncio.sleep(0.3)
        if not self.session:
            return

        # ── Phase 1: instant greeting ─────────────────────────────────────────
        lang_clause = f" Respond in {lang}." if lang else ""
        name_clause = f" Address the user as {name}." if name else ""

        # Inject last session context if available — pop removes it so it's never repeated
        last = await asyncio.to_thread(pop_last_session)
        session_clause = ""
        if last:
            try:
                _delta = (datetime.now() - datetime.strptime(last["date"], "%Y-%m-%d")).days
                _when  = "earlier today" if _delta == 0 else ("yesterday" if _delta == 1 else f"{_delta} days ago")
            except Exception:
                _when = "last time"
            session_clause = (
                f" Also briefly and naturally mention that {_when}: {last['summary']}"
            )

        p1 = (
            f"Greet the user warmly, mention it is {time_str}, and say you are fetching today's news now.{session_clause} "
            f"Keep it to 2 short sentences max. Do not call any tools.{lang_clause}{name_clause}"
        )

        # Clear the turn-done event so we can wait for Phase 1 to finish
        if self._turn_done_event:
            self._turn_done_event.clear()

        await self.session.send_client_content(
            turns={"parts": [{"text": p1}]},
            turn_complete=True,
        )
        self.ui.write_log("SYS: Briefing phase 1 (greeting) sent.")

        # ── Phase 2: fire as soon as Phase 1 audio is done ───────────────────
        async def _deliver_news():
            try:
                lang_str = f" Respond in {lang}." if lang else ""

                # Wait for news fetch (already running) and Phase 1 turn-complete
                # in parallel — whichever takes longer determines the wait time
                news_done   = asyncio.wrap_future(news_future)
                turn_waited = False
                if self._turn_done_event:
                    try:
                        await asyncio.wait_for(self._turn_done_event.wait(), timeout=6.0)
                        turn_waited = True
                    except asyncio.TimeoutError:
                        pass

                # Extra buffer: turn_complete fires when Gemini finishes *generating*
                # Phase 1, but audio may still be playing.  Waiting a beat here
                # prevents Phase 2 audio from arriving while Phase 1 is mid-sentence
                # (which sounds like a "repeated first response" to the user).
                if turn_waited:
                    await asyncio.sleep(0.8)
                else:
                    await asyncio.sleep(1.0)

                try:
                    news_text = await asyncio.wait_for(news_done, timeout=4.0)
                except Exception:
                    news_text = ""

                if not self.session:
                    return

                if news_text and len(news_text) > 60:
                    # Show on UI content panel immediately
                    self.ui.show_content("NEWS — top world news today", news_text)

                    p2 = (
                        f"[BRIEFING] Here are today's top news headlines:\n{news_text}\n\n"
                        "Pick ONE headline, summarise it in one sentence, then say the full list "
                        f"is displayed on screen. Do not call any tools.{lang_str}"
                    )
                else:
                    p2 = (
                        "News headlines could not be fetched right now. "
                        f"Let the user know briefly.{lang_str}"
                    )

                await self.session.send_client_content(
                    turns={"parts": [{"text": p2}]},
                    turn_complete=True,
                )
                self.ui.write_log("SYS: Briefing phase 2 (news) sent.")
            except Exception as e:
                print(f"[Briefing] Phase 2 error: {e}")
                self.ui.write_log(f"SYS: Briefing phase 2 failed: {e}")

        asyncio.create_task(_deliver_news())

    # ── Session memory ──────────────────────────────────────────────────────────

    async def _save_session_summary(self) -> None:
        """Summarise the current session in 1-2 sentences and save to long_term.json."""
        log = self._session_log
        if len(log) < 3:          # need at least one exchange to be worth saving
            return
        self._session_log = []    # reset immediately so the next session starts clean

        memory = load_memory()
        lang_entry = memory.get("identity", {}).get("language", {})
        lang = (lang_entry.get("value", "") if isinstance(lang_entry, dict) else str(lang_entry)).strip()
        lang = lang or "English"

        convo = "\n".join(log[-40:])   # cap at last 40 turns to stay within token budget
        prompt = (
            f"Summarize this conversation in 1-2 sentences in {lang}. "
            "Focus on what the user accomplished or discussed. "
            "Output ONLY the summary text, nothing else:\n\n" + convo
        )
        try:
            from core.headless.gemini_client import get_client
            client = get_client(_get_api_key())
            resp   = await asyncio.to_thread(
                client.models.generate_content,
                model="gemini-flash-latest",
                contents=prompt,
            )
            summary = (resp.text or "").strip()
            if summary:
                save_session_summary(summary, lang)
        except Exception as e:
            print(f"[Memory] ⚠️ Session summary failed: {e}")

    # ── System monitor ──────────────────────────────────────────────────────────

    async def _run_system_monitor(self) -> None:
        """Background task: voice alerts when metrics exceed thresholds."""
        while True:
            await asyncio.sleep(10)
            alert = await asyncio.to_thread(self._sys_monitor.check)
            if not alert or not self.session:
                continue
            # Don't interrupt an active conversation
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking or (time.monotonic() - self._last_user_speech) < 10:
                continue
            try:
                await self.session.send_client_content(
                    turns={"parts": [{"text": alert}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[Monitor] ⚠️ Could not send alert: {e}")

    # ── Background monitor ──────────────────────────────────────────────────────

    async def _run_background_monitor(self) -> None:
        """Check user-configured topics once per day; speak alerts when new headlines appear."""
        await asyncio.sleep(300)          # wait 5 min after startup before first check
        while True:
            if self.session:
                # Don't interrupt if user spoke recently or JARVIS is mid-sentence
                with self._speaking_lock:
                    speaking = self._is_speaking
                recent_speech = (time.monotonic() - self._last_user_speech) < 30
                if not speaking and not recent_speech:
                    try:
                        alerts = await asyncio.to_thread(monitor_check_all)
                        memory = load_memory()
                        lang_e = memory.get("identity", {}).get("language", {})
                        lang   = (lang_e.get("value", "") if isinstance(lang_e, dict) else str(lang_e)).strip() or "English"
                        for alert in alerts:
                            msg = (
                                f"{alert}\n\n"
                                f"Inform the user about this development naturally in {lang}. "
                                "One brief sentence only."
                            )
                            await self.session.send_client_content(
                                turns={"parts": [{"text": msg}]},
                                turn_complete=True,
                            )
                            self.ui.write_log(f"SYS: Monitor alert sent.")
                            await asyncio.sleep(6)   # gap between consecutive alerts
                    except Exception as e:
                        print(f"[Monitor] ⚠️ Background check error: {e}")
            await asyncio.sleep(1800)     # check every 30 minutes

    # ── Proactive mode ──────────────────────────────────────────────────────────

    async def _run_proactive_mode(self) -> None:
        """
        Background task: periodically checks if the user has been silent long enough,
        then hands time + memory context to Gemini so it can decide what (if anything)
        to say proactively. No hardcoded rules — Gemini makes the call.

        Reads the enabled flag and quiet-hours window fresh from config on
        every check (not cached at startup) so a live voice command to
        disable/snooze proactive mode takes effect on the very next check,
        not just after a restart.
        """
        while True:
            await asyncio.sleep(60)   # evaluate once per minute

            if not self.session:
                continue

            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue

            enabled     = await asyncio.to_thread(get_proactive_enabled)
            quiet_hours = await asyncio.to_thread(get_proactive_quiet_hours)
            if not self._proactive.should_trigger(self._last_user_speech, enabled=enabled, quiet_hours=quiet_hours):
                continue

            self._proactive.mark_triggered()

            try:
                memory       = await asyncio.to_thread(load_memory)
                monitors     = await asyncio.to_thread(list_monitors)
                recent_turns = self._session_log[-8:] if self._session_log else []
                prompt = self._proactive.build_prompt(
                    memory       = memory,
                    monitors     = monitors or None,
                    recent_turns = recent_turns or None,
                )
                await self.session.send_client_content(
                    turns={"parts": [{"text": prompt}]},
                    turn_complete=True,
                )
                self.ui.write_log("SYS: Proactive check-in.")
            except Exception as e:
                print(f"[Proactive] ⚠️ {e}")

    async def _run_agent_scheduler(self) -> None:
        """Background autonomy (Phase 7): periodically runs any agent that is
        due per its own `schedule` field. Both safety gates live in
        AgentOrchestrator.get_due_agents(), not here — an agent only runs
        unattended if Lee/Jarvis has explicitly started it (IDLE, via the
        agent_orchestrator tool's 'start' action) AND it isn't EXECUTE-level
        (those always need explicit approve_task, schedule or not). Results
        land in the orchestrator's task/event history either way; this loop
        just also surfaces a log line + toast so they're not silent.

        Holds a single-instance file lock (actions/agent_orchestrator.py's
        acquire/refresh/release_scheduler_lock) for the duration this
        process is actively scheduling — guards against two JARVIS
        processes both deciding the same due agent should run. A process
        that doesn't get the lock keeps retrying every poll rather than
        being permanently locked out (the other instance may exit later),
        and a stale lock (dead PID, or just old) is reclaimed automatically
        — see acquire_scheduler_lock's docstring."""
        have_lock = await asyncio.to_thread(agent_scheduler_lock.acquire_scheduler_lock)
        if not have_lock:
            print("[AgentScheduler] Another JARVIS instance holds the scheduler lock — will keep retrying.")
        try:
            while True:
                await asyncio.sleep(300)   # poll every 5 min; each agent's own `schedule` governs actual cadence
                if not have_lock:
                    have_lock = await asyncio.to_thread(agent_scheduler_lock.acquire_scheduler_lock)
                    if not have_lock:
                        continue
                else:
                    await asyncio.to_thread(agent_scheduler_lock.refresh_scheduler_lock)
                try:
                    due = await asyncio.to_thread(agent_orchestrator.get_due_agents)
                    for agent in due:
                        task = await asyncio.to_thread(
                            agent_orchestrator.assign_task, agent.id, "Scheduled background check"
                        )
                        summary_text = (task.result or {}).get("summary", "completed") if task.result else (task.error or "failed")
                        self.ui.write_log(f"AGENT: {agent.name} — {summary_text}")
                        if self._dashboard:
                            await self._dashboard.broadcast_nav({
                                "type": "notification", "text": f"🤖 {agent.name}: {summary_text}",
                            })
                except Exception as e:
                    print(f"[AgentScheduler] ⚠️ {e}")
        finally:
            if have_lock:
                await asyncio.to_thread(agent_scheduler_lock.release_scheduler_lock)

    # ── Phone audio relay ────────────────────────────────────────────────────────

    async def _relay_phone_audio(self) -> None:
        """Forward phone mic PCM chunks from dashboard queue into the Gemini Live session."""
        q = self._dashboard._phone_audio_queue
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # No audio for 1 s → phone mic inactive, give PC mic back
                self._phone_active = False
                continue
            self._phone_active = True   # phone is streaming — silence PC mic
            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking and not self.ui.muted:
                try:
                    self.out_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass

    def _on_phone_connected(self) -> None:
        self.ui.write_log("SYS: Phone connected via Remote Dashboard.")
        self.ui.notify_phone_connected()

    # ── dashboard command relay ─────────────────────────────────────────────

    async def _process_dashboard_commands(self) -> None:
        while True:
            try:
                text = await asyncio.wait_for(
                    self._dashboard._command_queue.get(), timeout=0.5
                )
                if not text:
                    continue
                # Wait up to 8s for session to become ready after a wake
                for _ in range(80):
                    if self.session:
                        break
                    await asyncio.sleep(0.1)
                if self.session:
                    await self.session.send_client_content(
                        turns={"parts": [{"text": text}]},
                        turn_complete=True,
                    )
                    self.ui.write_log(f"[Web]: {text}")
                else:
                    print(f"[Dashboard] Dropped command (no session): {text}")
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                print(f"[Dashboard] Command error: {e}")
                await asyncio.sleep(0.5)

    async def _watch_voice_reload(self) -> None:
        """Break out of the current Live session when voice settings change,
        so run()'s existing reconnect loop re-reads config and applies the
        new provider/voice/speed immediately instead of on next restart."""
        while True:
            await asyncio.sleep(0.5)
            if self._voice_reload_pending:
                self._voice_reload_pending = False
                raise _VoiceSettingsChanged()

    # ── main loop ───────────────────────────────────────────────────────────

    async def run(self):
        self._loop = asyncio.get_event_loop()

        # Start dashboard (optional — needs: pip install fastapi "uvicorn[standard]" cryptography)
        try:
            from dashboard.server import DashboardServer
            self._dashboard = DashboardServer()
            self._dashboard.set_connect_callback(self._on_phone_connected)
            asyncio.create_task(self._dashboard.serve())
            # Runs for the whole lifetime, not just inside an active session
            asyncio.create_task(self._process_dashboard_commands())
        except Exception as e:
            print(f"[Dashboard] Disabled: {e}")
            self._dashboard = None

        while True:
            _had_error = False   # distinguishes a real error's retry from a clean disconnect's — see below
            try:
                print("[JARVIS] Connecting...")
                self.ui.set_state("THINKING")

                voice_cfg = get_voice_provider_config()
                self._tts_player = None
                if voice_cfg.get("provider", "gemini") != "gemini":
                    try:
                        self._tts_player = await self._loop.run_in_executor(
                            None, build_tts_player, voice_cfg
                        )
                    except Exception as e:
                        print(
                            f"[JARVIS] ⚠ Voice provider '{voice_cfg.get('provider')}' "
                            f"failed to initialize — falling back to Gemini voice: {e}"
                        )
                        self.ui.write_log(
                            f"SYS: {voice_cfg.get('provider')} voice unavailable — using Gemini voice."
                        )
                        voice_cfg = dict(voice_cfg, provider="gemini")

                config = self._build_config(voice_cfg)

                # Fresh client on every reconnect — avoids stale HTTP session state
                client = genai.Client(
                    api_key=_get_api_key(),
                    http_options={"api_version": "v1beta"}
                )

                async with (
                    client.aio.live.connect(model=LIVE_MODEL, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session          = session
                    self.audio_in_queue   = asyncio.Queue()
                    self.out_queue        = asyncio.Queue(maxsize=200)
                    self._turn_done_event = asyncio.Event()

                    # Reset transient state that must not carry over from a previous session
                    self._pending_vision       = None
                    self._vision_cam_active    = False
                    self._vision_close_pending = False
                    self._vision_busy          = False
                    self._vision_last_time     = 0.0
                    self._interrupted          = False
                    self._voice_reload_pending = False

                    print("[JARVIS] Connected.")
                    self.ui.set_state("LISTENING")
                    self.ui.write_log("SYS: JARVIS online.")

                    if self._dashboard:
                        await self._dashboard.broadcast({"type": "status", "state": "active"})

                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._run_system_monitor())
                    tg.create_task(self._run_background_monitor())
                    tg.create_task(self._run_proactive_mode())
                    tg.create_task(self._run_agent_scheduler())
                    tg.create_task(self._watch_voice_reload())
                    if self._dashboard:
                        tg.create_task(self._relay_phone_audio())

                    # Morning briefing — fires once per process launch (if enabled)
                    if not self._briefing_sent and get_brief_enabled():
                        self._briefing_sent = True
                        tg.create_task(self._send_startup_briefing())

            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except BaseException as e:
                # Catches both Exception and BaseExceptionGroup (Python 3.11+
                # TaskGroup raises BaseExceptionGroup when tasks are cancelled
                # externally, which `except Exception` would miss, letting the
                # exception escape the while-loop and causing asyncio.run() to
                # start shutdown — resulting in "executor after shutdown" errors).
                # Voice settings changed (_watch_voice_reload) — reconnect quietly,
                # this isn't a real error. TaskGroup wraps it in a Base/ExceptionGroup,
                # so check via .subgroup() instead of isinstance(e, _VoiceSettingsChanged).
                voice_change = (
                    e.subgroup(_VoiceSettingsChanged)
                    if isinstance(e, BaseExceptionGroup)
                    else (e if isinstance(e, _VoiceSettingsChanged) else None)
                )
                if voice_change is not None:
                    print("[JARVIS] Voice settings changed — reconnecting...")
                    self._conn_backoff = 0
                    continue

                err_str = str(e)
                print(f"[JARVIS] Error ({type(e).__name__}): {e}")
                traceback.print_exc()

                # Invalid API key — stop hammering the API, prompt re-configuration
                if "API key not valid" in err_str or "1007" in err_str:
                    self.ui.write_log("ERR: API key invalid — please re-enter your key.")
                    self.ui.set_state("SLEEPING")
                    self._broadcast_orb_state("idle")
                    self.ui.prompt_reconfig()
                    while not self.ui._win._ready:
                        await asyncio.sleep(1)
                    print("[JARVIS] New API key saved — reconnecting...")
                    _conn_backoff = 3
                    continue

                # Every other failure — including audio-device errors that used
                # to retry silently with no visible explanation at all — always
                # gets a plain-English, user-visible log line. See
                # _classify_connection_error's docstring for the three cases.
                log_msg, _conn_backoff = _classify_connection_error(e, getattr(self, "_conn_backoff", 3))
                self._conn_backoff = _conn_backoff
                self.ui.write_log(log_msg)
                _had_error = True
            finally:
                self.session = None
                # Only save if there was a real conversation (≥3 turns)
                if len(self._session_log) >= 3:
                    asyncio.create_task(self._save_session_summary())

            self.set_speaking(False)
            self.ui.set_state(_post_session_state(_had_error))
            self._broadcast_orb_state("idle")

            if self._dashboard:
                await self._dashboard.broadcast({"type": "status", "state": "sleeping"})

            delay = getattr(self, "_conn_backoff", 3)
            print(f"[JARVIS] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)

def main():
    print_startup_banner()
    check_single_instance()

    ui = JarvisUI("face.png")

    def runner():
        ui.wait_for_api_key()
        jarvis = JarvisLive(ui)
        try:
            asyncio.run(jarvis.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")
        finally:
            # Covers Ctrl+C / an unhandled exception breaking out of
            # run()'s reconnect loop. The window-close (X button) path is
            # covered separately by QApplication.aboutToQuit in ui.py,
            # since that never reaches this thread at all.
            graceful_release_all_locks()

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()

if __name__ == "__main__":
    main()