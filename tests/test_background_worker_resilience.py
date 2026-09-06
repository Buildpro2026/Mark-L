"""core/headless/background.py's BackgroundWorker — proves the self-healing
claim every loop's docstring/try-except implies, rather than just trusting
it from reading the code: an exception inside a single pass must be caught,
logged, and the loop must keep running (and keep retrying the same work on
its next iteration) instead of the task silently dying. Every existing test
of this module (test_headless_core.py) checks that the six tasks start and
that the scheduler polls immediately — none of them ever inject a failure
and confirm the loop survives it. This file closes that gap for all six
loops, one at a time (each test drives only the one loop task under test,
with asyncio.sleep faked to fast-forward, so there's no cross-loop
interference from patching the shared bg.asyncio.sleep)."""
import asyncio

import pytest

from core.headless import background as bg

# Captured BEFORE any monkeypatching, and used only by this test file's own
# _spin_until — bg.asyncio IS the same shared `asyncio` module object (not a
# copy), so patching bg.asyncio.sleep below silently fakes every other
# caller's asyncio.sleep() too, this test file's included. Without its own
# untouched reference, _spin_until's "wait a little" would itself be faked
# to a zero-time yield, which starves the real wall-clock guard some of
# these loops use (see test_approval_notifier_survives_a_failed_pass_and_
# keeps_retrying) and made every spin loop here race the CPU instead of
# actual elapsed time.
_real_sleep = asyncio.sleep


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    """Replaces asyncio.sleep with an instant yield so a loop's real poll
    interval (5-30 min) doesn't make these tests slow — every loop here
    is driven for a handful of logical iterations, not real wall-clock
    time."""
    async def _tracking_sleep(secs):
        await _real_sleep(0)

    monkeypatch.setattr(bg.asyncio, "sleep", _tracking_sleep)


async def _spin_until(predicate, attempts: int = 1000):
    """Yields control repeatedly so a background task's iterations actually
    get scheduled, until `predicate()` is true or the attempt budget runs
    out (never hangs a test if a loop stops advancing). Uses the real,
    non-zero sleep captured above (not the faked bg.asyncio.sleep) — several
    of these loops route their actual work through asyncio.to_thread, which
    runs on a real OS thread pool, and one (_run_approval_notifier) paces
    itself off a real time.monotonic() wall-clock guard; either needs
    genuine elapsed time to pass, which a zero-delay yield can't provide."""
    for _ in range(attempts):
        if predicate():
            return True
        await _real_sleep(0.005)
    return False


# ── agent scheduler ──────────────────────────────────────────────────────

def test_agent_scheduler_survives_a_failed_poll_and_keeps_polling(monkeypatch):
    monkeypatch.setattr(bg.agent_scheduler_lock, "acquire_scheduler_lock", lambda: True)
    monkeypatch.setattr(bg.agent_scheduler_lock, "refresh_scheduler_lock", lambda: None)
    monkeypatch.setattr(bg.agent_scheduler_lock, "release_scheduler_lock", lambda: None)

    calls = {"n": 0}

    def _get_due_agents():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated transient failure")
        return []

    monkeypatch.setattr(bg.agent_orchestrator, "get_due_agents", _get_due_agents)

    async def _run():
        worker = bg.BackgroundWorker()
        task = asyncio.create_task(worker._run_agent_scheduler())
        try:
            survived = await _spin_until(lambda: calls["n"] >= 3)
            assert survived, f"scheduler loop stopped retrying after a failure (only {calls['n']} call(s))"
            assert not task.done(), "scheduler task died instead of catching the exception"
        finally:
            worker._stopping = True
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())


# ── objective loop ───────────────────────────────────────────────────────

def test_objective_loop_survives_a_failed_check_and_keeps_checking(monkeypatch):
    calls = {"n": 0}

    def _run_stale_autonomous_agents():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated transient failure")
        return []

    monkeypatch.setattr(bg.agent_orchestrator, "run_stale_autonomous_agents", _run_stale_autonomous_agents)

    async def _run():
        worker = bg.BackgroundWorker()
        task = asyncio.create_task(worker._run_objective_loop())
        try:
            survived = await _spin_until(lambda: calls["n"] >= 3)
            assert survived, f"objective loop stopped retrying after a failure (only {calls['n']} call(s))"
            assert not task.done(), "objective loop task died instead of catching the exception"
        finally:
            worker._stopping = True
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())


# ── background topic monitor ─────────────────────────────────────────────

def test_background_monitor_survives_a_failed_check_and_keeps_checking(monkeypatch):
    calls = {"n": 0}

    def _check_all():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated transient failure")
        return []

    monkeypatch.setattr(bg.background_monitor, "check_all", _check_all)

    async def _run():
        worker = bg.BackgroundWorker()
        task = asyncio.create_task(worker._run_background_monitor())
        try:
            survived = await _spin_until(lambda: calls["n"] >= 3)
            assert survived, f"background monitor stopped retrying after a failure (only {calls['n']} call(s))"
            assert not task.done(), "background monitor task died instead of catching the exception"
        finally:
            worker._stopping = True
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())


