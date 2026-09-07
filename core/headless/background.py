"""Background intelligence that runs without a live Gemini Live session —
the J1 finding this fixes: previously the agent scheduler, the topic
monitor, and the proactive engine only ran inside main.py's asyncio
TaskGroup, itself only alive while a websocket voice session was open
(main.py:3202-3234 in the pre-J2 codebase). None of that is a good reason
for background work to stop when nobody's talking to JARVIS.

Three loops, ported from main.py's _run_agent_scheduler/
_run_background_monitor/_run_proactive_mode:

  - Agent scheduler: unchanged in substance — actions/agent_orchestrator.py
    was already fully headless-safe (no UI/session imports at all), so this
    just re-runs the exact same due-agent poll main.py did, without a UI
    log line or dashboard toast (there's no UI here to write to).

  - Background topic monitor: main.py's version gated the actual check
    behind `if self.session:` — meaning with no live session it silently
    did nothing at all, not even the check. Here it always runs the check
    (checking DDG for monitored topics costs nothing extra) and logs any
    alert to business intelligence instead of speaking it, since there's
    no voice channel to speak through. Delivery to a human (push
    notification, dashboard toast, next voice session) is future work —
    this logs the fact that something changed, honestly, rather than
    pretending it was delivered.

  - Proactive engine: main.py's version decided WHEN to check in and
    handed Gemini a prompt to generate what to say, then spoke it into
    the live session. There's no "say something out loud" concept
    headless, and no reason to burn a Gemini call generating spoken text
    nobody will hear. This mode only runs the real should_trigger()
    decision gate and records that a check-in was due (mark_triggered(),
    the same persistent proactive_log write main.py's version made) —
    it does not fabricate or send a message. A future delivery channel
    (push notification, dashboard card) can read proactive_log and act on
    it; this doesn't pretend to be that channel.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from actions import agent_orchestrator as agent_scheduler_lock
from actions.agent_orchestrator import orchestrator as agent_orchestrator
from actions import background_monitor
from actions import business_intelligence as biz_intel
from actions import ceo_operating_cycle
from actions.proactive import ProactiveEngine
from core.headless import config as headless_config
from memory import config_manager

logger = logging.getLogger("jarvis.headless.background")

AGENT_SCHEDULER_POLL_SECS = 300
BACKGROUND_MONITOR_POLL_SECS = 1800
PROACTIVE_POLL_SECS = 60
# How often to check whether a decision is sitting on Lee. Five minutes is
# responsive enough that an approval doesn't rot, and the notifier itself
# deduplicates by task id, so this poll rate never turns into repeat texts.
APPROVAL_NOTIFY_POLL_SECS = 300
OBJECTIVE_LOOP_POLL_SECS = 21_600  # 6 hours — matches get_stale_autonomous_agents()'s default staleness
# How often to check whether it's time for the daily CEO operating cycle
# (Lee's autonomous-CEO/COS spec, Section THIRD). 15 minutes gives a UTC-hour
# target reasonable precision without being a busy poll; the cycle itself is
# idempotent per UTC date (see ceo_operating_cycle.already_ran_today), so a
# missed or repeated tick near the boundary can never double-run it.
CEO_CYCLE_POLL_SECS = 900
# How long a supervised loop waits before restarting after an unexpected
# crash, and the ceiling that backoff doubles toward. Short enough that a
# one-off blip costs almost nothing, capped so a permanently broken loop
# retries steadily instead of hot-spinning.
_SUPERVISOR_RESTART_BACKOFF_SECS = 5.0
_SUPERVISOR_RESTART_BACKOFF_MAX_SECS = 300.0


class BackgroundWorker:
    """Owns the three loops as separate asyncio tasks. `start()` returns
    immediately after scheduling them; `stop()` cancels and awaits
    cleanup (including releasing the scheduler lock, mirroring main.py's
    `finally` block). Safe to construct without starting — useful for
    tests that just want to call a loop's single-iteration body directly."""

    def __init__(self):
        self.proactive = ProactiveEngine()
        self._tasks: list[asyncio.Task] = []
        self._stopping = False

    def start(self) -> None:
        # Idempotent by design: a second start() while tasks are already
        # running is a no-op, so a double startup event (or a test calling
        # start() twice) can never produce two sets of loops racing each
        # other over the same agents.
        if self._tasks:
            return
        self._stopping = False

        loops: list[tuple[str, Any]] = [
            ("agent_scheduler", self._run_agent_scheduler),
            ("background_monitor", self._run_background_monitor),
            ("proactive_observer", self._run_proactive_observer),
            ("objective_loop", self._run_objective_loop),
            ("approval_notifier", self._run_approval_notifier),
        ]
        # The morning CEO cycle has exactly one scheduled owner. In this
        # deployment that owner is the Render Cron Job (jarvis-morning-ceo,
        # 0 11 * * *) running in its own container, so the web service does
        # NOT start this loop — see config.JARVIS_CEO_CYCLE_IN_WEB_SERVICE
        # for why a shared dedup table cannot solve this across containers.
        # The loop stays fully wired and tested; it is simply not scheduled
        # here unless the flag hands ownership back.
        if headless_config.JARVIS_CEO_CYCLE_IN_WEB_SERVICE:
            loops.append(("ceo_operating_cycle", self._run_ceo_cycle_loop))
            logger.info("CEO operating cycle loop ENABLED in the web service (owner: web service).")
        else:
            logger.info(
                "CEO operating cycle loop not started — the Render Cron Job owns the "
                "scheduled morning cycle. Set JARVIS_CEO_CYCLE_IN_WEB_SERVICE=true to "
                "hand ownership back to this process."
            )

        self._tasks = [
            asyncio.create_task(self._supervise(name, fn), name=name)
            for name, fn in loops
        ]

    async def _supervise(self, name: str, loop_fn) -> None:
        """Keeps one background loop alive for the life of the process.

        Each loop already guards its own per-iteration work, but an
        exception raised OUTSIDE that inner guard — in setup before the
        `while`, or in the loop machinery itself — used to end the task
        silently. asyncio does not report an exception until the task is
        awaited, and nothing awaits these until shutdown, so a dead worker
        looked exactly like a healthy idle one: no traceback, no log line,
        just work that quietly stopped happening.

        This restarts a crashed loop with a bounded backoff, and always
        says so. Cancellation is re-raised untouched so stop() still works;
        the backoff is capped so a permanently broken loop retries at a
        steady slow rate rather than spinning."""
        backoff = _SUPERVISOR_RESTART_BACKOFF_SECS
        while not self._stopping:
            try:
                await loop_fn()
                # A loop returning normally means self._stopping was set.
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._stopping:
                    return
                logger.exception(
                    "background loop %r crashed — restarting in %.0fs", name, backoff
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
                backoff = min(backoff * 2, _SUPERVISOR_RESTART_BACKOFF_MAX_SECS)

    async def stop(self) -> None:
        self._stopping = True
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._tasks = []

    # ── Agent scheduler ──────────────────────────────────────────────────

    async def run_scheduler_once(self) -> list:
        """One poll: run every currently-due agent. Exposed separately from
        the loop so tests (and a manual /admin trigger, if ever needed) can
        drive a single iteration without waiting on the real interval."""
        return await asyncio.to_thread(agent_orchestrator.run_due_agents)

    async def _lock_call(self, fn, default=None):
        """Runs one scheduler-lock operation without letting it end the loop.

        acquire/refresh/release are all documented as never raising, but
        they touch the filesystem, and the scheduler is the one loop whose
        death stops all scheduled agents. A lock error must degrade to
        "couldn't check the lock this tick", never to a silently dead
        worker — so every call goes through here."""
        try:
            return await asyncio.to_thread(fn)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("scheduler lock operation %s failed: %s", getattr(fn, "__name__", fn), e)
            return default

    async def _run_agent_scheduler(self) -> None:
        # Fails OPEN on a lock error (default=True): a filesystem hiccup
        # while reading the lock must not stop scheduled agents from
        # running, which is the same trade-off acquire_scheduler_lock()
        # itself makes internally.
        have_lock = await self._lock_call(agent_scheduler_lock.acquire_scheduler_lock, default=True)
        if not have_lock:
            logger.warning("Another JARVIS instance holds the scheduler lock — will keep retrying.")
        try:
            first_pass = True
            while not self._stopping:
                # Poll immediately on the very first pass rather than
                # sleeping AGENT_SCHEDULER_POLL_SECS (5 min) before ever
                # checking once — on a host that sleeps after inactivity
                # (e.g. Render's free tier), a wake-serve-sleep cycle
                # shorter than that delay would mean scheduled agents
                # never get polled at all, not just polled late.
                if not first_pass:
                    await asyncio.sleep(AGENT_SCHEDULER_POLL_SECS)
                first_pass = False
                if not have_lock:
                    have_lock = await self._lock_call(agent_scheduler_lock.acquire_scheduler_lock, default=True)
                    if not have_lock:
                        continue
                else:
                    await self._lock_call(agent_scheduler_lock.refresh_scheduler_lock)
                try:
                    due = await asyncio.to_thread(agent_orchestrator.get_due_agents)
                    for agent in due:
                        task = await asyncio.to_thread(
                            agent_orchestrator.assign_task, agent.id, "Scheduled background check"
                        )
                        summary_text = (task.result or {}).get("summary", "completed") if task.result else (task.error or "failed")
                        logger.info("agent %s: %s", agent.name, summary_text)
                except Exception as e:
                    logger.warning("agent scheduler poll failed: %s", e)
        finally:
            if have_lock:
                await self._lock_call(agent_scheduler_lock.release_scheduler_lock)

    # ── Autonomous objective loop (turns "monitor without being asked" ──
    # into real, executed work — see agent_orchestrator.get_stale_
    # autonomous_agents/run_stale_autonomous_agents for the actual
    # safety-scoped dispatch logic. This loop is deliberately thin: all
    # the judgment about which agents are safe to trigger generically
    # lives in AgentDefinition.autonomous_ok, not here.)

    async def run_objective_check_once(self) -> list:
        return await asyncio.to_thread(agent_orchestrator.run_stale_autonomous_agents)

    async def _run_objective_loop(self) -> None:
        first_pass = True
        while not self._stopping:
            if not first_pass:
                await asyncio.sleep(OBJECTIVE_LOOP_POLL_SECS)
            first_pass = False
            try:
                results = await self.run_objective_check_once()
                for task in results:
                    summary_text = (task.result or {}).get("summary", "completed") if task.result else (task.error or "failed")
                    logger.info("objective loop — agent %s: %s", task.agent_id, summary_text)
            except Exception as e:
                logger.warning("objective loop check failed: %s", e)

    # ── Background topic monitor ────────────────────────────────────────

    async def run_monitor_once(self) -> list[str]:
        alerts = await asyncio.to_thread(background_monitor.check_all)
        for alert in alerts:
            title = alert.splitlines()[0].replace("[MONITOR_ALERT] ", "").strip() or "Monitor alert"
            await asyncio.to_thread(
                biz_intel.add_entry, "market_observations", "general", title, alert
            )
            logger.info("monitor alert logged: %s", title)
        return alerts

    async def _run_background_monitor(self) -> None:
        await asyncio.sleep(30)   # brief settle time after process start
        while not self._stopping:
            try:
                await self.run_monitor_once()
            except Exception as e:
                logger.warning("background monitor check failed: %s", e)
            await asyncio.sleep(BACKGROUND_MONITOR_POLL_SECS)

    # ── Proactive observer (decide + record, never speak) ───────────────

    async def run_proactive_check_once(self, last_user_speech: float | None = None) -> bool:
        """Returns True if a check-in was judged due (and recorded).
        last_user_speech defaults to "a very long time ago" — headless has
        no concept of a live user session to have gone quiet on, so the
        silence gate is effectively always satisfied and only the cooldown/
        quiet-hours/enabled gates actually govern firing here."""
        if last_user_speech is None:
            last_user_speech = time.monotonic() - 10**6
        enabled = await asyncio.to_thread(config_manager.get_proactive_enabled)
        quiet_hours = await asyncio.to_thread(config_manager.get_proactive_quiet_hours)
        if not self.proactive.should_trigger(last_user_speech, enabled=enabled, quiet_hours=quiet_hours):
            return False
        self.proactive.mark_triggered()
        logger.info("proactive check-in due (headless — recorded, not spoken)")
        return True

    async def run_approval_notify_once(self) -> list:
        """One approval-notification pass, exposed separately so it can be
        driven directly by a test or an operator without waiting on the
        real interval — same pattern as the other loops here."""
        from actions import approval_notifier
        return await asyncio.to_thread(approval_notifier.notify_pending)

    async def _run_approval_notifier(self) -> None:
        # Wall-clock guard rather than trusting sleep() alone to pace this.
        # A pass reads the priorities engine and can send a text, so a loop
        # that spins — because sleeps were shortened, the clock jumped, or
        # the event loop got starved and woke everything at once — must not
        # turn into a burst of passes. This makes the interval a floor on
        # real elapsed time, not just a request to the scheduler.
        last_pass = 0.0
        await asyncio.sleep(45)   # let the first agent-scheduler pass land first
        while not self._stopping:
            now = time.monotonic()
            if now - last_pass >= APPROVAL_NOTIFY_POLL_SECS:
                last_pass = now
                try:
                    done = await self.run_approval_notify_once()
                    if done:
                        logger.info("approval notifications sent: %s", done)
                except Exception as e:
                    logger.warning("approval notifier pass failed: %s", e)
            await asyncio.sleep(APPROVAL_NOTIFY_POLL_SECS)

    async def _run_proactive_observer(self) -> None:
        while not self._stopping:
            await asyncio.sleep(PROACTIVE_POLL_SECS)
            try:
                await self.run_proactive_check_once()
            except Exception as e:
                logger.warning("proactive observer check failed: %s", e)

    # ── CEO operating cycle (Section THIRD) ──────────────────────────────
    # Deliberately thin, same pattern as every other loop here: the real
    # WAKE->REPORT logic lives in actions/ceo_operating_cycle.py so it can
    # be unit-tested and manually triggered without an event loop. This
    # just decides WHEN to call it — once the current UTC hour reaches the
    # configured target, and at most once per UTC calendar date (enforced
    # by ceo_operating_cycle.already_ran_today, not by this poll cadence,
    # so a restart mid-day can never double-run it).

    async def run_ceo_cycle_once(self, force: bool = False) -> dict:
        return await asyncio.to_thread(ceo_operating_cycle.run_cycle, force)

    async def _run_ceo_cycle_loop(self) -> None:
        await asyncio.sleep(20)   # brief settle time after process start
        while not self._stopping:
            try:
                now = datetime.now(timezone.utc)
                if now.hour >= headless_config.JARVIS_CEO_CYCLE_HOUR_UTC:
                    if not await asyncio.to_thread(ceo_operating_cycle.already_ran_today):
                        result = await self.run_ceo_cycle_once()
                        logger.info("CEO operating cycle: %s", result.get("state"))
            except Exception as e:
                logger.warning("CEO operating cycle check failed: %s", e)
            await asyncio.sleep(CEO_CYCLE_POLL_SECS)
