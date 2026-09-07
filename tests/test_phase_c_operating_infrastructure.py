"""Phase C — memory, Brain, notifications, approval gate, self-healing and
integration health, and the seams where they meet.

Every test drives real code. External transports (Twilio via the notifier,
the integrations' own HTTP calls) are faked at their own boundary; nothing
stubs the Phase C module under test and then asserts the stub was called.

Two properties recur because they are the ones that matter for an
unattended system: a subsystem failing must never fail the business work
that used it, and nothing here may weaken the approval gate.
"""
import time

import pytest

from actions import agent_orchestrator as ao
from actions import integration_health as ih
from actions import jarvis_brain as brain
from actions import notifications as notif
from actions import operating_memory as mem
from actions import self_healing as heal


def _agent(aid="a1", perm=ao.PermissionLevel.OBSERVE, handler=None):
    return ao.AgentDefinition(
        id=aid, name=f"Agent {aid}", description="x", nucleus_id="system",
        permission_level=perm, handler=handler or (lambda t: {"summary": "ok"}))


# ══ 1. MEMORY ════════════════════════════════════════════════════════════

def test_operating_memory_persists_and_recalls_with_metadata():
    mem.record(mem.CYCLE_RUN, source="ceo", summary="ran a cycle",
               subject="2026-09-07", data={"agents_run": 4}, ok=True)
    entries = mem.recall(entry_type=mem.CYCLE_RUN, limit=5)
    assert entries and entries[0]["source"] == "ceo"
    assert entries[0]["data"]["agents_run"] == 4
    assert entries[0]["ok"] is True
    assert entries[0]["ts"] > 0


def test_operating_memory_deduplicates_only_when_asked():
    first = mem.record(mem.INTEGRATION_STATE, "gmail", "auth error",
                       subject="gmail", dedup_seconds=3600)
    second = mem.record(mem.INTEGRATION_STATE, "gmail", "auth error",
                        subject="gmail", dedup_seconds=3600)
    assert first["recorded"] is True
    assert second["recorded"] is False and second["reason"] == "deduplicated"

    # Off by default: collapsing genuinely repeated events is how a
    # worsening problem gets hidden.
    assert mem.record(mem.INTEGRATION_STATE, "gmail", "auth error", subject="gmail")["recorded"] is True


def test_operating_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(mem, "MAX_ENTRIES_PER_TYPE", 5)
    for i in range(20):
        mem.record(mem.AGENT_OUTCOME, source="noisy", summary=f"run {i}", ok=True)
    assert len(mem.recall(entry_type=mem.AGENT_OUTCOME, limit=100)) <= 5


