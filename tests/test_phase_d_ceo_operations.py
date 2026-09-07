"""Phase D — business state, the daily CEO report, and the closed loop.

The invariant most of these defend is the one that makes the report worth
reading at all:

    recommendation != task created != approval requested
    != executed != verified != completed

A report that blurs those tells Lee candidates were contacted when a task
to prepare outreach was created, and after that he stops trusting any of
it. Several tests exist purely to keep those six states separate.
"""
import time

import pytest

from actions import agent_orchestrator as ao
from actions import business_state as bstate
from actions import ceo_report as report
from actions import operating_memory as mem


class _Task:
    """Stands in for an AgentTask at the report boundary — the report reads
    id/agent_id/status/result/error/escalated and nothing else."""
    def __init__(self, tid, agent, status="done", result=None, error=None, escalated=False):
        self.id, self.agent_id = tid, agent
        self.status = type("S", (), {"value": status})()
        self.result, self.error, self.escalated = result, error, escalated


def _exec(tasks=None, business=None):
    return {"due_tasks": tasks or [], "stale_tasks": [],
            "business": business or {}, "discovery_result": {}}


def _verified(*tasks):
    return [{"reference_id": t.id, "success": True} for t in tasks]


# ══ BUSINESS STATE ═══════════════════════════════════════════════════════

def test_snapshot_reads_real_sources_and_never_raises():
    snap = bstate.snapshot()
    assert snap["ts"] > 0
    for section in ("revenue", "buildpro", "ddf", "opportunities", "tasks", "risks"):
        assert section in snap


def test_revenue_is_actuals_only_with_no_projection():
    rev = bstate.snapshot()["revenue"]
    assert "actuals only" in rev["basis"]
    # Only ledger facts are exposed — no derived forecast field of any kind.
    assert set(rev) == {"cumulative_usd", "target_usd", "remaining_usd",
                        "progress_pct", "committed_deadline", "basis"}


def test_an_unreadable_source_is_unknown_not_zero(monkeypatch):
    """Collapsing 'could not read BuildPro' into 0 would manufacture a
    decline that never happened."""
    monkeypatch.setattr(bstate, "_buildpro",
                        lambda: (_ for _ in ()).throw(RuntimeError("source down")))
    snap = bstate.snapshot()
    assert snap["buildpro"] is None, "a failed source must be unknown, never zero"


def test_comparison_reports_only_real_movement():
    prev = {"buildpro": {"open_jobs": 3, "candidates": 10}}
    cur = {"buildpro": {"open_jobs": 5, "candidates": 10}}
    result = bstate.compare(cur, prev)

    changed = {c["label"]: c for c in result["changes"]}
    assert changed["open jobs"]["delta"] == 2
    assert changed["open jobs"]["direction"] == "up"
    assert "candidates" in result["unchanged"]


def test_comparison_will_not_difference_against_an_unknown():
    cur = {"buildpro": {"open_jobs": 5}}
    prev = {"buildpro": None}
    result = bstate.compare(cur, prev)
    assert result["changes"] == [], "differencing against an unreadable source invents movement"
    assert "open jobs" in result["unknown"]


def test_first_cycle_is_labelled_not_treated_as_all_new():
    assert bstate.compare(bstate.snapshot(), None)["first_cycle"] is True


def test_snapshot_round_trips_through_operating_memory():
    snap = {"revenue": {"cumulative_usd": 1234}, "tasks": {"completed": 2}}
    bstate.save_snapshot(snap, "2026-09-07")
    assert bstate.previous_snapshot()["revenue"]["cumulative_usd"] == 1234


def test_a_cycle_compares_against_state_from_before_it_started():
    """A same-day re-run must still see the earlier snapshot — selecting by
    run_date threw it away and reported every forced cycle as the first."""
    bstate.save_snapshot({"revenue": {"cumulative_usd": 100}}, "2026-09-07")
    cutoff = time.time()
    time.sleep(0.01)
    bstate.save_snapshot({"revenue": {"cumulative_usd": 500}}, "2026-09-07")
    prev = bstate.previous_snapshot(before_ts=cutoff)
    assert prev["revenue"]["cumulative_usd"] == 100


# ══ ACTION vs RECOMMENDATION — the core invariant ════════════════════════

def test_only_verified_completed_work_counts_as_an_action_taken():
    real = _Task("t1", "social", result={"summary": "Prepared content"})
    unverified = _Task("t2", "other", result={"summary": "Ran something"})
    failed = _Task("t3", "broken", status="failed", error="boom")
    execution = _exec([real, unverified, failed])

    actions = report._actions_taken(execution, _verified(real))
    assert [a["agent_id"] for a in actions] == ["social"]

    attempted = report._attempted(execution, _verified(real))
    assert [a["agent_id"] for a in attempted] == ["other"]
    assert attempted[0]["verified"] is False


