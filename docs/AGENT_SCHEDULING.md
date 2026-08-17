# Agent Scheduling — Persistence, Restart Recovery, and the Single-Instance Lock

`actions/agent_orchestrator.py`'s background agent scheduling
(`get_due_agents()`/`run_due_agents()`, polled every 5 minutes by
`main.py`'s `_run_agent_scheduler()`) already had solid, well-tested
schedule-calculation logic and safety gates — an agent only auto-runs if
it's `IDLE` (explicitly `start_agent()`'d) and not `EXECUTE`-level (those
always need `approve_task()`, schedule or not). What it didn't have: any
of that state survived a restart.

## The bug this fixes

Before this prompt, `AgentOrchestrator` was **entirely in-memory** — every
agent's `status`/`last_run_ts`/`last_error`, and all task/event history,
lived only in Python objects. On every JARVIS restart, every agent reset
to `AgentStatus.REGISTERED` (the dataclass default), silently undoing
every `start_agent()` call that had ever been made. Since `IDLE` status is
the *only* gate that lets an agent auto-run on schedule, this meant:
**every scheduled agent silently stopped running after every restart**,
with no error, no log line, nothing — the user would only notice by the
absence of expected background activity, if at all.

## What changed

Every mutator (`start_agent`/`stop_agent`/`assign_task`/`approve_task`/
`reject_task`/`run_task`/`report_event`/`_log_event`) now writes through
to `data/jarvis2.db` (three new tables: `agent_state`, `agent_tasks`,
`agent_events`) immediately, and `AgentOrchestrator.__init__` restores
persisted state on construction. All writes are best-effort — a
persistence failure (e.g. a locked/corrupt DB file) degrades to the old
in-memory-only behavior for that operation rather than blocking a real
status change or task run. See `tests/test_agent_scheduling_persistence.py`
for restart-recovery tests (a second `AgentOrchestrator` instance against
the same DB correctly recovers status/`last_run_ts`/`last_error`/task
and event history).

## Single-instance scheduler lock

Two JARVIS processes running simultaneously (e.g. launched twice by
accident) would previously both independently poll `get_due_agents()`
against their own separate in-memory state and could both decide the
same agent is due, running it twice. `main.py`'s `_run_agent_scheduler()`
now acquires a file lock (`data/agent_scheduler.lock`, PID + timestamp)
before scheduling, refreshes it every poll, and releases it on shutdown.

**A stale lock can never permanently block scheduling** — two
independent checks, either of which reclaims it:
1. The recorded PID isn't a running process (`psutil.pid_exists()`).
2. The lock is older than 15 minutes (three poll cycles) — a hard
   age-based backstop that reclaims the lock even if PID-liveness
   checking itself were somehow wrong.

A process that fails to acquire the lock doesn't give up — it retries on
every subsequent poll, so if the other instance exits, scheduling resumes
in this process without needing a restart.

## What was already fine (verified, not changed)

- **Timezone correctness**: schedules are duration-based ("60m", "2h"
  since last run), not wall-clock/calendar-based, so there's no timezone
  calculation to get wrong.
- **Bounded retries**: `run_task()`'s `finally` block sets `last_run_ts`
  regardless of success or failure — a failed scheduled task is not
  retried immediately; the next attempt waits for the agent's own
  schedule interval, same as a successful run would.
- **Cancellation**: `reject_task()` already covered the one case where
  cancellation is meaningful (an `EXECUTE`-level task still
  `PENDING_APPROVAL`) — `OBSERVE`/`SUGGEST` tasks run synchronously inside
  `assign_task()`, so there's no in-flight window to cancel.
- **Dashboard visibility**: `AgentOrchestrator.summary()` was already
  wired into `dashboard/server.py`'s system module data — now more
  accurate after a restart too, since the underlying state it reads is
  no longer wiped.
- **Schedule calculation tests**: `tests/test_agent_scheduler.py` already
  covered `get_due_agents()`/`_parse_schedule_minutes()` thoroughly.
