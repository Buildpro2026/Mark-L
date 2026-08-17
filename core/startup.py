"""Startup/shutdown reliability helpers.

Added to close two verified gaps that made restart/duplicate-launch
behavior undependable:

1. The dashboard (dashboard/server.py) let uvicorn call sys.exit() on a
   port that's already bound. Because that runs inside an asyncio Task
   created with create_task() and never awaited, asyncio.Task.__step's
   special-case handling for (KeyboardInterrupt, SystemExit) re-raises it
   into the event loop — silently killing the ENTIRE JARVIS process (not
   just the dashboard) the moment two instances' dashboards fight over a
   port. dashboard/server.py now pre-checks each port with is_port_free()
   before ever calling uvicorn, and also catches SystemExit around the
   uvicorn .serve() call as a backstop against any remaining bind-time
   race.
2. Nothing detected or reported a duplicate JARVIS instance, and nothing
   reliably released the agent-scheduler lock file on shutdown — voice
   shutdown used a raw os._exit(0) (no cleanup at all) and closing the
   window via the title-bar X never touched the background asyncio
   thread (a daemon thread, killed without running its `finally` blocks
   when the interpreter starts finalizing).

None of this replaces the existing, already-solid stale-lock design in
actions/agent_orchestrator.py (dead-PID reclaim, then an age backstop) —
the single-instance lock here mirrors that same pattern for a second,
whole-process-scoped lock.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from core.headless.config import DATA_DIR

BASE_DIR = Path(__file__).resolve().parent.parent
APP_LOCK_PATH = DATA_DIR / "jarvis_app.lock"
LOG_DIR = DATA_DIR / "logs"
LOG_PATH = LOG_DIR / "jarvis.log"

# This lock is written once at startup and released at shutdown — it is
# NOT refreshed periodically like the scheduler lock is, so its age
# backstop is deliberately generous (a real JARVIS session can run for
# hours) rather than a tight heartbeat window. Dead-PID reclaim is the
# primary signal either way.
_STALE_APP_LOCK_SECONDS = 6 * 3600

REQUIRED_CONFIG_KEYS = ["gemini_api_key"]
OPTIONAL_CONFIG_KEYS = ["hubspot_token", "twilio", "buffer_token", "airtable_token"]

_logger: logging.Logger | None = None


def get_logger() -> logging.Logger:
    """Lazily-created lifecycle logger — writes ONLY the startup/
    shutdown/lock/port-conflict events this module and dashboard/server.py
    emit (never raw conversation content), to a rotating file under
    data/logs/ (git-ignored, same as the rest of data/). Best-effort: if
    the file can't be created (e.g. read-only filesystem), logging silently
    falls back to being a no-op rather than blocking startup."""
    global _logger
    if _logger is not None:
        return _logger
    logger = logging.getLogger("jarvis.lifecycle")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # Check specifically for OUR handler type, not "any handler" — other
    # code (notably pytest's own log-capturing plugin, confirmed live: it
    # attaches its LogCaptureHandler directly to this named logger) can
    # legitimately attach unrelated handlers here too. Checking bare
    # `logger.handlers` would see those and wrongly skip ever adding ours.
    already_has_our_handler = any(isinstance(h, RotatingFileHandler) for h in logger.handlers)
    if not already_has_our_handler:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            logger.addHandler(handler)
        except Exception:
            pass
    _logger = logger
    return logger


def _log(message: str) -> None:
    try:
        get_logger().info(message)
    except Exception:
        pass


# ── single-instance app lock ────────────────────────────────────────────

def _pid_is_running(pid: int) -> bool:
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:
        return False


def acquire_app_instance_lock(lock_path: Path | None = None) -> tuple[bool, dict | None]:
    """True + None if this process now holds the single-instance lock
    (it was free, or a dead/stale lock got reclaimed). False + the
    existing holder's {"pid", "acquired_ts"} if another live process
    genuinely holds it. Never raises — a filesystem error fails OPEN
    (treated as acquired) so a local disk hiccup can never block startup.

    lock_path defaults to the module-level APP_LOCK_PATH, looked up here
    (not as a default-argument value) so tests can monkeypatch.setattr()
    the module attribute and have it actually take effect — a default
    argument would instead snapshot the value once at function-definition
    time and never see the patched one."""
    lock_path = lock_path or APP_LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    if lock_path.exists():
        try:
            data = json.loads(lock_path.read_text(encoding="utf-8"))
            age = now - float(data.get("acquired_ts", 0))
            pid = data.get("pid")
            if age < _STALE_APP_LOCK_SECONDS and pid and pid != os.getpid() and _pid_is_running(int(pid)):
                return False, data
        except Exception:
            pass   # unreadable/corrupt lock file — treat as stale, reclaim below
    try:
        lock_path.write_text(json.dumps({"pid": os.getpid(), "acquired_ts": now}), encoding="utf-8")
        return True, None
    except Exception:
        return True, None


def release_app_instance_lock(lock_path: Path | None = None) -> None:
    """Only removes the lock if this process is the one that holds it —
    never raises. See acquire_app_instance_lock's docstring for why
    lock_path defaults via a None sentinel rather than a default argument."""
    lock_path = lock_path or APP_LOCK_PATH
    try:
        if lock_path.exists():
            data = json.loads(lock_path.read_text(encoding="utf-8"))
            if data.get("pid") == os.getpid():
                lock_path.unlink()
    except Exception:
        pass


def check_single_instance() -> bool:
    """Acquires the app-level single-instance lock and prints + logs a
    clear warning if another live JARVIS process already holds it.
    Always returns whether THIS call freshly acquired it (informational)
    — it never blocks startup, since a legitimate second instance should
    still degrade gracefully (dashboard ports simply get skipped with
    their own clear message, see dashboard/server.py) rather than be
    refused outright."""
    acquired, holder = acquire_app_instance_lock()
    if not acquired and holder:
        age_min = int((time.time() - float(holder.get("acquired_ts", 0))) / 60)
        msg = (
            f"[Startup] WARNING: another JARVIS instance appears to already be running "
            f"(PID {holder.get('pid')}, started ~{age_min} min ago). Continuing anyway, but "
            f"the dashboard's network ports may already be in use by that instance. If this "
            f"is wrong (e.g. a crash left a stale lock), delete data\\jarvis_app.lock and restart."
        )
        print(msg)
        _log(msg)
    else:
        _log(f"App instance lock acquired by PID {os.getpid()}.")
    return acquired


def graceful_release_all_locks() -> None:
    """Best-effort cleanup meant to run from EVERY shutdown path (voice
    'shutdown_jarvis', window close, Ctrl+C) — releases both the
    app-instance lock and the agent-scheduler lock (safe to call even if
    this process never held it; both release functions check ownership
    before unlinking) so a normal shutdown never leaves a lock behind for
    the stale-reclaim logic to have to clean up on the next launch.
    Idempotent — safe to call more than once per shutdown."""
    release_app_instance_lock()
    try:
        from actions import agent_orchestrator as _ao
        _ao.release_scheduler_lock()
    except Exception:
        pass
    _log(f"Shutdown cleanup: locks released for PID {os.getpid()}.")


# ── port pre-flight check ───────────────────────────────────────────────

def is_port_free(port: int, host: str = "0.0.0.0") -> bool:
    """Non-destructive bind test — opens and immediately closes a probe
    socket, never starts a server. Used to detect a port conflict BEFORE
    handing the port to uvicorn, whose own bind failure raises SystemExit
    rather than a catchable OSError from an awaited coroutine."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.2)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ── config validation summary ───────────────────────────────────────────