def test_a_memory_write_failure_never_breaks_the_caller(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("disk gone")
    monkeypatch.setattr(mem, "_connect", _boom)

    assert mem.record(mem.CYCLE_RUN, "ceo", "x") == {"recorded": False, "reason": "write_failed"}
    assert mem.recall() == []          # retrieval degrades to "no history"
    assert mem.failure_streak("any") == 0
    assert mem.summarize()["total"] == 0


def test_failure_streak_resets_on_a_success():
    mem.record(mem.AGENT_OUTCOME, "agent_z", "fail", ok=False)
    mem.record(mem.AGENT_OUTCOME, "agent_z", "fail", ok=False)
    assert mem.failure_streak("agent_z") == 2
    mem.record(mem.AGENT_OUTCOME, "agent_z", "worked", ok=True)
    assert mem.failure_streak("agent_z") == 0, "a success must break the streak"


# ══ 2. JARVIS BRAIN ══════════════════════════════════════════════════════

def test_brain_is_reachable_from_the_headless_autonomous_path():
    """The vault ships in-repo and config defaults to it, so an autonomous
    run with no environment variable set must still find it."""
    brain.reset_cache()
    assert brain.is_available() is True
    assert brain.find_notes("CEO operating mandate"), "no notes found for a core operating topic"


def test_brain_answers_descriptive_queries_not_just_exact_substrings():
    """ObsidianVault.search_notes is a whole-string substring match, so a
    descriptive query matches nothing. The autonomous path must still be
    able to ask by topic."""
    brain.reset_cache()
    assert brain.find_notes("CEO operating mandate priorities"), (
        "term-based fallback did not fire for a multi-word query"
    )
    ctx = brain.operating_context(["mandate", "approval"])
    assert set(ctx) == {"mandate", "approval"}
    assert all(v.strip() for v in ctx.values())


def test_brain_retrieval_is_budgeted_not_a_whole_vault_dump():
    brain.reset_cache()
    text = brain.recall("approval", max_chars=400)
    assert 0 < len(text) <= 400


def test_brain_degrades_gracefully_when_the_vault_is_missing(monkeypatch, tmp_path):
    from core.headless import obsidian
    brain.reset_cache()
    monkeypatch.setattr(obsidian.config, "OBSIDIAN_VAULT_PATH", str(tmp_path / "nope"))
    try:
        assert brain.recall("anything") == ""
        assert brain.find_notes("anything") == []
        assert brain.read("x.md") is None
        assert brain.operating_context() == {}
    finally:
        brain.reset_cache()


# ══ 3. NOTIFICATIONS ═════════════════════════════════════════════════════

def _capture_notifier(monkeypatch, result=None, raises=False):
    sent = []

    def _fake(event_id, title, detail="", level=2, dry_run=False):
        if raises:
            raise RuntimeError("twilio unreachable")
        sent.append({"event_id": event_id, "title": title, "level": level})
        return result or {"event_id": event_id, "action": "sms", "ok": True, "level": level}

    from actions import approval_notifier
    monkeypatch.setattr(approval_notifier, "notify_urgent_event", _fake)
    return sent


def test_message_type_decides_severity_not_the_call_site(monkeypatch):
    sent = _capture_notifier(monkeypatch)
    notif.info("e1", "fyi")
    notif.business_alert("e2", "signal")
    notif.urgent("e3", "now")
    levels = {s["title"]: s["level"] for s in sent}
    assert levels["fyi"] == 1, "informational messages must not text"
    assert levels["signal"] == 2
    assert levels["URGENT: now"] == 3


def test_an_approval_request_is_visibly_different_and_carries_context(monkeypatch):
    sent = []
    from actions import approval_notifier

    def _fake(event_id, title, detail="", level=2, dry_run=False):
        sent.append({"title": title, "detail": detail, "event_id": event_id})
        return {"action": "sms", "ok": True}

    monkeypatch.setattr(approval_notifier, "notify_urgent_event", _fake)
    notif.approval_request("task-9", "BuildPro Responder", "Email 3 candidates", why="strong matches")

    body = sent[0]
    assert body["title"].startswith("APPROVAL NEEDED: ")
    assert "Email 3 candidates" in body["detail"]
    assert "BuildPro Responder" in body["detail"]
    assert "task-9" in body["detail"]
    assert "will NOT run until you approve" in body["detail"]


def test_delivery_failure_is_reported_and_recorded_never_retried(monkeypatch):
    calls = {"n": 0}
    from actions import approval_notifier

    def _fake(**kwargs):
        calls["n"] += 1
        raise RuntimeError("twilio unreachable")

    monkeypatch.setattr(approval_notifier, "notify_urgent_event", _fake)
    result = notif.urgent("e-fail", "something broke")

    assert result["ok"] is False and "twilio" in result["error"]
    assert calls["n"] == 1, "re-sending an SMS is a real side effect and must not be retried here"
    assert notif.recent_failures(), "an undelivered notification must be observable"


def test_notification_delivery_uses_a_stable_event_id_for_dedup(monkeypatch):
    sent = _capture_notifier(monkeypatch)
    notif.daily_report("ceo_cycle-2026-09-07", "Brief", "body")
    notif.daily_report("ceo_cycle-2026-09-07", "Brief", "body")
    assert {s["event_id"] for s in sent} == {"ceo_cycle-2026-09-07"}


# ══ 4. APPROVAL GATE ═════════════════════════════════════════════════════

def test_execute_work_cannot_bypass_the_gate_however_it_is_triggered():
    ran = {"n": 0}
    orch = ao.AgentOrchestrator(agents={"x": _agent(
        "x", ao.PermissionLevel.EXECUTE, lambda t: ran.__setitem__("n", ran["n"] + 1) or {"summary": "sent"})})

    task = orch.assign_task("x", "send outreach")
    assert task.status == ao.TaskStatus.PENDING_APPROVAL
    assert ran["n"] == 0

    with pytest.raises(PermissionError):
        orch.run_task(task.id)
    assert ran["n"] == 0

    # the autonomous sweeps must not pick it up either
    orch.run_due_agents()
    orch.run_stale_autonomous_agents()
    assert ran["n"] == 0, "an autonomous sweep executed EXECUTE-level work without approval"

    assert orch.approve_task(task.id).status == ao.TaskStatus.DONE
    assert ran["n"] == 1


def test_approving_twice_does_not_execute_twice():
    ran = {"n": 0}
    orch = ao.AgentOrchestrator(agents={"x": _agent(
        "x", ao.PermissionLevel.EXECUTE, lambda t: ran.__setitem__("n", ran["n"] + 1) or {"summary": "sent"})})
    task = orch.assign_task("x", "send outreach")
    orch.approve_task(task.id)
    orch.approve_task(task.id)
    assert ran["n"] == 1, "a replayed approval re-ran an irreversible action"


def test_an_expired_approval_cannot_be_resurrected():
    """Silence is not consent, and an approval nobody gave in time must not
    act on a situation that has since changed."""
    ran = {"n": 0}
    orch = ao.AgentOrchestrator(agents={"x": _agent(
        "x", ao.PermissionLevel.EXECUTE, lambda t: ran.__setitem__("n", ran["n"] + 1) or {"summary": "sent"})})
    task = orch.assign_task("x", "send outreach")
    orch._tasks[task.id].created_ts = time.time() - (ao.APPROVAL_TTL_SECONDS + 60)

    expired = orch.expire_stale_approvals()
    assert [t.id for t in expired] == [task.id]
    assert orch.approve_task(task.id).status == ao.TaskStatus.EXPIRED
    assert ran["n"] == 0


def test_a_cancelled_request_cannot_be_approved():
    orch = ao.AgentOrchestrator(agents={"x": _agent("x", ao.PermissionLevel.EXECUTE)})
    task = orch.assign_task("x", "send outreach")
    assert orch.cancel_task(task.id, "superseded").status == ao.TaskStatus.CANCELLED
    assert orch.approve_task(task.id).status == ao.TaskStatus.CANCELLED


def test_a_failed_approval_notification_leaves_the_task_awaiting_approval(monkeypatch):
    """If the request never reached Lee, the safe state is 'still waiting' —
    never 'assume approved'."""
    from actions import approval_notifier
    monkeypatch.setattr(approval_notifier, "notify_urgent_event",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("no signal")))

    orch = ao.AgentOrchestrator(agents={"x": _agent("x", ao.PermissionLevel.EXECUTE)})
    task = orch.assign_task("x", "send outreach")
    result = orch.request_approval(task.id)

    assert result["ok"] is False
    assert orch.get_task(task.id).status == ao.TaskStatus.PENDING_APPROVAL


