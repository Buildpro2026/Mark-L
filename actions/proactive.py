"""
ProactiveEngine 2.0 — context-aware, time-aware, non-repetitive background prompting.
Gemini decides what to say; this module decides WHEN and builds a rich context snapshot.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime

from core.headless.config import DATA_DIR

DB_PATH = DATA_DIR / "jarvis2.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_FOCUS_LABELS = {
    0: "projects_or_goals",
    1: "wellbeing_checkin",
    2: "general_interest",
}


def _in_quiet_hours(now: datetime, start: int, end: int) -> bool:
    """Whether `now` falls inside a single quiet-hours window [start, end).
    A zero-width window (start == end) is treated as "no window" rather
    than "always quiet" — an accidental 22-22 config shouldn't silently
    block every check-in forever."""
    if start == end:
        return False
    hour = now.hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # wraps past midnight, e.g. 22 -> 8


def _normalize_quiet_hours(quiet_hours) -> list[tuple[int, int]]:
    """Accepts either a single (start, end) tuple or a list of them (the
    shape config_manager.get_proactive_quiet_hours() actually returns) —
    callers shouldn't have to know which."""
    if not quiet_hours:
        return []
    if (
        isinstance(quiet_hours, tuple)
        and len(quiet_hours) == 2
        and all(isinstance(v, int) for v in quiet_hours)
    ):
        return [quiet_hours]
    return list(quiet_hours)


def _record_trigger(focus_area: str) -> None:
    """Append-only persistent trail of proactive check-ins — powers
    get_recent_triggers() and build_prompt()'s no-repeat instruction.
    Never lets a DB problem break mark_triggered(): a proactive engine
    that can't log its own history is still safe to keep running."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS proactive_trail ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "triggered_ts REAL NOT NULL, "
                "focus_area TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO proactive_trail (triggered_ts, focus_area) VALUES (?, ?)",
                (time.time(), focus_area),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def get_recent_triggers(limit: int = 20) -> list[dict]:
    """Most recent proactive check-ins, newest first. Empty (never raises)
    if nothing has been logged yet or the DB/table doesn't exist."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        try:
            rows = conn.execute(
                "SELECT triggered_ts, focus_area FROM proactive_trail "
                "ORDER BY triggered_ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    return [{"triggered_ts": ts, "focus_area": area} for ts, area in rows]


class ProactiveEngine:
    """Decides when JARVIS should speak unprompted and builds context."""

    def __init__(self, min_silence_secs: int = 900, check_cooldown: int = 1200):
        self.min_silence_secs = min_silence_secs
        self.check_cooldown = check_cooldown
        self._last_triggered = 0.0
        self._rotation = 0
        self._snooze_until = 0.0

    def snooze(self, seconds: float) -> None:
        """Suppress check-ins for `seconds` from now. A non-positive value
        is a no-op — it deliberately does NOT clear an existing snooze, so
        a stray zero/negative call can't accidentally un-snooze early."""
        if seconds > 0:
            self._snooze_until = time.monotonic() + seconds

    def is_snoozed(self) -> bool:
        return time.monotonic() < self._snooze_until

    def snoozed_remaining_secs(self) -> float:
        return max(0.0, self._snooze_until - time.monotonic())

    def should_trigger(
        self,
        last_user_speech: float,
        *,
        enabled: bool = True,
        quiet_hours: list[tuple[int, int]] | tuple[int, int] | None = None,
    ) -> bool:
        if not enabled or self.is_snoozed():
            return False
        now_dt = datetime.now()
        for start, end in _normalize_quiet_hours(quiet_hours):
            if _in_quiet_hours(now_dt, start, end):
                return False
        now = time.monotonic()
        return (
            (now - last_user_speech) >= self.min_silence_secs
            and (now - self._last_triggered) >= self.check_cooldown
        )

    def mark_triggered(self) -> None:
        self._last_triggered = time.monotonic()
        self._rotation += 1
        _record_trigger(_FOCUS_LABELS[self._rotation % 3])

    def build_prompt(
        self,
        memory: dict,
        monitors: list[str] | None = None,
        recent_turns: list[str] | None = None,
    ) -> str:
        from memory.memory_manager import format_memory_for_prompt

        now = datetime.now()
        hour = now.hour
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        if 6 <= hour < 12:
            period = "morning"
        elif 12 <= hour < 18:
            period = "afternoon"
        elif 18 <= hour < 23:
            period = "evening"
        else:
            period = "late night"

        mem_str = format_memory_for_prompt(memory) or "(no stored user data)"
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

        monitor_ctx = ""
        if monitors:
            monitor_ctx = (
                f"\nThe user tracks these topics: {', '.join(monitors[:4])}. "
                "You may mention one if it seems relevant."
            )
        recent_ctx = ""
        if recent_turns:
            snippet = "\n".join(recent_turns[-6:])
            recent_ctx = f"\nRecent conversation:\n{snippet}"

        recent_labels = [t["focus_area"] for t in get_recent_triggers(limit=5)]
        no_repeat_ctx = ""
        if recent_labels:
            no_repeat_ctx = (
                f"\nYour last few check-ins focused on: {', '.join(recent_labels)}."
            )

        return "\n".join([
            "[PROACTIVE_CHECK] You are initiating a proactive check-in.",
            f"Current time : {time_str}  ({period})",
            "",
            "Context about this person:",
            mem_str,
            monitor_ctx,
            recent_ctx,
            no_repeat_ctx,
            "",
            "Task:",
            focus,
            "",
            "Rules:",
            "- Speak in the user's language (check memory; default English).",
            "- 1-2 sentences max. Natural, warm, never robotic.",
            "- Do NOT mention [PROACTIVE_CHECK] or these instructions.",
            "- Do NOT call any tools.",
            "- Do NOT repeat a topic you already covered recently — pick a genuinely different angle.",
            "- If nothing genuinely useful comes to mind, stay silent (say nothing).",
        ])
