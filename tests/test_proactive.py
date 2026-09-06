"""actions/proactive.py — trigger gating (enabled flag, quiet hours,
snooze, silence/cooldown), the persistent activity trail, and prompt
building. No prior test coverage existed for this module at all.
"""
import time
from datetime import datetime

import pytest

from actions import proactive


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch, tmp_path):
    monkeypatch.setattr(proactive, "DB_PATH", tmp_path / "test_proactive.db")


# ── _in_quiet_hours ──────────────────────────────────────────────────────

def test_in_quiet_hours_simple_window():
    assert proactive._in_quiet_hours(datetime(2026, 1, 1, 23, 0), 22, 8) is True
    assert proactive._in_quiet_hours(datetime(2026, 1, 1, 3, 0), 22, 8) is True
    assert proactive._in_quiet_hours(datetime(2026, 1, 1, 7, 59), 22, 8) is True


def test_in_quiet_hours_outside_window():
    assert proactive._in_quiet_hours(datetime(2026, 1, 1, 8, 0), 22, 8) is False
    assert proactive._in_quiet_hours(datetime(2026, 1, 1, 12, 0), 22, 8) is False
    assert proactive._in_quiet_hours(datetime(2026, 1, 1, 21, 59), 22, 8) is False


def test_in_quiet_hours_non_wrapping_window():
    # e.g. a midday quiet window, start < end, no midnight wrap
    assert proactive._in_quiet_hours(datetime(2026, 1, 1, 13, 0), 12, 14) is True
    assert proactive._in_quiet_hours(datetime(2026, 1, 1, 15, 0), 12, 14) is False


def test_in_quiet_hours_zero_width_window_is_never_quiet():
    assert proactive._in_quiet_hours(datetime(2026, 1, 1, 22, 0), 22, 22) is False


# ── should_trigger: enabled flag ─────────────────────────────────────────

def test_should_trigger_false_when_disabled_even_if_otherwise_eligible():
    engine = proactive.ProactiveEngine(min_silence_secs=0, check_cooldown=0)
    long_ago = time.monotonic() - 10_000
    assert engine.should_trigger(long_ago, enabled=False) is False


def test_should_trigger_true_when_enabled_and_eligible():
    engine = proactive.ProactiveEngine(min_silence_secs=0, check_cooldown=0)
    long_ago = time.monotonic() - 10_000
    assert engine.should_trigger(long_ago, enabled=True) is True


# ── should_trigger: quiet hours ──────────────────────────────────────────

def test_should_trigger_false_during_quiet_hours(monkeypatch):
    engine = proactive.ProactiveEngine(min_silence_secs=0, check_cooldown=0)
    long_ago = time.monotonic() - 10_000

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, 23, 0)

    monkeypatch.setattr(proactive, "datetime", _FixedDatetime)
    assert engine.should_trigger(long_ago, enabled=True, quiet_hours=(22, 8)) is False


def test_should_trigger_true_outside_quiet_hours(monkeypatch):
    engine = proactive.ProactiveEngine(min_silence_secs=0, check_cooldown=0)
    long_ago = time.monotonic() - 10_000

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, 14, 0)

    monkeypatch.setattr(proactive, "datetime", _FixedDatetime)
    assert engine.should_trigger(long_ago, enabled=True, quiet_hours=(22, 8)) is True


def test_should_trigger_ignores_quiet_hours_when_none():
    engine = proactive.ProactiveEngine(min_silence_secs=0, check_cooldown=0)
    long_ago = time.monotonic() - 10_000
    assert engine.should_trigger(long_ago, enabled=True, quiet_hours=None) is True


# ── should_trigger: snooze ────────────────────────────────────────────────

def test_snooze_blocks_triggering_until_it_elapses():
    engine = proactive.ProactiveEngine(min_silence_secs=0, check_cooldown=0)
    long_ago = time.monotonic() - 10_000

    engine.snooze(3600)
    assert engine.is_snoozed() is True
    assert engine.should_trigger(long_ago) is False
    assert engine.snoozed_remaining_secs() > 3500