def test_approval_state_is_persisted():
    orch = ao.AgentOrchestrator(agents={"x": _agent("x", ao.PermissionLevel.EXECUTE)})
    task = orch.assign_task("x", "send outreach")
    reloaded = ao._load_tasks()[task.id]
    assert reloaded.status == ao.TaskStatus.PENDING_APPROVAL


# ══ 5. SELF-HEALING ══════════════════════════════════════════════════════

def test_a_task_wedged_in_running_is_recovered_and_frees_its_agent():
    """A stuck task holds its agent out of get_due_agents() forever, which
    silently removes that agent from the workforce with no error anywhere."""
    orch = ao.AgentOrchestrator(agents={"x": _agent("x")})
    task = orch.assign_task("x", "work")
    task.status = ao.TaskStatus.RUNNING
    task.started_ts = time.time() - (heal.STALE_TASK_SECONDS + 600)
    ao._save_task(task)
    orch._agents["x"].status = ao.AgentStatus.RUNNING

    recovered = heal.recover_stale_tasks(orchestrator=orch)

    assert [r["task_id"] for r in recovered] == [task.id]
    assert orch.get_task(task.id).status == ao.TaskStatus.FAILED
    assert orch.get_task(task.id).escalated is True
    assert orch._agents["x"].status == ao.AgentStatus.IDLE


