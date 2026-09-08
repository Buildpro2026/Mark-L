"""Conversation turn ownership — one owner of the response channel at a
time, and stale work that cannot speak.

WHAT WAS ACTUALLY WRONG
Seven different places in main.py could write into the same Gemini Live
session: the user's voice, typed commands, the dashboard command relay,
the startup briefing, the system monitor, the background monitor and
proactive mode. Every background writer guarded itself with the same
shape:

    with self._speaking_lock:
        speaking = self._is_speaking
    if not speaking:
        await self.session.send_client_content(...)   # <-- gap

That is a check-then-act race. The lock is released before the send, so a
user can begin speaking in the gap and the monitor's text lands in the
middle of their turn. Three independent writers doing this is why JARVIS
would change topic mid-sentence.

`_interrupted` was a bare bool, so it could only describe "an interrupt is
in flight", never WHICH response was interrupted. After a barge-in the old
response's turn_complete cleared the flag; audio still in flight from that
same response then found the flag clear and played into the new turn.
A monotonic generation counter fixes that class of bug outright: audio
carries the generation it was produced for, and anything not matching the
current generation is discarded without needing to know why it is late.

And nothing guaranteed release. set_speaking(True) ... set_speaking(False)
were separate statements with a body between them; an exception in that
body left _is_speaking True forever, and because the mic callback gates on
_is_speaking, the microphone never reopened. JARVIS was silently deaf
until restart.

THE MODEL
One counter, one owner, one lock held across the decision AND the claim.

    claim()    -- atomic: decide and take ownership under one lock
    is_current -- stale-work test for anything holding an older generation
    cancel()   -- atomic: bump the generation; all older work is now stale
    turn()     -- context manager; releases on the exception path too

Background work never wins a contest against the user. It is deferred and
replayed later, not dropped and not forced through.

This module owns no audio, no Qt, no network. It is the decision layer
only, which is what makes it testable without a sound card.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

# ── States ───────────────────────────────────────────────────────────────
IDLE = "IDLE"
LISTENING = "LISTENING"
USER_TURN = "USER_TURN"
THINKING = "THINKING"
TOOL_EXECUTING = "TOOL_EXECUTING"
SPEAKING = "SPEAKING"
INTERRUPTING = "INTERRUPTING"
CANCELLED = "CANCELLED"
COMPLETED = "COMPLETED"
ERROR = "ERROR"

ALL_STATES = (IDLE, LISTENING, USER_TURN, THINKING, TOOL_EXECUTING, SPEAKING,
              INTERRUPTING, CANCELLED, COMPLETED, ERROR)

# A turn is either the user's or the system's. The distinction is the whole
# priority model: USER always wins, BACKGROUND always yields.
SOURCE_USER = "USER"
SOURCE_BACKGROUND = "BACKGROUND"

# States in which the user is mid-interaction and must not be talked over.
_BUSY_STATES = frozenset({USER_TURN, THINKING, TOOL_EXECUTING, SPEAKING, INTERRUPTING})

# How long a deferred background message stays worth saying. A system alert
# replayed forty minutes late is noise, not help.
DEFAULT_DEFER_TTL_SECONDS = 900.0


class TurnRejected(Exception):
    """Raised when a background turn cannot take the channel. Carries the
    reason so the caller can log why rather than failing silently."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Turn:
    """A claim on the response channel. `generation` is the stale-work key:
    hold it, and check is_current() before writing anything anywhere."""

    __slots__ = ("generation", "source", "started", "manager")

    def __init__(self, generation: int, source: str, manager: "ConversationManager"):
        self.generation = generation
        self.source = source
        self.started = time.monotonic()
        self.manager = manager

    @property
    def is_current(self) -> bool:
        return self.manager.is_current(self.generation)

    def __repr__(self) -> str:
        return f"<Turn gen={self.generation} source={self.source}>"