# ── proactive observer ────────────────────────────────────────────────────

def test_proactive_observer_survives_a_failed_check_and_keeps_checking(monkeypatch):
    calls = {"n": 0}

    def _get_proactive_enabled():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated transient failure")
        return False   # disabled — should_trigger() short-circuits, no further mocking needed

    monkeypatch.setattr(bg.config_manager, "get_proactive_enabled", _get_proactive_enabled)

    async def _run():
        worker = bg.BackgroundWorker()
        task = asyncio.create_task(worker._run_proactive_observer())
        try:
            survived = await _spin_until(lambda: calls["n"] >= 3)
            assert survived, f"proactive observer stopped retrying after a failure (only {calls['n']} call(s))"
            assert not task.done(), "proactive observer task died instead of catching the exception"
        finally:
            worker._stopping = True
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())


# ── approval notifier ─────────────────────────────────────────────────────

def test_approval_notifier_survives_a_failed_pass_and_keeps_retrying(monkeypatch):
    # _run_approval_notifier paces itself off a real time.monotonic() wall-clock
    # guard, deliberately not just asyncio.sleep() (see its own comment: a fast
    # loop must not turn into a burst of passes even if sleeps are shortened).
    # Faking bg.asyncio.sleep alone can't fast-forward past that real-time
    # guard, so shrink the interval itself to something the test's actual
    # (sub-second) run time can clear.
    monkeypatch.setattr(bg, "APPROVAL_NOTIFY_POLL_SECS", 0.01)

    calls = {"n": 0}

    def _notify_pending():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated transient failure")
        return []

    from actions import approval_notifier
    monkeypatch.setattr(approval_notifier, "notify_pending", _notify_pending)

    async def _run():
        worker = bg.BackgroundWorker()
        task = asyncio.create_task(worker._run_approval_notifier())
        try:
            survived = await _spin_until(lambda: calls["n"] >= 3)
            assert survived, f"approval notifier stopped retrying after a failure (only {calls['n']} call(s))"
            assert not task.done(), "approval notifier task died instead of catching the exception"
        finally:
            worker._stopping = True
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())


# ── CEO operating cycle loop ──────────────────────────────────────────────

def test_ceo_cycle_loop_survives_a_failed_check_and_keeps_checking(monkeypatch):
    monkeypatch.setattr(bg.headless_config, "JARVIS_CEO_CYCLE_HOUR_UTC", 0)  # always past the target hour

    calls = {"n": 0}

    def _already_ran_today():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated transient failure")
        return True   # already ran — short-circuits before run_cycle, no further mocking needed

    monkeypatch.setattr(bg.ceo_operating_cycle, "already_ran_today", _already_ran_today)

    async def _run():
        worker = bg.BackgroundWorker()
        task = asyncio.create_task(worker._run_ceo_cycle_loop())
        try:
            survived = await _spin_until(lambda: calls["n"] >= 3)
            assert survived, f"CEO cycle loop stopped retrying after a failure (only {calls['n']} call(s))"
            assert not task.done(), "CEO cycle loop task died instead of catching the exception"
        finally:
            worker._stopping = True
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())


# ── cross-loop isolation ──────────────────────────────────────────────────

def test_one_loop_failing_does_not_affect_the_others(monkeypatch):
    """The actual point of catching exceptions per-loop rather than letting
    one propagate: a failure in the agent scheduler must not take down the
    background monitor, objective loop, etc. — they're independent asyncio
    tasks, but only a real run proves nothing implicit couples them (e.g.
    a shared un-caught exception context, or a lock held across loops)."""
    monkeypatch.setattr(bg.agent_scheduler_lock, "acquire_scheduler_lock", lambda: True)
    monkeypatch.setattr(bg.agent_scheduler_lock, "refresh_scheduler_lock", lambda: None)
    monkeypatch.setattr(bg.agent_scheduler_lock, "release_scheduler_lock", lambda: None)

    def _always_fails():
        raise RuntimeError("scheduler is permanently broken this test")

    monkeypatch.setattr(bg.agent_orchestrator, "get_due_agents", _always_fails)

    monitor_calls = {"n": 0}
    monkeypatch.setattr(bg.background_monitor, "check_all", lambda: (monitor_calls.__setitem__("n", monitor_calls["n"] + 1) or []))

    async def _run():
        worker = bg.BackgroundWorker()
        worker.start()
        try:
            survived = await _spin_until(lambda: monitor_calls["n"] >= 2)
            assert survived, "background monitor stalled while the scheduler loop kept failing"
            assert all(not t.done() for t in worker._tasks), "a loop died as a side effect of another loop's failure"
        finally:
            await worker.stop()

    asyncio.run(_run())