def summarize_startup_config(config_path: Path | None = None) -> dict:
    """Read-only summary of which required/optional config keys are
    present — never returns or prints actual values, only key presence.
    Never raises."""
    path = config_path or (BASE_DIR / "config" / "api_keys.json")
    summary = {"required_missing": [], "optional_missing": [], "config_readable": True}
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        data = {}
        summary["config_readable"] = False
    for key in REQUIRED_CONFIG_KEYS:
        if not data.get(key):
            summary["required_missing"].append(key)
    for key in OPTIONAL_CONFIG_KEYS:
        if not data.get(key):
            summary["optional_missing"].append(key)
    return summary


def print_startup_banner() -> None:
    """Prints (and logs) a concise, secret-free startup summary: required
    config status and which optional integrations aren't configured.
    Never blocks startup and never prints a value, only key presence."""
    summary = summarize_startup_config()
    lines = ["[Startup] Configuration check:"]
    if not summary["config_readable"]:
        lines.append("[Startup]   config/api_keys.json missing or invalid JSON — using defaults.")
    if summary["required_missing"]:
        lines.append(
            f"[Startup]   MISSING required: {', '.join(summary['required_missing'])} "
            "— JARVIS will prompt for setup."
        )
    else:
        lines.append("[Startup]   Required configuration present.")
    if summary["optional_missing"]:
        lines.append(
            "[Startup]   Optional integrations not configured (will report NOT_CONFIGURED, "
            f"degrade gracefully): {', '.join(summary['optional_missing'])}"
        )
    for line in lines:
        print(line)
    _log(" | ".join(lines))
