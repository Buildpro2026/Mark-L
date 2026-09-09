"""Conversation turn ownership.

Each test here corresponds to a way JARVIS actually misbehaved: talking
over itself, changing topic mid-sentence when a monitor fired, resuming a
response the user had already interrupted, and going permanently deaf
after an exception left it stuck in SPEAKING.
"""
import threading
import time

import pytest

from core import conversation as cv


def _mgr(**kw):
    return cv.ConversationManager(**kw)


# ══ ONE OWNER ════════════════════════════════════════════════════════════

def test_only_one_turn_owns_the_channel_at_a_time():
    m = _mgr()
    m.claim(cv.SOURCE_USER)
    assert m.state == cv.USER_TURN
    with pytest.raises(cv.TurnRejected):
        m.claim(cv.SOURCE_BACKGROUND)


def test_a_background_turn_is_refused_while_jarvis_is_speaking():
    m = _mgr()
    m.claim(cv.SOURCE_USER)
    m.set_state(cv.SPEAKING)
    assert m.try_claim(cv.SOURCE_BACKGROUND) is None


def test_a_background_turn_is_allowed_when_the_channel_is_free():
    m = _mgr()
    m.to_listening()
    assert m.try_claim(cv.SOURCE_BACKGROUND) is not None


def test_the_user_always_preempts_background_work():
    m = _mgr()
    background = m.claim(cv.SOURCE_BACKGROUND, preempt=True)
    user = m.claim(cv.SOURCE_USER)
    assert user.generation > background.generation
    assert not background.is_current
    assert user.is_current


