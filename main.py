import platform as _platform
import subprocess as _subprocess

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
import os
import re
import threading
import time
import json
import sys
import traceback
import uuid
from datetime import datetime
from pathlib import Path

import numpy as np
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
from actions.proactive         import ProactiveEngine
from actions.background_monitor import (
    add_monitor, remove_monitor, list_monitors, check_all as monitor_check_all,
)
from core.headless.context import ToolContext
from core.headless.tool_executor import ToolExecutor, UnknownToolError
from core.conversation import (
    ConversationManager, SOURCE_BACKGROUND,
    BARGE_IN_RMS_THRESHOLD_DEFAULT, should_forward_mic_audio,
)
from core import conversation as _cv
from actions import workspace_navigation
from actions import nucleus_hierarchy
# Same module objects core/headless/tool_executor.py imports (Python
# caches modules in sys.modules, so this is the identical object, not a
# second copy) — exposed under main.'s own name too so tests, and any
# future desktop-specific code, can reach them the same way the shared
# executor's tests already do (main.gmail_integration, main.biz_intel...).
from actions import gmail_integration
from actions import calendar_integration
from actions import airtable_integration
from actions import hubspot_integration
from actions import buffer_integration
from actions import buildpro_data
from actions import buildpro_matching
from actions import daily_deal_finders as ddf
from actions import google_auth
from actions import business_intelligence as biz_intel
from actions import opportunity_engine as opp_engine
from actions import decision_engine
from actions import audit_log
from actions import twilio_integration as twilio
from actions import cloud_bridge
from actions.web_search        import _news as _fetch_news_sync
from memory.config_manager     import get_brief_enabled


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

# See core/conversation.py's should_forward_mic_audio for what this is and
# why it exists — env-overridable per machine without a code change.
try:
    BARGE_IN_RMS_THRESHOLD = float(
        os.environ.get("JARVIS_BARGE_IN_RMS_THRESHOLD", "") or BARGE_IN_RMS_THRESHOLD_DEFAULT)
except ValueError:
    BARGE_IN_RMS_THRESHOLD = BARGE_IN_RMS_THRESHOLD_DEFAULT

# How much confirmed speech Gemini's own server-side VAD requires before it
# commits to "the user has started talking" (see _build_config's
# realtime_input_config). Lower is more sensitive/faster to trigger but
# more prone to false positives from a brief noise; this is Gemini's own
# tunable, not core/conversation.py's client-side RMS one — the two are
# independent, complementary layers, not duplicates of each other.
BARGE_IN_PREFIX_PADDING_MS = 100

def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JARVIS, Tony Stark's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

def _clean_transcript(text: str) -> str:    
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()