def test_a_recently_started_task_is_left_alone():
    orch = ao.AgentOrchestrator(agents={"x": _agent("x")})
    task = orch.assign_task("x", "work")
    task.status = ao.TaskStatus.RUNNING
    task.started_ts = time.time() - 30
    ao._save_task(task)
    assert heal.recover_stale_tasks(orchestrator=orch) == []


def test_repeated_agent_failures_escalate_once_per_cooldown(monkeypatch):
    sent = _capture_notifier(monkeypatch)
    orch = ao.AgentOrchestrator(agents={"x": _agent("x")})
    for _ in range(heal.FAILURE_STREAK_ESCALATE):
        mem.record(mem.AGENT_OUTCOME, source="x", summary="failed", ok=False)

    broken = heal.check_agent_failures(orchestrator=orch)
    assert [b["agent_id"] for b in broken] == ["x"]
    assert len(sent) == 1

    heal.check_agent_failures(orchestrator=orch)
    assert len(sent) == 1, "escalation must be rate-limited, not repeated every sweep"


def test_a_single_agent_failure_does_not_escalate(monkeypatch):
    sent = _capture_notifier(monkeypatch)
    orch = ao.AgentOrchestrator(agents={"x": _agent("x")})
    mem.record(mem.AGENT_OUTCOME, source="x", summary="failed", ok=False)
    assert heal.check_agent_failures(orchestrator=orch) == []
    assert sent == []


def test_an_unconfigured_integration_is_never_escalated(monkeypatch):
    """Paging daily about a credential nobody has connected is how real
    alerts get ignored."""
    sent = _capture_notifier(monkeypatch)
    monkeypatch.setattr(ih, "PROBES", {
        "thing": lambda: {"state": ih.NOT_CONFIGURED, "detail": "no key set"}})
    heal.check_integrations()
    assert sent == [], "NOT_CONFIGURED must not page anyone"


def test_an_integration_that_stopped_authenticating_does_escalate(monkeypatch):
    sent = _capture_notifier(monkeypatch)
    monkeypatch.setattr(ih, "PROBES", {
        "thing": lambda: {"state": ih.AUTH_ERROR, "detail": "token rejected"}})
    heal.check_integrations()
    assert len(sent) == 1 and "thing" in sent[0]["title"]


def test_one_failing_check_does_not_stop_the_sweep(monkeypatch):
    monkeypatch.setattr(heal, "check_integrations",
                        lambda: (_ for _ in ()).throw(RuntimeError("probe exploded")))
    report = heal.run_sweep()
    assert "integrations" in report["errors"]
    assert "cycle_health" in report["checks"], "a failing check aborted the rest of the sweep"


def test_self_healing_runs_as_a_supervised_background_worker():
    import asyncio
    from core.headless import background as bg

    async def _run():
        worker = bg.BackgroundWorker()
        worker.start()
        try:
            assert "self_healing" in {t.get_name() for t in worker._tasks}
        finally:
            await worker.stop()

    asyncio.run(_run())


# ══ 6. INTEGRATION HEALTH ════════════════════════════════════════════════

