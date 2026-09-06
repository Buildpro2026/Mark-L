# Windows Operator Runbook — JARVIS (Mark-L)

Concise start/stop/restart/health-check/recovery reference for running
JARVIS on Windows. For development setup, testing, and per-integration
detail, see `docs/OPERATIONS.md`. This runbook does not install a Windows
service, Scheduled Task, or startup-on-boot entry — JARVIS is a
foreground desktop app you start and stop by hand (or via the launcher
below).

## Start

Double-click **`Start Mark-L.bat`** in the repository root. It:
- switches to the repo folder regardless of where it's double-clicked from,
- checks `.venv\Scripts\python.exe` exists (prints a clear message and
  pauses if not — see "First-time setup" below),
- runs `main.py`, and pauses with the exit code + a pointer to
  `data\logs\jarvis.log` if it exits non-zero, so a double-click launch
  doesn't just flash a window and vanish on failure.

Equivalent manual command (same thing, run from a terminal):
```
.venv\Scripts\python.exe main.py
```

On launch, JARVIS prints a startup banner covering:
- whether required configuration (`gemini_api_key`) is present,
- which **optional** integrations (HubSpot/Twilio/Buffer/Airtable) aren't
  configured — these degrade gracefully (`NOT_CONFIGURED`), they don't
  block startup,
- a warning if another JARVIS instance appears to already be running
  (see "Duplicate instance" below).

## Stop

Three ways, all safe:
- Say "shut down" (or similar) to JARVIS — it says goodbye, saves the
  session summary, releases its lock files, then exits.
- Close the JARVIS window (the title-bar X). This runs the same
  lock-release cleanup as voice shutdown before the process exits.
- Ctrl+C in the terminal, if launched from one.

All three paths release the app-instance lock (`data\jarvis_app.lock`)
and the agent-scheduler lock (`data\agent_scheduler.lock`) before
exiting. If JARVIS is killed harder than that (Task Manager "End Task",
a system crash, power loss), the locks are still recovered safely on the
next launch — see "Stale locks" below.

## Restart

Stop (any method above), then Start again. No extra steps — there is no
separate service to restart, and no manual lock cleanup needed in the
normal case.

## Health check

Non-destructive, makes no external calls, safe to run anytime (including
while JARVIS is running):
```
.venv\Scripts\python.exe scripts\health_check.py
```
Reports Python version, required package availability, whether
`config/api_keys.json` has a `gemini_api_key` set (presence only — never
prints the value), which optional integrations are configured, Google
OAuth file presence, database/memory file validity, and whether the
dashboard's ports (8000/8001/8002) are currently free. Exit code `0` if
all *required* checks pass.

## Logs

Lifecycle events (startup config summary, duplicate-instance/port-conflict
warnings, shutdown lock cleanup) are written to:
```
data\logs\jarvis.log
```
(rotates at ~2MB, keeps 3 backups). This file never contains API keys,
tokens, or message content — only key **presence**, PIDs, ports, and
fixed status text. General conversation/error output still goes to the
console and the in-app log panel, unchanged from before.

## Common recovery steps

**Port already in use / "another JARVIS instance" warning**
JARVIS's dashboard tries ports 8000/8001/8002. If one is already bound
(most commonly a second JARVIS instance, or a previous one that hasn't
fully exited yet), JARVIS now prints a clear message and skips *only*
that dashboard port — the rest of JARVIS (voice, tools, memory) keeps
working normally; you just won't get remote/phone control until the
conflict is resolved. To resolve: check Task Manager for a leftover
`python.exe` and close it, or close whatever else is using that port,
then restart JARVIS.

**Stale locks (`data\jarvis_app.lock`, `data\agent_scheduler.lock`)**
Both are PID + timestamp files, reclaimed automatically on next launch
if the PID that wrote them is no longer running — you do not need to
delete them by hand after a normal crash. If JARVIS still reports
"another instance is running" and you're sure that's wrong (e.g. the PID
in the warning message no longer appears in Task Manager), delete the
two files above and restart.

**JARVIS won't start / exits immediately**
1. Run the health check (above) — it will point at the specific missing
   requirement (Python version, missing package, missing
   `gemini_api_key`).
2. Check `data\logs\jarvis.log` and the console output the launcher left
   on screen for the actual error.
3. If `.venv` is missing or broken, see "First-time setup" below to
   rebuild it.

**Corrupt config/memory file**
`config/api_keys.json` and `memory/long_term.json` both self-heal on a
parse failure: the corrupt file is renamed aside
(`*.corrupt-<timestamp>.json`) and a fresh default is used, rather than
JARVIS failing to start. If this happens, re-enter any API keys that
were lost via the app's setup/reconfiguration prompt.

## First-time setup (only if `.venv` doesn't exist yet)

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```
Then create `config/api_keys.json` with at least a `gemini_api_key` (see
`docs/OPERATIONS.md` for the full key table), and run the health check
to confirm before first launch.