# TOOL_DECLARATIONS is the shared schema (core/headless/tool_registry.py) —
# the exact same declarations the headless/cloud service advertises to
# Gemini, so the desktop voice loop and the browser orb UI offer Gemini
# the identical tool surface, not two hand-maintained copies that can
# silently drift apart (see core/headless/tool_executor.py's module
# docstring for the matching execution-side unification). Tools in
# SESSION_ONLY_TOOLS (screen_process, close_camera, shutdown_jarvis) are
# declared here too, and _execute_tool below still handles all three
# inline, since they need this real live desktop/voice session (a camera,
# a screen, this Gemini Live session object itself). navigate_command_
# center is NOT in that set — it's handled inline just below for the
# real reason (this is where main.py's own live self._dashboard instance
# lives, not because the shared ToolExecutor can't run it: since 2026-09,
# ToolExecutor has its own navigate_command_center branch too, using a
# DashboardServer instance from ToolContext.dashboard_server, which is
# what makes it reachable from /ui's chat and dashboard_bridge.py's /3d
# relay — see core/headless/tool_executor.py's module docstring.
from core.headless.tool_registry import TOOL_DECLARATIONS

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
        self._audio_generation     = None    # conversation generation the in-flight audio belongs to
        self._out_stream           = None    # live sd.RawOutputStream, set by _play_audio — lets
                                              # interrupt() abort in-flight playback immediately
        # Single owner of the response channel (core/conversation.py). The
        # bare _is_speaking flag below stays because the mic callback reads
        # it on a hot path, but it is now a mirror of the manager's state,
        # not the source of truth. The manager is what makes cancellation
        # atomic and stale audio unplayable.
        self._conversation = ConversationManager(
            on_state_change=lambda old_state, new_state: self._broadcast_orb_state(
                new_state.lower()))
        self.ui.on_text_command   = self._on_text_command
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_interrupt      = self.interrupt
        self._turn_done_event: asyncio.Event | None = None
        self._dashboard     = None
        self._briefing_sent    = False          # morning briefing fires once per process
        self._sys_monitor      = SystemMonitor()  # persistent cooldown state
        self._proactive        = ProactiveEngine()
        self._last_user_speech = time.monotonic()  # updated on every user utterance
        self._session_log: list[str] = []          # conversation turns for end-of-session summary
        self.__tool_executor_cache = None   # built lazily — see _tool_executor property

    @property
    def _tool_executor(self) -> ToolExecutor:
        """Shared execution layer (core/headless/tool_executor.py) — every
        tool NOT in SESSION_ONLY_TOOLS runs through this exact same code
        path as the headless/cloud service, so there is one JARVIS
        execution system behind both the desktop voice loop and the
        browser orb UI, not two. Lazy + cached (rather than built once in
        __init__) so it works correctly even for a JarvisLive built via
        object.__new__ with only `.ui` set (see
        test_desktop_app_uses_the_shared_tool_executor_not_a_duplicate),
        not just via the normal constructor path.
        """
        cache = getattr(self, "_JarvisLive__tool_executor_cache", None)
        if cache is None:
            cache = ToolExecutor(ToolContext(
                ui=self.ui,
                speak=getattr(self, "speak", lambda text: None),
                proactive=getattr(self, "_proactive", None),
            ))
            self.__tool_executor_cache = cache
        return cache

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

    def set_speaking(self, value: bool):
        """Still the hot-path flag the mic callback reads, but the
        conversation manager is now the authority — so the UI can never
        show a state the turn machinery disagrees with."""
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self._conversation.set_state(_cv.SPEAKING, reason="audio playback")
            self.ui.set_state("SPEAKING")
            self._broadcast_orb_state("speaking")
        elif not self.ui.muted:
            self._conversation.to_listening("playback finished")
            self.ui.set_state("LISTENING")
            self._broadcast_orb_state("listening")
        else:
            self._conversation.to_idle("muted")

    def _broadcast_orb_state(self, state: str) -> None:
        """Cosmetic push to the /3d spatial scene reflecting JARVIS's
        current voice state (listening/thinking/speaking/interrupted) so
        the orb there mirrors the desktop app's own state — visual
        activity tied to what's actually happening, never faked. Must
        never break the actual voice loop: a safe no-op whenever there's
        no dashboard running or no event loop yet to schedule onto."""
        if getattr(self, "_dashboard", None) is None or getattr(self, "_loop", None) is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._dashboard.broadcast_nav({"type": "jarvis_state", "state": state}),
                self._loop,
            )
        except Exception:
            pass

    def interrupt(self) -> None:
        """Stop JARVIS mid-speech: drain queued audio and open mic immediately.

        barge_in() bumps the generation BEFORE the queue is drained, so a
        chunk arriving during the drain is already stale and cannot be
        replayed. The old code set a bare bool that the ending response's
        own turn_complete then cleared — which is how an interrupted answer
        could pick itself back up mid-sentence.

        Draining the queue only stops audio not yet handed to the sound
        card — up to ~200ms already dispatched to the output stream (see
        _play_audio's batching) would otherwise keep playing out to
        completion instead of being discarded immediately. abort() (never
        stop(), which waits for buffered audio to finish first) cuts that
        off at the hardware. _play_audio restarts the stream itself before
        its next write, so this never leaves playback dead for the rest of
        the session."""
        self._conversation.barge_in("user interrupted JARVIS")
        self._interrupted = True
        out_stream = getattr(self, "_out_stream", None)
        if out_stream is not None:
            try:
                out_stream.abort()
            except Exception:
                pass
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
        self.set_speaking(False)
        if self._turn_done_event:
            self._turn_done_event.clear()
        self.ui.write_log("SYS: Interrupted — listening...")
        self._broadcast_orb_state("interrupted")

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

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        # Load customization from config
        try:
            _cfg = json.loads(open(API_CONFIG_PATH, encoding="utf-8").read())
            self._asst_name = (_cfg.get("assistant_name") or "JARVIS").strip()
            _user_name = (_cfg.get("user_name") or "").strip()
        except Exception:
            self._asst_name = "JARVIS"
            _user_name = ""

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        from core.headless.obsidian import ObsidianVault
        knowledge_str = ObsidianVault().format_for_prompt(query=None, max_chars=6000)
        sys_prompt = _load_system_prompt()

        parts: list[str] = []
        parts.append(knowledge_str)

        if mem_str:
            parts.append(mem_str)

        parts.append(sys_prompt)

        config = types.LiveConnectConfig(
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": TOOL_DECLARATIONS}],
            session_resumption=types.SessionResumptionConfig(),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name="Charon"
                    )
                )
            ),
            # Gemini's own server-side voice-activity detection — the
            # existing audio stack's real echo/barge-in protection, used
            # here rather than reinvented client-side. START_OF_ACTIVITY_
            # INTERRUPTS makes the server itself stop generation and report
            # server_content.interrupted=True (handled in _receive_audio)
            # the moment it detects genuine new speech, which is a stronger
            # signal than anything derivable purely from client-side RMS.
            # LOW start-of-speech sensitivity keeps it from firing on a
            # brief noise; should_forward_mic_audio's RMS gate and
            # is_genuine_user_transcript's content check (core/
            # conversation.py) still run independently on top of this —
            # three separate, complementary layers, not one point of
            # failure.
            realtime_input_config=types.RealtimeInputConfig(
                activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
                automatic_activity_detection=types.AutomaticActivityDetection(
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
                    prefix_padding_ms=BARGE_IN_PREFIX_PADDING_MS,
                ),
            ),
        )
        return config

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})

        print(f"[JARVIS] 🔧 {name}  {args}")
        self.ui.set_state("THINKING")

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
                if self._dashboard is None:
                    result = "The command center dashboard isn't running right now — press Remote Control to start it."
                else:
                    action = (args.get("action") or "").strip().lower()
                    target = (args.get("target") or "").strip()
                    if not action:
                        action = "back" if target.lower() == "go back" else ("open" if target else "status")
                    # Every branch below reports what the navigation
                    # ACTUALLY did. apply_navigation() mutates server-side
                    # state whether or not a Command Center window is open,
                    # and broadcast_nav() used to return nothing — so with
                    # no window connected JARVIS still said "Opened
                    # BuildPro" while nothing moved on any screen. It now
                    # returns a delivered-client count, and zero is
                    # reported as the no-op it is.
                    # Voice and clicks now resolve through the SAME
                    # resolver (actions/workspace_navigation.py) and the
                    # same apply_navigation() mutator, so the two can no
                    # longer disagree about what "open BuildPro" means.
                    # The resolver also handles external pages, which used
                    # to fall out of the system entirely and get read
                    # aloud as a URL for Lee to click himself.
                    if action == "status":
                        nav = self._dashboard.apply_navigation("status", "")
                        result = f"You're currently looking at {nav['name']} in the command center."
                    else:
                        destination = workspace_navigation.resolve(
                            target, action=action, target=target)
                        delivered = await self._dashboard.execute_destination(destination)
                        result = workspace_navigation.describe(destination, delivered)

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
                    import os as _os
                    _os._exit(0)
                asyncio.create_task(_do_shutdown())

            else:
                result = await self._tool_executor.execute(name, args)

        except UnknownToolError as e:
            result = f"Unknown tool: {name}"
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

    async def _listen_audio(self):
        print("[JARVIS] 🎤 Mic started")
        loop = asyncio.get_event_loop()

        def callback(indata, frames, time_info, status):
            with self._speaking_lock:
                jarvis_speaking = self._is_speaking
            # should_forward_mic_audio (core/conversation.py): while JARVIS
            # is speaking, only audio loud enough to plausibly be a real,
            # nearby interruption is forwarded — see that function's own
            # docstring for why (no AEC available; loudness is the
            # fallback signal). RMS is only computed when actually needed
            # (jarvis_speaking), not on every callback, since it isn't free.
            rms = 0.0
            if jarvis_speaking:
                rms = float(np.sqrt(np.mean(np.square(indata.astype(np.float64)))))
            if should_forward_mic_audio(
                jarvis_speaking=jarvis_speaking, muted=self.ui.muted,
                phone_active=self._phone_active, rms=rms,
                threshold=BARGE_IN_RMS_THRESHOLD,
            ):
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
        self._audio_generation = None

        try:
            while True:
                async for response in self.session.receive():

                    if response.data:
                        # Latch the generation this response belongs to on
                        # its first chunk. After a barge-in the generation
                        # has moved on, so every remaining chunk of the old
                        # response fails this test and is dropped — even
                        # after turn_complete clears _interrupted, which is
                        # precisely when the old answer used to resume.
                        if self._audio_generation is None:
                            self._audio_generation = self._conversation.generation
                        if self._interrupted or not self._conversation.is_current(
                                self._audio_generation):
                            pass  # discard: interrupted, or belongs to a superseded turn
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

                        # Gemini's own server-side VAD (configured in
                        # _build_config's realtime_input_config) already
                        # decided this is a genuine interruption using the
                        # real audio stream server-side — the strongest
                        # signal available, and the one the SDK itself
                        # documents as "a good signal to stop and empty the
                        # current queue". Trust it directly rather than
                        # re-deriving the same decision from transcripts.
                        if sc.interrupted and not self._interrupted:
                            self.interrupt()

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            if txt and txt != (out_buf[-1] if out_buf else ""):
                                out_buf.append(txt)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            with self._speaking_lock:
                                jarvis_was_speaking = self._is_speaking
                            # should_forward_mic_audio (core/conversation.py)
                            # already judged the AUDIO loud enough to be a
                            # genuine interruption before it was even sent to
                            # Gemini — but loudness alone cannot tell a real
                            # interruption apart from JARVIS's own voice
                            # leaking back into the mic loud enough to clear
                            # that gate (a hard-surfaced room, the volume
                            # turned up). is_genuine_user_transcript adds the
                            # second, independent check this content itself
                            # can carry: reject it if it duplicates the chunk
                            # already accepted, or if it reads as an echo of
                            # what JARVIS is currently saying (out_buf).
                            # Together these are "not solely an RMS
                            # threshold," as required.
                            accepted = _cv.is_genuine_user_transcript(
                                txt,
                                recent_jarvis_text=" ".join(out_buf) if jarvis_was_speaking else "",
                                last_seen_text=in_buf[-1] if in_buf else "",
                            )
                            if accepted:
                                # Real transcribed input arrived while JARVIS was
                                # speaking. Act on it immediately rather than
                                # silently accumulating it into in_buf for a
                                # turn_complete that belongs to the response
                                # already being interrupted.
                                if jarvis_was_speaking and not self._interrupted:
                                    self.interrupt()
                                    in_buf = []
                                in_buf.append(txt)
                                self._last_user_speech = time.monotonic()

                        if sc.turn_complete:
                            self._audio_generation = None
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

    async def _play_audio(self):
        print("[JARVIS] 🔊 Play started")

        stream = sd.RawOutputStream(
            samplerate=RECEIVE_SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=CHUNK_SIZE,
        )
        stream.start()
        self._out_stream = stream   # interrupt() can now abort this stream immediately

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=0.1
                    )
                except asyncio.TimeoutError:
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                    continue

                self.set_speaking(True)

                # Batch all immediately-available chunks into one write to reduce
                # thread-pool round-trips (was one asyncio.to_thread per 50ms slice).
                # Cap at ~200 ms so interrupt() still stops audio within ~200 ms.
                batch = bytearray(chunk)
                while len(batch) < 9600:   # 9600 bytes ≈ 200 ms at 24 kHz / 16-bit mono
                    try:
                        batch.extend(self.audio_in_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                try:
                    # interrupt() may have aborted this exact stream since
                    # the last write (immediate hardware stop, so old audio
                    # is discarded rather than finishing its buffer) — abort
                    # leaves it stopped, so the next response's audio needs
                    # a fresh start() or the write below would raise.
                    if stream.stopped:
                        stream.start()
                    await asyncio.to_thread(stream.write, bytes(batch))
                except (RuntimeError, asyncio.CancelledError):
                    break   # executor shutting down — exit cleanly
        except Exception as e:
            print(f"[JARVIS] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            self._out_stream = None
            stream.stop()
            stream.close()

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
            from google import genai as _genai
            client = _genai.Client(api_key=_get_api_key())
            resp   = await asyncio.to_thread(
                client.models.generate_content,
                model="gemini-2.5-flash",
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
            # Claim the channel instead of checking whether it looks free.
            # The old shape read _is_speaking, released the lock, and only
            # then sent — so a user could start talking in that gap and get
            # a system alert dropped into the middle of their sentence.
            # try_claim() decides and takes ownership under one lock.
            if (time.monotonic() - self._last_user_speech) < 10:
                continue
            turn = self._conversation.try_claim(SOURCE_BACKGROUND, label="system alert")
            if turn is None:
                self._conversation.defer(alert, kind="system_alert")
                continue
            try:
                await self.session.send_client_content(
                    turns={"parts": [{"text": alert}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[Monitor] ⚠️ Could not send alert: {e}")
            finally:
                self._conversation.complete(turn)

    # ── Background monitor ──────────────────────────────────────────────────────

    async def _run_background_monitor(self) -> None:
        """Check user-configured topics once per day; speak alerts when new headlines appear."""
        await asyncio.sleep(300)          # wait 5 min after startup before first check
        while True:
            if self.session:
                # Same fix as the system monitor: claim, don't peek. Anything
                # refused is deferred and replayed when the user is free —
                # a topic alert is worth saying late, never worth saying over.
                recent_speech = (time.monotonic() - self._last_user_speech) < 30
                monitor_turn = (None if recent_speech else
                                self._conversation.try_claim(SOURCE_BACKGROUND,
                                                             label="topic monitor"))
                if monitor_turn is not None:
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
                    finally:
                        self._conversation.complete(monitor_turn)
            await asyncio.sleep(1800)     # check every 30 minutes

    # ── Proactive mode ──────────────────────────────────────────────────────────

    async def _run_proactive_mode(self) -> None:
        """
        Background task: periodically checks if the user has been silent long enough,
        then hands time + memory context to Gemini so it can decide what (if anything)
        to say proactively. No hardcoded rules — Gemini makes the call.
        """
        while True:
            await asyncio.sleep(60)   # evaluate once per minute

            if not self.session:
                continue

            if not self._proactive.should_trigger(self._last_user_speech):
                continue

            # Proactive speech is the lowest-priority writer of the three,
            # so it is the one that most needed to stop barging in. Refused
            # means skipped, not deferred: a check-in whose whole premise is
            # "the user has gone quiet" is void the moment they have not.
            proactive_turn = self._conversation.try_claim(SOURCE_BACKGROUND,
                                                          label="proactive check-in")
            if proactive_turn is None:
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
            finally:
                self._conversation.complete(proactive_turn)

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
            # A fresh id per reconnect attempt — register_session (core/
            # conversation.py) refuses a second live session while one is
            # already registered, which is the documented protection
            # against two live Gemini sessions listening to one microphone
            # at once (a reconnect firing before the previous session's own
            # cleanup has actually released it). Normal operation never
            # trips this — this loop only ever holds one connection open —
            # but it is real, tested protection rather than an assumption.
            session_id = uuid.uuid4().hex
            try:
                print("[JARVIS] Connecting...")
                self.ui.set_state("THINKING")
                config = self._build_config()

                if not self._conversation.register_session(session_id):
                    print("[JARVIS] A live session is still registered — waiting before reconnecting.")
                    await asyncio.sleep(getattr(self, "_conn_backoff", 3))
                    continue

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
                err_str = str(e)
                print(f"[JARVIS] Error ({type(e).__name__}): {e}")
                traceback.print_exc()

                # Invalid API key — stop hammering the API, prompt re-configuration
                if "API key not valid" in err_str or "1007" in err_str:
                    self.ui.write_log("ERR: API key invalid — please re-enter your key.")
                    self.ui.set_state("SLEEPING")
                    self.ui.prompt_reconfig()
                    while not self.ui._win._ready:
                        await asyncio.sleep(1)
                    print("[JARVIS] New API key saved — reconnecting...")
                    _conn_backoff = 3
                    continue

                # Network / timeout errors — log clearly and back off
                is_net_err = any(k in err_str for k in (
                    "TimeoutError", "timed out", "getaddrinfo", "CancelledError",
                    "ConnectionRefusedError", "OSError", "Cannot connect",
                ))
                if is_net_err:
                    _conn_backoff = min(getattr(self, "_conn_backoff", 3) * 2, 60)
                    self._conn_backoff = _conn_backoff
                    self.ui.write_log(
                        f"NET: Bağlantı kurulamadı — {_conn_backoff}s sonra tekrar deneniyor. "
                        "(VPN gerekiyor olabilir)"
                    )
                else:
                    self._conn_backoff = 3
            finally:
                self.session = None
                self._conversation.release_session(session_id)
                # Only save if there was a real conversation (≥3 turns)
                if len(self._session_log) >= 3:
                    asyncio.create_task(self._save_session_summary())

            self.set_speaking(False)
            self.ui.set_state("SLEEPING")

            if self._dashboard:
                await self._dashboard.broadcast({"type": "status", "state": "sleeping"})

            delay = getattr(self, "_conn_backoff", 3)
            print(f"[JARVIS] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)

def main():
    ui = JarvisUI("face.png")

    def runner():
        ui.wait_for_api_key()
        jarvis = JarvisLive(ui)
        try:
            asyncio.run(jarvis.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()

if __name__ == "__main__":
    main()