class ConversationManager:
    """Single owner of the response channel.

    Every public method takes the same lock, and each one decides AND acts
    inside it. That is the fix for the check-then-act race: there is no
    window between "is anyone speaking?" and "then I will speak"."""

    def __init__(self, on_state_change: Optional[Callable[[str, str], None]] = None,
                 defer_ttl: float = DEFAULT_DEFER_TTL_SECONDS):
        self._lock = threading.RLock()
        self._generation = 0
        self._state = IDLE
        self._owner: Optional[Turn] = None
        self._deferred: list[dict[str, Any]] = []
        self._defer_ttl = defer_ttl
        self._on_state_change = on_state_change
        self._last_user_activity = 0.0
        self.history: list[dict[str, Any]] = []   # ordered transitions, for tests and debugging

    # ── state ────────────────────────────────────────────────────────────
    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def is_current(self, generation: int) -> bool:
        """False for anything produced before the last cancel/new turn. This
        is the single test that keeps a stale response from speaking."""
        with self._lock:
            return generation == self._generation

    def set_state(self, state: str, reason: str = "") -> None:
        if state not in ALL_STATES:
            raise ValueError(f"unknown conversation state: {state}")
        with self._lock:
            previous, self._state = self._state, state
            self.history.append({"from": previous, "to": state, "reason": reason,
                                 "generation": self._generation, "ts": time.monotonic()})
            if state in (USER_TURN, LISTENING):
                self._last_user_activity = time.monotonic()
        if self._on_state_change and previous != state:
            try:
                self._on_state_change(previous, state)
            except Exception:
                # A UI callback must never be able to break the voice loop.
                pass

    # ── claiming ─────────────────────────────────────────────────────────
    def claim(self, source: str = SOURCE_USER, *, preempt: bool = None,
              label: str = "") -> Turn:
        """Take ownership of the response channel.

        A USER turn preempts whatever is running — that is barge-in, and it
        is not negotiable. A BACKGROUND turn never preempts; it raises
        TurnRejected while the user is mid-interaction, and the caller is
        expected to defer() it.

        Decision and claim happen under one lock, so no turn can start in
        the gap between them."""
        if preempt is None:
            preempt = (source == SOURCE_USER)
        with self._lock:
            if not preempt and self._state in _BUSY_STATES:
                raise TurnRejected(
                    f"{source} turn refused: conversation is {self._state}")
            # Bumping the generation is what makes cancellation atomic —
            # every older audio chunk, tool result and callback is stale
            # from this instant, with no separate flag to clear.
            self._generation += 1
            turn = Turn(self._generation, source, self)
            self._owner = turn
        self.set_state(USER_TURN if source == SOURCE_USER else THINKING,
                       reason=label or f"{source} turn claimed")
        return turn

    def try_claim(self, source: str = SOURCE_BACKGROUND, label: str = "") -> Optional[Turn]:
        """claim() without the exception, for the common background case."""
        try:
            return self.claim(source, label=label)
        except TurnRejected:
            return None

    def cancel(self, reason: str = "interrupted") -> int:
        """Atomically invalidate the current turn. Returns the new
        generation. Everything produced for the old one is stale from the
        moment this returns — no draining required for correctness, though
        callers still drain queued audio so the user stops hearing it."""
        with self._lock:
            self._generation += 1
            self._owner = None
            new_generation = self._generation
        self.set_state(CANCELLED, reason=reason)
        return new_generation

    def complete(self, turn: Optional[Turn] = None, reason: str = "completed") -> None:
        """Finish a turn normally. A stale turn completing is a no-op: it
        must not drag the manager out of the newer turn's state."""
        with self._lock:
            if turn is not None and turn.generation != self._generation:
                return
            self._owner = None
        self.set_state(COMPLETED, reason=reason)

    def fail(self, turn: Optional[Turn] = None, reason: str = "error") -> None:
        with self._lock:
            if turn is not None and turn.generation != self._generation:
                return
            self._owner = None
        self.set_state(ERROR, reason=reason)

    def to_idle(self, reason: str = "idle") -> None:
        self.set_state(IDLE, reason=reason)

    def to_listening(self, reason: str = "listening") -> None:
        self.set_state(LISTENING, reason=reason)

    # ── the guarantee ────────────────────────────────────────────────────
    @contextmanager
    def turn(self, source: str = SOURCE_USER, label: str = "") -> Iterator[Turn]:
        """Own the channel for the duration of the block, and release it on
        the way out — including when the block raises.

        This is the fix for JARVIS getting stuck SPEAKING forever: the
        release is structural, not a statement someone has to remember to
        reach."""
        claimed = self.claim(source, label=label)
        try:
            yield claimed
        except BaseException:
            self.fail(claimed, reason="exception during turn")
            raise
        else:
            self.complete(claimed)
        finally:
            # Whatever happened, never leave the channel marked busy.
            with self._lock:
                stuck = self._state in _BUSY_STATES and (
                    self._owner is None or self._owner.generation == claimed.generation)
            if stuck:
                self.set_state(LISTENING, reason="released after turn")

    # ── barge-in ─────────────────────────────────────────────────────────
    def barge_in(self, reason: str = "user spoke over JARVIS") -> Turn:
        """The user spoke while JARVIS was talking. One atomic step: the old
        generation dies and the user's new turn is claimed, with no window
        in between for the old response to sneak a chunk through."""
        with self._lock:
            self.set_state(INTERRUPTING, reason=reason)
            self._generation += 1
            self._owner = None
            self._generation += 1
            turn = Turn(self._generation, SOURCE_USER, self)
            self._owner = turn
        self.set_state(USER_TURN, reason="user turn after barge-in")
        return turn

    # ── deferred background work ─────────────────────────────────────────
    def defer(self, payload: Any, kind: str = "background") -> None:
        """Hold a background message until the user is free. Deferring is
        what a refused background turn should do — dropping it loses a real
        alert, and forcing it through is the bug we are fixing."""
        with self._lock:
            self._deferred.append({"payload": payload, "kind": kind, "at": time.monotonic()})

    def take_deferred(self) -> list[Any]:
        """Everything still worth saying, oldest first. Expired items are
        discarded rather than replayed late — a stale alert is noise."""
        now = time.monotonic()
        with self._lock:
            fresh = [d for d in self._deferred if (now - d["at"]) <= self._defer_ttl]
            self._deferred = []
        return [d["payload"] for d in fresh]

    @property
    def deferred_count(self) -> int:
        with self._lock:
            return len(self._deferred)

    def seconds_since_user_activity(self) -> float:
        with self._lock:
            if not self._last_user_activity:
                return float("inf")
            return time.monotonic() - self._last_user_activity

    # ── duplicate-session protection ─────────────────────────────────────
    def register_session(self, session_id: str) -> bool:
        """True if this session may start, False if one is already live.

        main.py builds the Gemini Live session inside a reconnect loop; a
        reconnect that fires while the previous session is still up gives
        two live sessions and two listeners on one microphone, which
        presents as JARVIS answering twice."""
        with self._lock:
            existing = getattr(self, "_session_id", None)
            if existing is not None and existing != session_id:
                return False
            self._session_id = session_id
            return True

    def release_session(self, session_id: str) -> None:
        with self._lock:
            if getattr(self, "_session_id", None) == session_id:
                self._session_id = None

    @property
    def active_session(self) -> Optional[str]:
        with self._lock:
            return getattr(self, "_session_id", None)
