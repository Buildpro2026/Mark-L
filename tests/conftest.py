"""Shared test fixtures.

Applies to every test in this directory (pytest auto-discovers
conftest.py). Currently: isolates actions/agent_orchestrator.py's
persistence layer (added for restart-recovery — see docs/AGENT_SCHEDULING.md)
so the ~40 existing AgentOrchestrator(...) test-constructions across
test_agent_orchestrator_execution.py / test_agent_orchestrator_hooks.py /
test_agent_scheduler.py / test_agent_workforce.py / test_buildpro_agents.py
don't read or write the real data/jarvis2.db, and don't leak state between
tests via a shared file.
"""
import logging
from logging.handlers import RotatingFileHandler

import pytest

from actions import agent_orchestrator as _ao
from core import startup as _startup
from core.headless import config as _headless_config

# Fixed test credential for dashboard/server.py's /3d/api/*  and /3d/ws
# auth (added in J2 to close the J1 finding that those routes had none).
# Real deployments set JARVIS_API_TOKEN as a secret; tests use this fixed
# value via the autouse fixture below so every test that hits those routes
# can authenticate without touching a real pairing-key login flow. See
# tests/test_dashboard_3d.py, test_dashboard_buildpro_data.py,
# test_dashboard_phase9_integration.py, test_pwa_and_connection_state.py.
DASHBOARD_TEST_TOKEN = "test-dashboard-token-not-a-real-secret"
DASHBOARD_AUTH_HEADERS = {"Authorization": f"Bearer {DASHBOARD_TEST_TOKEN}"}


@pytest.fixture(autouse=True)
def _isolate_agent_orchestrator_db(monkeypatch, tmp_path):
    monkeypatch.setattr(_ao, "DB_PATH", tmp_path / "test_agent_orchestrator.db")
    monkeypatch.setattr(_ao, "LOCK_PATH", tmp_path / "test_agent_scheduler.lock")


@pytest.fixture
def gmail_not_authorized(monkeypatch):
    """Opt-in (NOT autouse — test_google_auth.py tests the real function
    and would break under a blanket override). J3 wired
    actions/agent_orchestrator.py's buildpro_email_monitor handler to a
    real google_auth.get_credential_status() check and, if authorized, a
    real Gmail API call — this machine genuinely has Gmail OAuth-authorized
    (see the J3 report), so any test that assigns a task to
    buildpro_email_monitor (a SUGGEST-level agent, auto-runs on
    assign_task) needs this to avoid a real network call, unless the test
    is specifically about Gmail's authorized path and mocks
    gmail_integration itself instead."""
    from actions import google_auth as _ga
    monkeypatch.setattr(_ga, "get_credential_status", lambda: {
        "credential_file": "present", "credential_type": "installed",
        "token_cached": False, "authorized": False,
    })


@pytest.fixture(autouse=True)
def _isolate_audit_log_db(monkeypatch, tmp_path):
    # actions/audit_log.py (J3 Part 18) is written to from
    # core/headless/tool_executor.py on every consequential action (gmail
    # send, calendar create/update, airtable/hubspot writes, buffer
    # publish, twilio send/call) — isolate it the same way the agent
    # orchestrator DB is isolated, so gmail/calendar/etc. tool tests don't
    # write real rows into data/jarvis2.db.
    from actions import audit_log as _al
    monkeypatch.setattr(_al, "DB_PATH", tmp_path / "test_audit_log.db")


@pytest.fixture(autouse=True)
def _isolate_app_instance_lock(monkeypatch, tmp_path):
    # core/startup.py's single-instance app lock (see docs/WINDOWS_RUNBOOK.md)
    # defaults to data/jarvis_app.lock — isolate it the same way the
    # scheduler lock above is isolated, so no test ever reads/writes the
    # real repo's lock file or leaks lock state between tests.
    monkeypatch.setattr(_startup, "APP_LOCK_PATH", tmp_path / "test_jarvis_app.lock")


@pytest.fixture(autouse=True)
def _dashboard_api_token(monkeypatch):
    monkeypatch.setattr(_headless_config, "API_TOKEN", DASHBOARD_TEST_TOKEN)


@pytest.fixture(autouse=True)
def _isolate_lifecycle_logger(monkeypatch, tmp_path):
    # core/startup.py's get_logger() calls logging.getLogger("jarvis.lifecycle"),
    # which — unlike a module-level variable — is a process-wide singleton
    # keyed by name in Python's own logging registry: it survives across
    # tests no matter what module attribute gets monkeypatched. Without
    # this fixture, the FIRST test to log anything would permanently wire
    # that singleton's handler to whatever LOG_PATH was active at the
    # time (real repo path or a since-deleted tmp_path from an earlier
    # test), and every later test's log calls would silently go there
    # instead of wherever that later test expects. Resetting _logger to
    # None plus pointing LOG_DIR/LOG_PATH at this test's own tmp_path
    # forces a fresh, correctly-pathed handler on first use each test;
    # closing and removing handlers on teardown prevents both file-handle
    # leaks and stale handlers pointed at an already-deleted tmp_path.
    monkeypatch.setattr(_startup, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(_startup, "LOG_PATH", tmp_path / "logs" / "jarvis.log")
    monkeypatch.setattr(_startup, "_logger", None)
    yield
    # Only remove OUR RotatingFileHandler — pytest's own log-capturing
    # plugin also attaches handlers directly to this named logger, and
    # those must be left alone (they're pytest's to manage, not ours).
    logger = logging.getLogger("jarvis.lifecycle")
    for handler in list(logger.handlers):
        if isinstance(handler, RotatingFileHandler):
            handler.close()
            logger.removeHandler(handler)
