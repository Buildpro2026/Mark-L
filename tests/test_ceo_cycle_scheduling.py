"""BackgroundWorker's CEO-operating-cycle loop (core/headless/background.py)
— the scheduling half of Lee's autonomous-CEO/COS spec Section THIRD ("This
cycle must be independently schedulable. It must NOT require me to initiate
a chat."). The cycle's own WAKE->REPORT logic is tested directly in
tests/test_ceo_operating_cycle.py; this file only tests WHEN the loop
decides to call it.
"""
import asyncio
from datetime import datetime, timezone

import pytest

from core.headless import background as bg

_REAL_SLEEP = asyncio.sleep  # captured before any test patches asyncio.sleep


def _fast_sleep(monkeypatch):
    """The loop opens with a hardcoded `await asyncio.sleep(20)` settle
    delay (same pattern as the background monitor's own 30s settle sleep)
    — real in production, but collapsed here so the loop reaches its poll
    body within a short test wait. Uses the real sleep(0) underneath so it
    still yields to the event loop each iteration rather than busy-spinning."""
    async def _tracking_sleep(secs):
        await _REAL_SLEEP(0)
    monkeypatch.setattr(bg.asyncio, "sleep", _tracking_sleep)


async def _run_loop_briefly(worker, wait_secs: float = 0.1) -> None:
    task = asyncio.create_task(worker._run_ceo_cycle_loop())
    await _REAL_SLEEP(wait_secs)
    worker._stopping = True
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_run_ceo_cycle_once_delegates_to_the_real_cycle(monkeypatch):
    calls = []
    monkeypatch.setattr(bg.ceo_operating_cycle, "run_cycle", lambda force=False: (calls.append(force) or {"state": "RAN"}))

    async def _run():
        worker = bg.BackgroundWorker()
        result = await worker.run_ceo_cycle_once()
        assert result == {"state": "RAN"}

    asyncio.run(_run())
    assert calls == [False]


def test_loop_runs_the_cycle_once_the_configured_utc_hour_is_reached(monkeypatch):
    _fast_sleep(monkeypatch)
    monkeypatch.setattr(bg.headless_config, "JARVIS_CEO_CYCLE_HOUR_UTC", 6)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 3, 6, 5, tzinfo=timezone.utc)

    monkeypatch.setattr(bg, "datetime", _FixedDatetime)
    monkeypatch.setattr(bg.ceo_operating_cycle, "already_ran_today", lambda: False)

    ran = []
    monkeypatch.setattr(bg.ceo_operating_cycle, "run_cycle", lambda force=False: (ran.append(1) or {"state": "RAN"}))

    async def _run():
        worker = bg.BackgroundWorker()
        await _run_loop_briefly(worker)

    asyncio.run(_run())
    assert ran, "loop never ran the cycle even though the target hour was reached"


def test_loop_does_not_run_before_the_configured_hour(monkeypatch):
    _fast_sleep(monkeypatch)
    monkeypatch.setattr(bg.headless_config, "JARVIS_CEO_CYCLE_HOUR_UTC", 11)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 3, 4, 0, tzinfo=timezone.utc)  # well before 11:00 UTC

    monkeypatch.setattr(bg, "datetime", _FixedDatetime)

    ran = []
    monkeypatch.setattr(bg.ceo_operating_cycle, "run_cycle", lambda force=False: (ran.append(1) or {"state": "RAN"}))

    async def _run():
        worker = bg.BackgroundWorker()
        await _run_loop_briefly(worker)

    asyncio.run(_run())
    assert not ran, "loop ran the cycle before the configured UTC hour"


def test_loop_does_not_rerun_if_already_ran_today(monkeypatch):
    _fast_sleep(monkeypatch)
    monkeypatch.setattr(bg.headless_config, "JARVIS_CEO_CYCLE_HOUR_UTC", 6)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(bg, "datetime", _FixedDatetime)
    monkeypatch.setattr(bg.ceo_operating_cycle, "already_ran_today", lambda: True)

    ran = []
    monkeypatch.setattr(bg.ceo_operating_cycle, "run_cycle", lambda force=False: (ran.append(1) or {"state": "RAN"}))

    async def _run():
        worker = bg.BackgroundWorker()
        await _run_loop_briefly(worker)

    asyncio.run(_run())
    assert not ran, "loop re-ran the cycle even though already_ran_today() was True"