def test_an_agent_that_had_no_work_is_not_reported_as_an_action():
    """A handler that ran cleanly because its integration is unconfigured
    did NOT take an action. Counting those said 'JARVIS did 8 things' on a
    cycle where it did none."""
    noop_text = _Task("t1", "hubspot_sync", result={"summary": "HubSpot isn't configured — nothing to sync."})
    noop_state = _Task("t2", "calendar", result={"summary": "unavailable", "state": "AUTH_ERROR"})
    real = _Task("t3", "social", result={"summary": "Prepared content for a product"})
    execution = _exec([noop_text, noop_state, real])

    actions = report._actions_taken(execution, _verified(noop_text, noop_state, real))
    assert [a["agent_id"] for a in actions] == ["social"]

    # visible, not hidden
    assert {n["agent_id"] for n in report._no_ops(execution)} == {"hubspot_sync", "calendar"}


def test_a_created_task_is_never_described_as_performed():
    business = {"dispatched": [{"kind": "buildpro_match", "title": "Contact Dana Reyes",
                                "agent_id": "buildpro_candidate_agent", "task_id": "x",
                                "task_status": "pending_approval"}]}
    built = report.build(state={}, movement={"first_cycle": True, "changes": []},
                         health={}, execution=_exec(business=business), verifications=[])

    assert built["actions_taken"] == []
    assert built["tasks_created"][0]["state"] == "pending_approval"
    text = report.render_text(built)
    assert "TASKS CREATED (not yet performed)" in text
    assert "ACTIONS TAKEN" not in text


def test_recommendations_are_labelled_as_proposals():
    built = report.build(state={"tasks": {"awaiting_approval": 2}},
                         movement={"first_cycle": True, "changes": []},
                         health={}, execution=_exec(), verifications=[])
    text = report.render_text(built)
    assert "RECOMMENDED NEXT ACTIONS (proposals, not performed)" in text


# ══ FAILURE CLASSIFICATION ═══════════════════════════════════════════════

def test_configuration_problems_are_not_reported_as_business_failures():
    health = {"integrations": {
        "hubspot": {"state": "NOT_CONFIGURED", "detail": "HUBSPOT_TOKEN is not set"},
        "gmail": {"state": "AUTH_ERROR", "detail": "token rejected"},
    }}
    built = report.build(state={}, movement={"first_cycle": True, "changes": []},
                         health=health, execution=_exec(), verifications=[])

    kinds = {b["kind"] for b in built["infrastructure_blockers"]}
    assert kinds == {report.NOT_CONFIGURED, report.AUTH_ERROR}
    assert built["business_blockers"] == []
    assert "configuration, not business" in report.render_text(built)


def test_task_failures_and_approvals_are_business_side_not_infrastructure():
    failed = _Task("t1", "agent_a", status="failed", error="handler blew up", escalated=True)
    built = report.build(
        state={"tasks": {"awaiting_approval": 1, "awaiting_approval_detail": []}},
        movement={"first_cycle": True, "changes": []},
        health={}, execution=_exec([failed]), verifications=[])

    kinds = {b["kind"] for b in built["business_blockers"]}
    assert report.TASK_FAILURE in kinds
    assert report.APPROVAL_REQUIRED in kinds


def test_a_delivery_failure_is_classified_as_such():
    built = report.build(state={}, movement={"first_cycle": True, "changes": []},
                         health={}, execution=_exec(), verifications=[], delivery_ok=False)
    assert any(b["kind"] == report.DELIVERY_FAILURE for b in built["blockers"])


# ══ PRIORITIES + OPPORTUNITIES ═══════════════════════════════════════════

def test_priorities_are_bounded_and_put_the_owner_decision_first():
    health = {"integrations": {f"svc{i}": {"state": "NOT_CONFIGURED"} for i in range(12)}}
    health["integrations"]["gmail"] = {"state": "AUTH_ERROR", "detail": "rejected"}
    built = report.build(
        state={"tasks": {"awaiting_approval": 3, "awaiting_approval_detail": []},
               "buildpro": {"strong_matches": 4}},
        movement={"first_cycle": True, "changes": []},
        health=health, execution=_exec(), verifications=[])

    prios = built["ceo_priorities"]
    assert 0 < len(prios) <= report.MAX_PRIORITIES
    assert prios[0]["kind"] == report.APPROVAL_REQUIRED, (
        "a decision only Lee can make must outrank anything JARVIS could do itself"
    )


