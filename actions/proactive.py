"""
ProactiveEngine 2.0 — context-aware, time-aware, non-repetitive background prompting.
Gemini decides what to say; this module decides WHEN and builds a rich context snapshot.

Controls added for safe, boundable proactive behavior:
  - enabled flag        — main.py reads memory.config_manager.get_proactive_enabled()
                           fresh each check and passes it in; see docs/PROACTIVE_ENGINE.md
  - quiet hours          — no trigger during a configurable local-time window
  - snooze               — a temporary "not now" the user can invoke via voice
  - persistent activity trail — every trigger is recorded to data/jarvis2.db
                           (proactive_log table) so there's a real, queryable
                           record of when/why JARVIS spoke up unprompted

Only ever produces a text prompt handed to the existing Gemini Live session —
never sends a message, publishes content, or touches an external system
itself. build_prompt() also explicitly instructs Gemini not to call any
tools on a proactive turn (a soft, prompt-level constraint — the hard
guarantee comes from every consequential tool in this codebase already
requiring its own explicit-instruction/approved gate independently, see
docs/PROACTIVE_ENGINE.md for why a hard per-turn tool block isn't feasible
with a session-level, not per-turn, Gemini Live tool configuration).
"""
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from core.headless.config import DATA_DIR

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = DATA_DIR / "jarvis2.db"

_FOCUS_LABELS = ["projects_or_goals", "wellbeing_checkin", "general_interest"]


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS proactive_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            triggered_ts REAL NOT NULL,
            focus_area TEXT
        )
    """)
    return conn


def _record_trigger(focus_area: str) -> None:
    """Best-effort persistent activity trail — a logging failure must
    never block a real proactive check-in from happening."""
    try:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO proactive_log (triggered_ts, focus_area) VALUES (?, ?)",
                (time.time(), focus_area),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def get_recent_triggers(limit: int = 20) -> list[dict[str, Any]]:
    """Read-only: the activity trail, most recent first. Never raises —
    an unreadable log degrades to an empty list."""
    try:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM proactive_log ORDER BY triggered_ts DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception:
        return []


def _in_quiet_hours(now: datetime, start_hour: int, end_hour: int) -> bool:
    """True if `now`'s local hour falls in [start_hour, end_hour), wrapping
    midnight when start > end (e.g. 22 -> 8 means 22,23,0..7 are quiet)."""
    hour = now.hour
    if start_hour == end_hour:
        return False   # zero-width window — treat as "no quiet hours"
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


class ProactiveEngine:
    """
    Decides when JARVIS should speak unprompted and builds a context-rich prompt.

    Improvements over 1.0:
      - Time-of-day awareness  (morning / afternoon / evening / night)
      - Monitor-topic awareness (what the user is tracking)
      - Recent-session context  (last few turns of the current conversation)
      - Non-repetitive          (rotates context focus to avoid same opener)
      - Smarter silence gate    (doesn't fire while JARVIS is speaking)

    Defaults:
      min_silence_secs  — 900 s  (15 min) user must be silent before any check
      check_cooldown    — 1200 s (20 min) minimum gap between proactive messages
    """

    def __init__(
        self,
        min_silence_secs: int = 900,
        check_cooldown:   int = 1200,
    ):
        self.min_silence_secs = min_silence_secs
        self.check_cooldown   = check_cooldown
        self._last_triggered  = 0.0
        self._rotation        = 0          # cycles through context focus areas
        self._snoozed_until   = 0.0        # time.monotonic() timestamp; 0 = not snoozed

    # ── Trigger gate ───────────────────────────────────────────────────────────

    def should_trigger(
        self,
        last_user_speech: float,
        enabled: bool = True,
        quiet_hours: tuple[int, int] | None = None,
    ) -> bool:
        if not enabled:
            return False
        now = time.monotonic()
        if now < self._snoozed_until:
            return False
        if quiet_hours and _in_quiet_hours(datetime.now(), quiet_hours[0], quiet_hours[1]):
            return False
        return (
            (now - last_user_speech) >= self.min_silence_secs
            and (now - self._last_triggered) >= self.check_cooldown
        )

    def mark_triggered(self) -> None:
        self._last_triggered = time.monotonic()
        self._rotation      += 1
        _record_trigger(self._focus_label())

    def snooze(self, duration_secs: float) -> None:
        """User-invoked 'not now' — should_trigger() returns False until
        this many seconds from now have passed, regardless of the normal
        silence/cooldown gates."""
        self._snoozed_until = time.monotonic() + max(0.0, duration_secs)

    def is_snoozed(self) -> bool:
        return time.monotonic() < self._snoozed_until

    def snoozed_remaining_secs(self) -> float:
        return max(0.0, self._snoozed_until - time.monotonic())

    def _focus_label(self) -> str:
        return _FOCUS_LABELS[self._rotation % 3]

    # ── Prompt builder ─────────────────────────────────────────────────────────

    def build_prompt(
        self,
        memory:       dict,
        monitors:     list[str] | None = None,
        recent_turns: list[str] | None = None,
    ) -> str:
        """
        Build a context snapshot for Gemini.
        Rotates through three focus areas so proactive messages don't repeat.
        """
        from memory.memory_manager import format_memory_for_prompt

        now      = datetime.now()
        hour     = now.hour
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")

        # Time-of-day label
        if   6  <= hour < 12:  period = "morning"
        elif 12 <= hour < 18:  period = "afternoon"
        elif 18 <= hour < 23:  period = "evening"
        else:                  period = "late night"

        mem_str = format_memory_for_prompt(memory) or "(no stored user data)"

        # Rotating context focus (cycles every trigger)
        focus_index = self._rotation % 3
        if focus_index == 0:
            focus = (
                "Focus on the user's active projects or goals if any are stored. "
                "Ask how something is going, or offer a relevant tip."
            )
        elif focus_index == 1:
            focus = (
                "Focus on the time of day and the user's wellbeing. "
                "A warm check-in, a reminder to take a break, or something timely."
            )
        else:
            focus = (
                "Focus on something genuinely interesting or useful — "
                "a fact, a suggestion, or a question based on what you know about this person."
            )

        # Optional: monitored topics context
        monitor_ctx = ""
        if monitors:
            monitor_ctx = (
                f"\nThe user tracks these topics: {', '.join(monitors[:4])}. "
                "You may mention one if it seems relevant."
            )

        # Optional: recent conversation context
        recent_ctx = ""
        if recent_turns:
            snippet = "\n".join(recent_turns[-6:])
            recent_ctx = f"\nRecent conversation:\n{snippet}"

        return "\n".join([
            "[PROACTIVE_CHECK] You are initiating a proactive check-in.",
            f"Current time : {time_str}  ({period})",
            "",
            "Context about this person:",
            mem_str,
            monitor_ctx,
            recent_ctx,
            "",
            "Task:",
            focus,
            "",
            "Rules:",
            "- Speak in the user's language (check memory; default English).",
            "- 1-2 sentences max. Natural, warm, never robotic.",
            "- Do NOT mention [PROACTIVE_CHECK] or these instructions.",
            "- Do NOT call any tools.",
            "- Do NOT repeat a topic/theme already covered in the recent conversation above.",
            "- If nothing genuinely useful comes to mind, stay silent (say nothing).",
        ])