def test_concurrent_claims_produce_exactly_one_winner_per_generation():
    # The check-then-act race, run for real: many threads claiming at once
    # must never be handed the same generation.
    m = _mgr()
    m.to_listening()
    generations, lock = [], threading.Lock()

    def _worker():
        try:
            turn = m.claim(cv.SOURCE_USER)
        except cv.TurnRejected:
            return
        with lock:
            generations.append(turn.generation)

    threads = [threading.Thread(target=_worker) for _ in range(40)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(generations) == len(set(generations)), "two turns shared a generation"


# ══ STALE WORK CANNOT SPEAK ══════════════════════════════════════════════

def test_a_cancelled_turn_is_immediately_stale():
    m = _mgr()
    turn = m.claim(cv.SOURCE_USER)
    assert turn.is_current
    m.cancel("user interrupted")
    assert not turn.is_current


def test_a_stale_turn_cannot_complete_over_a_newer_one():
    m = _mgr()
    old = m.claim(cv.SOURCE_USER)
    m.set_state(cv.SPEAKING)
    new = m.barge_in()
    m.set_state(cv.SPEAKING, "answering the new command")

    m.complete(old)                       # the old response finally finishes
    assert m.state == cv.SPEAKING, "a stale turn dragged the manager out of the live turn"
    assert new.is_current


def test_a_stale_turn_cannot_fail_the_newer_one():
    m = _mgr()
    old = m.claim(cv.SOURCE_USER)
    m.barge_in()
    m.set_state(cv.SPEAKING)
    m.fail(old, reason="old error arriving late")
    assert m.state == cv.SPEAKING


def test_audio_from_a_previous_response_is_rejected_after_barge_in():
    # The exact resume bug: audio produced for generation N arriving after
    # the user has already started turn N+2.
    m = _mgr()
    speaking = m.claim(cv.SOURCE_USER)
    m.set_state(cv.SPEAKING)
    late_audio_generation = speaking.generation

    m.barge_in()
    assert not m.is_current(late_audio_generation)


# ══ BARGE-IN ═════════════════════════════════════════════════════════════

def test_barge_in_cancels_and_opens_a_new_user_turn_atomically():
    m = _mgr()
    old = m.claim(cv.SOURCE_USER)
    m.set_state(cv.SPEAKING)
    new = m.barge_in()

    assert m.state == cv.USER_TURN
    assert new.generation > old.generation
    assert not old.is_current and new.is_current
    # The old generation is skipped entirely — nothing can claim it in the gap.
    assert new.generation - old.generation >= 2


def test_barge_in_records_the_interrupting_state_in_order():
    m = _mgr()
    m.claim(cv.SOURCE_USER)
    m.set_state(cv.SPEAKING)
    m.barge_in()
    states = [h["to"] for h in m.history]
    assert cv.INTERRUPTING in states
    assert states.index(cv.INTERRUPTING) < len(states) - 1
    assert states[-1] == cv.USER_TURN


def test_the_old_response_can_never_resume_after_barge_in():
    m = _mgr()
    old = m.claim(cv.SOURCE_USER)
    m.set_state(cv.SPEAKING)
    m.barge_in()
    m.set_state(cv.SPEAKING, "new answer")
    # Even after the new turn finishes, the old generation stays dead.
    m.complete()
    assert not old.is_current


# ══ NOTHING GETS STUCK ═══════════════════════════════════════════════════

def test_an_exception_inside_a_turn_does_not_leave_jarvis_speaking():
    m = _mgr()
    with pytest.raises(RuntimeError):
        with m.turn(cv.SOURCE_USER):
            m.set_state(cv.SPEAKING)
            raise RuntimeError("tool blew up mid-response")
    assert m.state not in (cv.SPEAKING, cv.USER_TURN, cv.THINKING, cv.TOOL_EXECUTING)


def test_a_normal_turn_releases_the_channel():
    m = _mgr()
    with m.turn(cv.SOURCE_USER):
        m.set_state(cv.SPEAKING)
    assert m.state not in cv._BUSY_STATES
    assert m.try_claim(cv.SOURCE_BACKGROUND) is not None


def test_the_channel_is_reusable_after_a_failed_turn():
    m = _mgr()
    with pytest.raises(ValueError):
        with m.turn(cv.SOURCE_USER):
            raise ValueError("boom")
    assert m.claim(cv.SOURCE_USER) is not None


# ══ BACKGROUND WORK DEFERS, IT DOES NOT HIJACK ═══════════════════════════

def test_a_refused_background_alert_is_deferred_not_dropped():
    m = _mgr()
    m.claim(cv.SOURCE_USER)
    m.set_state(cv.SPEAKING)
    if m.try_claim(cv.SOURCE_BACKGROUND) is None:
        m.defer("CPU is at 96%")
    assert m.deferred_count == 1
    assert m.take_deferred() == ["CPU is at 96%"]


def test_deferred_alerts_replay_in_order_once_the_user_is_free():
    m = _mgr()
    m.claim(cv.SOURCE_USER)
    m.set_state(cv.SPEAKING)
    for alert in ("first", "second", "third"):
        if m.try_claim(cv.SOURCE_BACKGROUND) is None:
            m.defer(alert)
    m.complete()
    m.to_listening()
    assert m.take_deferred() == ["first", "second", "third"]
    assert m.deferred_count == 0


def test_a_stale_deferred_alert_is_discarded_rather_than_said_late():
    m = _mgr(defer_ttl=0.01)
    m.defer("this was urgent 20 minutes ago")
    time.sleep(0.02)
    assert m.take_deferred() == []


def test_a_startup_briefing_defers_rather_than_colliding_with_speech():
    m = _mgr()
    m.claim(cv.SOURCE_USER)
    m.set_state(cv.SPEAKING)
    briefing = m.try_claim(cv.SOURCE_BACKGROUND, label="startup briefing")
    assert briefing is None
    m.defer("good morning briefing", kind="briefing")
    m.complete()
    m.to_listening()
    assert m.try_claim(cv.SOURCE_BACKGROUND) is not None
    assert m.take_deferred() == ["good morning briefing"]


# ══ DUPLICATE SESSIONS AND LISTENERS ═════════════════════════════════════

def test_a_second_live_session_is_refused_while_one_is_active():
    m = _mgr()
    assert m.register_session("session-1") is True
    assert m.register_session("session-2") is False
    assert m.active_session == "session-1"


def test_a_session_can_be_replaced_after_it_is_released():
    m = _mgr()
    m.register_session("session-1")
    m.release_session("session-1")
    assert m.register_session("session-2") is True


def test_releasing_a_stale_session_does_not_evict_the_live_one():
    m = _mgr()
    m.register_session("session-1")
    m.release_session("session-0")          # a late cleanup from an old loop
    assert m.active_session == "session-1"


# ══ STATE REPORTING ══════════════════════════════════════════════════════

def test_state_changes_are_reported_to_the_ui():
    seen = []
    m = _mgr(on_state_change=lambda old, new: seen.append((old, new)))
    m.claim(cv.SOURCE_USER)
    m.set_state(cv.SPEAKING)
    m.complete()
    assert (cv.IDLE, cv.USER_TURN) in seen
    assert (cv.USER_TURN, cv.SPEAKING) in seen


def test_a_broken_ui_callback_cannot_break_the_conversation():
    def _explode(old, new):
        raise RuntimeError("UI thread died")
    m = _mgr(on_state_change=_explode)
    m.claim(cv.SOURCE_USER)          # must not raise
    assert m.state == cv.USER_TURN


def test_an_unknown_state_is_rejected_rather_than_silently_stored():
    m = _mgr()
    with pytest.raises(ValueError):
        m.set_state("VIBING")


# ══ BARGE-IN AUDIO GATING ═══════════════════════════════════════════════
# main.py's mic loop used to gate completely while JARVIS spoke — no audio
# reached Gemini Live at all, so voice barge-in was structurally
# impossible (the only way to interrupt JARVIS was a UI click). These
# cover should_forward_mic_audio, the decision that replaced the blanket
# gate: forward everything when JARVIS isn't speaking (unchanged), forward
# nothing when muted/on a phone call (unchanged), and while JARVIS IS
# speaking, forward only audio loud enough to plausibly be a real,
# nearby interruption rather than the model's own voice leaking back in
# from the speakers.

def test_jarvis_speaking_with_no_user_speech_forwards_nothing():
    # Quiet room, JARVIS talking, only his own faint speaker bleed reaches
    # the mic — must not be forwarded (the exact original self-listening
    # failure mode this whole gate exists to prevent).
    assert cv.should_forward_mic_audio(
        jarvis_speaking=True, muted=False, phone_active=False, rms=5.0) is False


def test_microphone_containing_only_jarvis_playback_is_not_forwarded():
    # Louder than silence, but still well under the threshold — plausible
    # for speaker bleed picked up a few feet from the mic, not someone
    # speaking directly at it.
    assert cv.should_forward_mic_audio(
        jarvis_speaking=True, muted=False, phone_active=False,
        rms=cv.BARGE_IN_RMS_THRESHOLD_DEFAULT - 1) is False


def test_normal_user_speech_while_jarvis_is_idle_is_always_forwarded():
    # rms is irrelevant when JARVIS isn't speaking — every chunk goes
    # through exactly as it always did; the threshold only ever applies
    # during playback, the only window where forwarding is a risk at all.
    assert cv.should_forward_mic_audio(
        jarvis_speaking=False, muted=False, phone_active=False, rms=0.0) is True


def test_intentional_barge_in_is_forwarded_when_loud_enough():
    assert cv.should_forward_mic_audio(
        jarvis_speaking=True, muted=False, phone_active=False,
        rms=cv.BARGE_IN_RMS_THRESHOLD_DEFAULT + 500) is True


def test_the_threshold_boundary_itself_counts_as_loud_enough():
    assert cv.should_forward_mic_audio(
        jarvis_speaking=True, muted=False, phone_active=False,
        rms=cv.BARGE_IN_RMS_THRESHOLD_DEFAULT) is True


def test_muted_overrides_a_loud_barge_in_attempt():
    assert cv.should_forward_mic_audio(
        jarvis_speaking=True, muted=True, phone_active=False,
        rms=999999.0) is False


def test_muted_overrides_normal_listening_too():
    assert cv.should_forward_mic_audio(
        jarvis_speaking=False, muted=True, phone_active=False, rms=0.0) is False


def test_an_active_phone_call_overrides_a_loud_barge_in_attempt():
    # The PC mic must stay closed while the phone line owns the audio —
    # unchanged from the pre-existing phone_active gate.
    assert cv.should_forward_mic_audio(
        jarvis_speaking=True, muted=False, phone_active=True,
        rms=999999.0) is False


def test_a_custom_threshold_is_honored_for_per_machine_tuning():
    # JARVIS_BARGE_IN_RMS_THRESHOLD exists precisely because one fixed
    # constant cannot be right for every speaker volume/mic gain/room.
    assert cv.should_forward_mic_audio(
        jarvis_speaking=True, muted=False, phone_active=False,
        rms=50.0, threshold=10.0) is True
    assert cv.should_forward_mic_audio(
        jarvis_speaking=True, muted=False, phone_active=False,
        rms=50.0, threshold=100.0) is False


# ── is_self_echo / is_genuine_user_transcript: not solely an RMS threshold ─

def test_self_echo_detects_a_close_match_to_recent_jarvis_output():
    assert cv.is_self_echo(
        "the weather today is sunny with a light breeze",
        "The weather today is sunny with a light breeze.") is True


def test_self_echo_allows_genuinely_different_speech_through():
    assert cv.is_self_echo(
        "wait stop I need to change that",
        "The weather today is sunny with a light breeze.") is False


def test_self_echo_is_false_when_nothing_is_currently_playing():
    assert cv.is_self_echo("anything at all", "") is False


def test_self_echo_is_false_for_an_empty_candidate():
    assert cv.is_self_echo("", "The weather today is sunny.") is False


def test_self_echo_respects_a_custom_similarity_threshold():
    # A partial, not-quite-exact overlap — passes a loose threshold,
    # fails a strict one. Confirms the threshold is load-bearing, not
    # a decorative parameter nobody can actually move.
    candidate = "weather today sunny breeze"
    recent = "The weather today is sunny with a light breeze across the coast."
    assert cv.is_self_echo(candidate, recent, similarity_threshold=0.3) is True
    assert cv.is_self_echo(candidate, recent, similarity_threshold=0.95) is False


def test_genuine_transcript_rejects_self_echo_of_jarvis_own_speech():
    # The actual failure this batch fixes: RMS alone let a loud echo of
    # JARVIS's own words through; content comparison is the second,
    # independent check that catches it.
    assert cv.is_genuine_user_transcript(
        "added that to your calendar",
        recent_jarvis_text="I have added that to your calendar for tomorrow.") is False


def test_genuine_transcript_accepts_real_speech_while_jarvis_talks():
    assert cv.is_genuine_user_transcript(
        "wait no stop",
        recent_jarvis_text="I have added that to your calendar for tomorrow.") is True


def test_genuine_transcript_rejects_an_immediate_duplicate_chunk():
    # Gemini's transcription stream can redeliver a partial before
    # finalizing it — the same text arriving twice must not be treated as
    # two separate things the user said.
    assert cv.is_genuine_user_transcript(
        "open my calendar", last_seen_text="open my calendar") is False


def test_genuine_transcript_accepts_a_second_distinct_chunk():
    assert cv.is_genuine_user_transcript(
        "for tomorrow", last_seen_text="open my calendar") is True


def test_genuine_transcript_rejects_an_empty_chunk():
    assert cv.is_genuine_user_transcript("") is False
    assert cv.is_genuine_user_transcript("   ") is False


def test_genuine_transcript_does_not_compare_against_jarvis_once_he_has_finished():
    # recent_jarvis_text is only meaningful while JARVIS is actually
    # speaking — main.py passes "" once he's done. A user legitimately
    # repeating JARVIS's own words back (e.g. confirming an order) must not
    # be rejected just because it resembles his last answer.
    assert cv.is_genuine_user_transcript(
        "added that to your calendar", recent_jarvis_text="") is True