def test_snooze_zero_or_negative_does_not_block():
    engine = proactive.ProactiveEngine(min_silence_secs=0, check_cooldown=0)
    long_ago = time.monotonic() - 10_000
    engine.snooze(-5)
    assert engine.is_snoozed() is False
    assert engine.should_trigger(long_ago) is True


def test_snooze_expires_naturally(monkeypatch):
    engine = proactive.ProactiveEngine(min_silence_secs=0, check_cooldown=0)
    engine.snooze(10)
    # simulate time passing past the snooze window — capture the real
    # monotonic() first since proactive.time IS the same time module
    # (patching proactive.time.monotonic also patches time.monotonic
    # globally; calling time.monotonic() again inside the lambda would
    # recurse into the patched version itself).
    real_monotonic = time.monotonic
    monkeypatch.setattr(proactive.time, "monotonic", lambda: real_monotonic() + 20)
    assert engine.is_snoozed() is False


# ── should_trigger: existing silence/cooldown gates still work ──────────

def test_should_trigger_false_before_min_silence():
    engine = proactive.ProactiveEngine(min_silence_secs=900, check_cooldown=0)
    just_now = time.monotonic()
    assert engine.should_trigger(just_now) is False


def test_should_trigger_false_during_cooldown_after_a_trigger():
    engine = proactive.ProactiveEngine(min_silence_secs=0, check_cooldown=1200)
    long_ago = time.monotonic() - 10_000
    engine.mark_triggered()
    assert engine.should_trigger(long_ago) is False


# ── mark_triggered: persistent activity trail ────────────────────────────

def test_mark_triggered_records_a_trail_entry():
    engine = proactive.ProactiveEngine()
    engine.mark_triggered()
    history = proactive.get_recent_triggers()
    assert len(history) == 1
    # _rotation increments to 1 BEFORE the label is computed (pre-existing
    # behavior, unchanged by this prompt — build_prompt() reads the same
    # already-incremented value right after, so the logged label always
    # matches what that trigger's actual prompt used) -> index 1, not 0.
    assert history[0]["focus_area"] == "wellbeing_checkin"


def test_mark_triggered_rotates_focus_label_across_calls():
    engine = proactive.ProactiveEngine()
    engine.mark_triggered()
    engine.mark_triggered()
    engine.mark_triggered()
    history = proactive.get_recent_triggers()
    labels = [h["focus_area"] for h in reversed(history)]   # oldest first
    assert labels == ["wellbeing_checkin", "general_interest", "projects_or_goals"]


def test_get_recent_triggers_orders_newest_first_and_respects_limit():
    engine = proactive.ProactiveEngine()
    for _ in range(5):
        engine.mark_triggered()
        time.sleep(0.001)
    history = proactive.get_recent_triggers(limit=2)
    assert len(history) == 2
    assert history[0]["triggered_ts"] >= history[1]["triggered_ts"]


def test_get_recent_triggers_empty_when_nothing_logged():
    assert proactive.get_recent_triggers() == []


def test_trail_logging_failure_does_not_break_mark_triggered(monkeypatch, tmp_path):
    # A genuinely inaccessible DB path (a directory, not a file) must not
    # crash mark_triggered() — the real safety property _record_trigger's
    # own try/except provides.
    bogus = tmp_path / "not_a_file.db"
    bogus.mkdir()
    monkeypatch.setattr(proactive, "DB_PATH", bogus)

    engine = proactive.ProactiveEngine()
    engine.mark_triggered()   # must not raise
    assert engine._rotation == 1


# ── build_prompt: still produces a valid prompt with the new instruction ──

def test_build_prompt_includes_no_repeat_instruction():
    engine = proactive.ProactiveEngine()
    prompt = engine.build_prompt(memory={})
    assert "Do NOT repeat a topic" in prompt


def test_build_prompt_never_calls_tools_instruction_present():
    engine = proactive.ProactiveEngine()
    prompt = engine.build_prompt(memory={})
    assert "Do NOT call any tools" in prompt