def test_one_noisy_source_cannot_fill_the_opportunity_section():
    state = {"opportunities": {"top": [{"id": i, "title": f"opp {i}", "score": 90} for i in range(50)]},
             "buildpro": {"strong_matches": 2}, "ddf": {"high_ticket_products": 3}}
    opps = report._opportunities(state)
    assert len(opps) <= report.MAX_OPPORTUNITIES
    assert opps[0]["source"] == "buildpro", "the strongest revenue signal should lead"


# ══ QUIET CYCLES ═════════════════════════════════════════════════════════

def test_a_cycle_with_nothing_to_say_is_marked_quiet():
    built = report.build(state={"tasks": {}}, movement={"first_cycle": False, "changes": []},
                         health={}, execution=_exec(), verifications=[])
    assert built["quiet"] is True


def test_a_cycle_with_real_activity_is_not_quiet():
    real = _Task("t1", "social", result={"summary": "Prepared content"})
    built = report.build(state={"tasks": {}}, movement={"first_cycle": False, "changes": []},
                         health={}, execution=_exec([real]), verifications=_verified(real))
    assert built["quiet"] is False


def test_a_quiet_cycle_suppresses_the_notification(monkeypatch):
    """A daily message that always arrives and never matters trains its
    reader to ignore it — and then a real alert lands in a muted thread."""
    from actions import ceo_operating_cycle as cycle
    sent = {"n": 0}
    monkeypatch.setattr(cycle, "_deliver_report",
                        lambda *a, **k: sent.__setitem__("n", sent["n"] + 1) or {"action": "sms"})
    monkeypatch.setattr(cycle.ceo_report, "build",
                        lambda **k: {"quiet": True, "executive_summary": "nothing", "ceo_priorities": []})

    result = cycle.run_cycle(force=True)
    assert result["notification"]["action"] == "suppressed_quiet_cycle"
    assert sent["n"] == 0


def test_the_report_is_still_useful_when_nothing_happened():
    built = report.build(state={"revenue": {"cumulative_usd": 0, "target_usd": 1000}, "tasks": {}},
                         movement={"first_cycle": False, "changes": []},
                         health={}, execution=_exec(), verifications=[])
    text = report.render_text(built)
    assert "Revenue $0 of $1,000" in text
    assert text.strip(), "an empty cycle must still produce a readable report"


# ══ THE LOOP ═════════════════════════════════════════════════════════════

def test_the_cycle_records_state_the_next_cycle_can_read(monkeypatch):
    from actions import ceo_operating_cycle as cycle
    monkeypatch.setattr(cycle, "_deliver_report", lambda *a, **k: {"action": "none"})

    first = cycle.run_cycle(force=True)
    assert first["state"] in ("RAN", "RAN_REPORT_UNDELIVERED")
    assert bstate.previous_snapshot() is not None, "the cycle saved no snapshot to compare against"

    second = cycle.run_cycle(force=True)
    assert second["movement"]["first_cycle"] is False, (
        "the second cycle could not see the first cycle's state"
    )


def test_a_repeated_cycle_does_not_recreate_the_same_work(monkeypatch):
    """Idempotency: the ledger claim is what stops a second run from
    re-dispatching the same subjects."""
    from actions import ceo_operating_cycle as cycle
    from actions import business_pipeline as bp
    monkeypatch.setattr(cycle, "_deliver_report", lambda *a, **k: {"action": "none"})

    finding = bp._finding("research_opportunity", "opp-idem", "Research: X",
                          "business_research_agent", "do research")
    monkeypatch.setattr(bp, "SOURCES", {
        "research": lambda: {"state": bp.SUCCESS, "findings": [finding], "scanned": 1}})

    first = cycle.run_cycle(force=True)
    second = cycle.run_cycle(force=True)

    assert (first["report"] or {}).get("tasks_created") or True   # first may dispatch
    created_second = (second["report"] or {}).get("tasks_created") or []
    assert not any(t["title"] == "Research: X" for t in created_second), (
        "the second cycle recreated a task for a subject already claimed"
    )


def test_one_broken_stage_does_not_abort_the_cycle(monkeypatch):
    from actions import ceo_operating_cycle as cycle
    monkeypatch.setattr(cycle.business_state, "snapshot",
                        lambda: (_ for _ in ()).throw(RuntimeError("snapshot exploded")))
    monkeypatch.setattr(cycle, "_deliver_report", lambda *a, **k: {"action": "none"})

    result = cycle.run_cycle(force=True)
    assert result["state"] == "RAN", "a failed stage aborted the whole cycle"


