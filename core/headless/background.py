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

from actions import agent_orchestrator as agent_scheduler_lock
from actions.agent_orchestrator import orchestrator as agent_orchestrator
from actions import background_monitor
from actions import business_intelligence as biz_intel
from actions.proactive import ProactiveEngine
from memory import config_manager

logger = logging.getLogger("jarvis.headless.background")

AGENT_SCHEDULER_POLL_SECS = 300
BACKGROUND_MONITOR_POLL_SECS = 1800
PROACTIVE_POLL_SECS = 60
OBJECTIVE_LOOP_POLL_SECS = 21_600  # 6 hours — matches get_stale_autonomous_agents()'s default staleness


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
        if self._tasks:
            return
        self._stopping = False
        self._tasks = [
            asyncio.create_task(self._run_agent_scheduler(), name="agent_scheduler"),
            asyncio.create_task(self._run_background_monitor(), name="background_monitor"),
            asyncio.create_task(self._run_proactive_observer(), name="proactive_observer"),
            asyncio.create_task(self._run_objective_loop(), name="objective_loop"),
        ]

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

    async def _run_agent_scheduler(self) -> None:
        have_lock = await asyncio.to_thread(agent_scheduler_lock.acquire_scheduler_lock)
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
                        logger.info("agent %s: %s", agent.name, summary_text)
                except Exception as e:
                    logger.warning("agent scheduler poll failed: %s", e)
        finally:
            if have_lock:
                await asyncio.to_thread(agent_scheduler_lock.release_scheduler_lock)

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

    async def _run_proactive_observer(self) -> None:
        while not self._stopping:
            await asyncio.sleep(PROACTIVE_POLL_SECS)
            try:
                await self.run_proactive_check_once()
            except Exception as e:
                logger.warning("proactive observer check failed: %s", e)