def test_missing_credentials_read_as_not_configured(monkeypatch):
    from actions import hubspot_integration as hs
    monkeypatch.setattr(hs, "is_configured", lambda: False)
    assert ih._probe("hubspot", ih._hubspot)["state"] == ih.NOT_CONFIGURED


def test_a_probe_that_raises_is_isolated_not_fatal():
    result = ih._probe("boom", lambda: (_ for _ in ()).throw(RuntimeError("kaboom")))
    assert result["state"] == ih.UNAVAILABLE
    assert "kaboom" in result["detail"]


def test_capabilities_are_derived_from_real_dependencies():
    integrations = {
        "google": {"state": ih.CONFIGURED},
        "hubspot": {"state": ih.AUTH_ERROR},
        "twilio": {"state": ih.CONFIGURED},
        "owner_phone": {"state": ih.NOT_CONFIGURED},
    }
    caps = ih.available_capabilities(integrations)
    assert caps["email_triage"] is True
    assert caps["crm_operations"] is False
    assert caps["owner_alerts"] is False, "alerts need both Twilio AND a destination number"


def test_secrets_are_redacted_before_they_can_reach_a_report():
    dirty = "auth failed for sk-abcd1234efgh5678 using ACdeadbeefdeadbeefdeadbeefdeadbeef"
    clean = ih.redact(dirty)
    assert "sk-abcd1234efgh5678" not in clean
    assert "ACdeadbeefdeadbeefdeadbeefdeadbeef" not in clean
    assert "[REDACTED]" in clean


def test_health_report_never_contains_a_credential_value(monkeypatch):
    monkeypatch.setenv("HUBSPOT_TOKEN", "pat-na1-supersecrettokenvalue123456")
    report = ih.check_all()
    assert "supersecrettokenvalue" not in str(report)


def test_google_scope_gap_does_not_read_as_broken_auth(monkeypatch):
    """The Phase B fix: a token missing the Tasks scope still authenticates
    Gmail and Calendar. Reporting that as AUTH_ERROR would force a needless
    reauthorization."""
    from actions import google_auth
    monkeypatch.setattr(google_auth, "get_credential_status", lambda: {
        "credential_file": "present", "credential_type": "installed",
        "token_cached": True, "authorized": True,
        "missing_scopes": ["https://www.googleapis.com/auth/tasks"]})

    result = ih._google()
    assert result["state"] == ih.CONFIGURED
    assert result["missing_scopes"] == ["https://www.googleapis.com/auth/tasks"]
    assert "tasks" in result["detail"]


# ══ CROSS-SYSTEM ═════════════════════════════════════════════════════════

def test_ceo_cycle_consults_health_and_brain_and_records_what_it_did(monkeypatch):
    """Brain -> Memory -> CEO cycle -> orchestrator -> notification ->
    memory, in one real run."""
    from actions import ceo_operating_cycle as cycle
    monkeypatch.setattr(cycle, "_deliver_report",
                        lambda run_date, summary: {"action": "none", "configured": False})

    result = cycle.run_cycle(force=True)
    assert result["state"] == "RAN"

    runs = mem.recall(entry_type=mem.CYCLE_RUN, limit=1)
    assert runs and runs[0]["subject"] == result["run_date"], (
        "the cycle did not record itself in operating memory"
    )
    assert "Capabilities unavailable this cycle" in result["summary"] or \
           all((result.get("health") or {}).get("capabilities", {}).values()), (
        "the brief must say which capabilities were unavailable"
    )


def test_a_broken_subsystem_does_not_stop_the_cycle(monkeypatch):
    from actions import ceo_operating_cycle as cycle
    monkeypatch.setattr(cycle.integration_health, "check_all",
                        lambda: (_ for _ in ()).throw(RuntimeError("health probe exploded")))
    monkeypatch.setattr(cycle.jarvis_brain, "operating_context",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("vault on fire")))
    monkeypatch.setattr(cycle, "_deliver_report",
                        lambda run_date, summary: {"action": "none", "configured": False})

    assert cycle.run_cycle(force=True)["state"] == "RAN"