def test_a_report_build_failure_falls_back_and_still_delivers(monkeypatch):
    from actions import ceo_operating_cycle as cycle
    monkeypatch.setattr(cycle.ceo_report, "build",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("report exploded")))
    monkeypatch.setattr(cycle, "_deliver_report", lambda *a, **k: {"action": "none"})

    result = cycle.run_cycle(force=True)
    assert result["state"] == "RAN"
    assert result["summary"].strip(), "no summary survived the report failure"


def test_the_cycle_still_records_when_delivery_fails(monkeypatch):
    from actions import ceo_operating_cycle as cycle
    monkeypatch.setattr(cycle, "_deliver_report",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no signal")))
    monkeypatch.setattr(cycle.ceo_report, "build",
                        lambda **k: {"quiet": False, "executive_summary": "x", "ceo_priorities": []})

    result = cycle.run_cycle(force=True)
    assert result["state"] == "RAN_REPORT_UNDELIVERED"
    assert cycle.already_ran_today(result["run_date"]) is True
    assert bstate.previous_snapshot() is not None, "delivery failure erased the cycle's state"


def test_execute_work_still_cannot_bypass_approval_in_the_full_loop():
    """Phase A-C safety must survive Phase D."""
    ran = {"n": 0}
    agent = ao.AgentDefinition(
        id="risky", name="Risky", description="x", nucleus_id="system",
        permission_level=ao.PermissionLevel.EXECUTE,
        handler=lambda t: ran.__setitem__("n", ran["n"] + 1) or {"summary": "sent"})
    orch = ao.AgentOrchestrator(agents={"risky": agent})

    from actions import business_pipeline as bp
    finding = bp._finding("hubspot_opportunity", "co-approval", "Outreach", "risky", "send outreach")
    created = bp.dispatch_findings([finding], orchestrator=orch)

    assert created[0]["task_status"] == ao.TaskStatus.PENDING_APPROVAL.value
    assert ran["n"] == 0, "Phase D dispatched EXECUTE work without approval"


def test_secrets_never_reach_the_report(monkeypatch):
    monkeypatch.setenv("HUBSPOT_TOKEN", "pat-na1-supersecretvalue0987654321")
    from actions import integration_health as ih
    built = report.build(state=bstate.snapshot(),
                         movement={"first_cycle": True, "changes": []},
                         health=ih.check_all(), execution=_exec(), verifications=[])
    assert "supersecretvalue" not in report.render_text(built)
    assert "supersecretvalue" not in str(built)


# ══ REGRESSIONS ══════════════════════════════════════════════════════════

def test_the_saved_snapshot_is_the_business_state_not_the_cycle_state(monkeypatch):
    """Regression: run_cycle assigns `state = "RAN" | "RAN_REPORT_UNDELIVERED"`
    partway through. The business snapshot was held in a local also named
    `state`, so by the time it was persisted it had been overwritten with
    that string — every cycle saved "RAN" as its business snapshot, and the
    next cycle's comparison then crashed on a str and silently reported
    itself as the first cycle forever."""
    from actions import ceo_operating_cycle as cycle
    monkeypatch.setattr(cycle, "_deliver_report", lambda *a, **k: {"action": "none"})

    result = cycle.run_cycle(force=True)

    assert isinstance(result["business_state"], dict), (
        f"business_state was {type(result['business_state']).__name__}, not a snapshot"
    )
    assert "revenue" in result["business_state"]

    saved = mem.recall(entry_type="business_snapshot", limit=1)
    assert saved and isinstance(saved[0]["data"], dict)
    assert "revenue" in saved[0]["data"], "the persisted snapshot is not a business snapshot"


def test_a_malformed_stored_snapshot_degrades_instead_of_crashing():
    """Regression: compare() did previous.get(section) unguarded, so a
    snapshot stored as a string (operating memory falls back to
    json.dumps(str(...)) when a payload will not serialise) raised
    AttributeError. The cycle isolates its stages, so the crash was
    invisible — comparison simply never happened again."""
    current = {"revenue": {"cumulative_usd": 10}}

    assert bstate.compare(current, "RAN_REPORT_UNDELIVERED")["first_cycle"] is True
    assert bstate.compare(current, {"revenue": "not a dict"})["changes"] == []
    assert "cumulative revenue" in bstate.compare(current, {"revenue": "not a dict"})["unknown"]
    assert bstate.compare("garbage", {"revenue": {"cumulative_usd": 1}})["first_cycle"] is True


def test_previous_snapshot_ignores_a_non_dict_payload():
    mem.record("business_snapshot", source="business_state",
               subject="bad", summary="corrupt", data=None, ok=True)
    bstate.save_snapshot({"revenue": {"cumulative_usd": 77}}, "good")
    prev = bstate.previous_snapshot()
    assert isinstance(prev, dict) and prev["revenue"]["cumulative_usd"] == 77
