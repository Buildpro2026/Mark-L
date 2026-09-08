"""The connections between JARVIS's integrations and his decisions.

Every integration worked and none of them reached the decision.
get_calendar_snapshot() had computed real scheduling conflicts since it
was written and nothing in the priority path called it, so a
double-booking at 09:00 could not become a priority at 08:00. Overdue
Google Tasks were in the same position. And record_inbound_sms() stored
Lee's replies while nothing read them, so the approval round trip stopped
at "SMS sent".
"""
from datetime import datetime, timedelta, timezone

import pytest

from actions import cross_system as cs


def _iso(hours_from_now):
    return (datetime.now(timezone.utc) + timedelta(hours=hours_from_now)).isoformat()


# ══ CALENDAR REACHES THE DECISION ════════════════════════════════════════

def test_a_scheduling_conflict_becomes_a_high_severity_priority(monkeypatch):
    from actions import priorities_engine
    monkeypatch.setattr(priorities_engine, "get_calendar_snapshot", lambda **k: {
        "available": True, "conflicts": [
            {"first": {"id": "e1", "summary": "Client call"},
             "second": {"id": "e2", "summary": "Site visit"}}],
        "next_event": None, "events": []})

    items = cs.calendar_signals()
    assert len(items) == 1
    assert items[0]["kind"] == "calendar_conflict"
    assert items[0]["severity"] == 4, "a double-booking is already wrong, not a suggestion"
    assert "Client call" in items[0]["title"] and "Site visit" in items[0]["title"]


def test_an_imminent_appointment_becomes_a_deadline_signal(monkeypatch):
    from actions import priorities_engine
    monkeypatch.setattr(priorities_engine, "get_calendar_snapshot", lambda **k: {
        "available": True, "conflicts": [],
        "next_event": {"id": "e9", "summary": "Board meeting", "start": _iso(2)}})

    items = cs.calendar_signals()
    assert items and items[0]["kind"] == "calendar_deadline"
    # ceo_decision reads deadline language out of the title, so the
    # urgency signal and the human text must be the same string.
    assert "today" in items[0]["title"].lower()


def test_a_distant_appointment_is_not_urgent(monkeypatch):
    from actions import priorities_engine
    monkeypatch.setattr(priorities_engine, "get_calendar_snapshot", lambda **k: {
        "available": True, "conflicts": [],
        "next_event": {"id": "e9", "summary": "Next week", "start": _iso(72)}})
    assert cs.calendar_signals() == []


def test_an_unavailable_calendar_contributes_nothing_and_does_not_raise(monkeypatch):
    from actions import priorities_engine
    monkeypatch.setattr(priorities_engine, "get_calendar_snapshot", lambda **k: {
        "available": False, "reason": "Calendar not authorized",
        "conflicts": [], "next_event": None})
    assert cs.calendar_signals() == []


# ══ TASKS REACH THE DECISION ═════════════════════════════════════════════

def _tasks(monkeypatch, rows, ok=True):
    from actions import google_tasks_integration as gt
    monkeypatch.setattr(gt, "list_tasks", lambda **k: {"ok": ok, "tasks": rows})


def test_an_overdue_task_becomes_a_priority(monkeypatch):
    _tasks(monkeypatch, [{"id": "t1", "title": "Send the contract",
                          "status": "needsAction", "due": _iso(-30)}])
    items = cs.task_signals()
    assert items and items[0]["kind"] == "task_overdue"
    assert items[0]["severity"] == 4, "more than a day overdue"
    # Time spent overdue is real waiting, and ceo_decision reads this field.
    assert items[0]["waited_hours"] > 24


def test_a_recently_overdue_task_is_ranked_below_a_long_overdue_one(monkeypatch):
    _tasks(monkeypatch, [{"id": "t1", "title": "A", "status": "needsAction", "due": _iso(-2)}])
    assert cs.task_signals()[0]["severity"] == 3


def test_a_task_due_soon_is_surfaced_at_lower_severity(monkeypatch):
    _tasks(monkeypatch, [{"id": "t2", "title": "Prepare brief",
                          "status": "needsAction", "due": _iso(12)}])
    items = cs.task_signals()
    assert items and items[0]["kind"] == "task_due"
    assert items[0]["severity"] == 2


def test_a_task_with_no_due_date_is_not_a_deadline(monkeypatch):
    # An undated task is a list entry, not a deadline; treating it as one
    # fills the priority list with things that were never urgent.
    _tasks(monkeypatch, [{"id": "t3", "title": "Someday", "status": "needsAction",
                          "due": None}])
    assert cs.task_signals() == []


def test_a_completed_task_is_never_a_priority(monkeypatch):
    _tasks(monkeypatch, [{"id": "t4", "title": "Done", "status": "completed",
                          "due": _iso(-100)}])
    assert cs.task_signals() == []


def test_a_far_future_task_is_not_yet_pressure(monkeypatch):
    _tasks(monkeypatch, [{"id": "t5", "title": "Later", "status": "needsAction",
                          "due": _iso(400)}])
    assert cs.task_signals() == []


def test_an_unauthorized_tasks_api_contributes_nothing(monkeypatch):
    _tasks(monkeypatch, [], ok=False)
    assert cs.task_signals() == []


# ══ FAILURE ISOLATION ════════════════════════════════════════════════════

def test_one_broken_collector_does_not_stop_the_others(monkeypatch):
    monkeypatch.setattr(cs, "calendar_signals",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("calendar down")))
    _tasks(monkeypatch, [{"id": "t1", "title": "Overdue", "status": "needsAction",
                          "due": _iso(-5)}])
    out = cs.collect_signals()
    assert out["states"]["calendar"]["state"] == cs.FAILED
    assert out["states"]["google_tasks"]["state"] == cs.OK
    assert any(i["kind"] == "task_overdue" for i in out["items"])


def test_a_healthy_empty_source_is_distinguishable_from_a_broken_one(monkeypatch):
    # "no conflicts today" and "Calendar is down" must not look the same.
    monkeypatch.setattr(cs, "calendar_signals", lambda **k: [])
    monkeypatch.setattr(cs, "task_signals",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = cs.collect_signals()
    assert out["states"]["calendar"]["state"] == cs.OK
    assert out["states"]["google_tasks"]["state"] == cs.FAILED
    assert out["healthy_sources"] == 1


def test_the_priority_list_survives_a_total_signal_failure(monkeypatch):
    from actions import priorities_engine, buildpro_data
    from actions import buildpro_intelligence, executive_brief
    from actions.agent_orchestrator import orchestrator
    monkeypatch.setattr(executive_brief, "_operational_risks", lambda: [])
    monkeypatch.setattr(orchestrator, "list_tasks", lambda **k: [])
    monkeypatch.setattr(buildpro_data, "list_clients_needing_followup", lambda **k: [])
    monkeypatch.setattr(buildpro_intelligence, "generate_morning_report_data",
                        lambda: {"recommended_actions": []})
    monkeypatch.setattr(cs, "collect_signals",
                        lambda: (_ for _ in ()).throw(RuntimeError("all down")))
    assert priorities_engine.get_todays_priorities() == []   # must not raise


def test_signals_actually_reach_the_priority_list(monkeypatch):
    from actions import priorities_engine, buildpro_data
    from actions import buildpro_intelligence, executive_brief
    from actions.agent_orchestrator import orchestrator
    monkeypatch.setattr(executive_brief, "_operational_risks", lambda: [])
    monkeypatch.setattr(orchestrator, "list_tasks", lambda **k: [])
    monkeypatch.setattr(buildpro_data, "list_clients_needing_followup", lambda **k: [])
    monkeypatch.setattr(buildpro_intelligence, "generate_morning_report_data",
                        lambda: {"recommended_actions": []})
    monkeypatch.setattr(cs, "collect_signals", lambda: {
        "items": [{"kind": "calendar_conflict", "severity": 4,
                   "title": "Conflict", "source": "calendar"}],
        "states": {}, "healthy_sources": 1})

    items = priorities_engine.get_todays_priorities()
    assert any(i["kind"] == "calendar_conflict" for i in items)


# ══ THE APPROVAL ROUND TRIP ══════════════════════════════════════════════

@pytest.mark.parametrize("reply", ["yes", "Y", "approve", "OK", "go", "do it", "proceed"])
def test_an_approving_reply_is_understood(reply):
    assert cs.interpret_reply(reply)["decision"] == "approve"


@pytest.mark.parametrize("reply", ["no", "N", "reject", "deny", "stop", "cancel", "don't"])
def test_a_rejecting_reply_is_understood(reply):
    assert cs.interpret_reply(reply)["decision"] == "reject"


@pytest.mark.parametrize("reply", ["", "maybe later", "what was that about?", "👍"])
def test_an_ambiguous_reply_is_never_guessed(reply):
    # Acting on an unclear reply executes something Lee did not authorise.
    assert cs.interpret_reply(reply)["decision"] == "unclear"


def test_a_reply_can_name_the_task_it_answers():
    assert cs.interpret_reply("YES a1b2c3d4")["task_ref"] == "a1b2c3d4"


class _Task:
    def __init__(self, tid, status="pending_approval", agent_id="agent", desc="do a thing"):
        self.id = tid
        self.agent_id = agent_id
        self.description = desc
        self.status = type("S", (), {"value": status})()


def _orchestrator(monkeypatch, tasks, approved=None, rejected=None):
    from actions import agent_orchestrator
    orch = agent_orchestrator.orchestrator
    # `approved or []` would build a NEW list whenever the caller passed an
    # empty one, silently discarding every append — the list has to be used
    # directly.
    approved = [] if approved is None else approved
    rejected = [] if rejected is None else rejected
    monkeypatch.setattr(orch, "list_tasks", lambda **k: list(tasks))
    monkeypatch.setattr(orch, "approve_task",
                        lambda tid: (approved.append(tid), _Task(tid, "approved"))[1])
    monkeypatch.setattr(orch, "reject_task",
                        lambda tid: (rejected.append(tid), _Task(tid, "rejected"))[1])
    return orch


def test_a_yes_approves_the_single_pending_task(monkeypatch):
    approved = []
    _orchestrator(monkeypatch, [_Task("task-1")], approved=approved)
    result = cs.apply_sms_decision("yes")
    assert result["ok"] is True and result["state"] == "APPROVED"
    assert approved == ["task-1"]


def test_a_no_rejects_it(monkeypatch):
    rejected = []
    _orchestrator(monkeypatch, [_Task("task-1")], rejected=rejected)
    result = cs.apply_sms_decision("no")
    assert result["state"] == "REJECTED"
    assert rejected == ["task-1"]


def test_a_bare_yes_with_several_pending_changes_nothing(monkeypatch):
    # Guessing which approval a bare "yes" meant is how the wrong thing
    # gets executed.
    approved = []
    _orchestrator(monkeypatch, [_Task("task-1"), _Task("task-2")], approved=approved)
    result = cs.apply_sms_decision("yes")
    assert result["ok"] is False
    assert result["state"] == "AMBIGUOUS"
    assert result["applied"] is False
    assert approved == []


def test_naming_the_task_resolves_the_ambiguity(monkeypatch):
    approved = []
    _orchestrator(monkeypatch, [_Task("aaa111"), _Task("bbb222")], approved=approved)
    result = cs.apply_sms_decision("yes bbb222")
    assert result["state"] == "APPROVED"
    assert approved == ["bbb222"]


def test_an_unclear_reply_applies_nothing(monkeypatch):
    approved = []
    _orchestrator(monkeypatch, [_Task("task-1")], approved=approved)
    result = cs.apply_sms_decision("hmm not sure")
    assert result["applied"] is False
    assert result["state"] == "UNCLEAR"
    assert approved == []


def test_a_reply_with_nothing_pending_is_reported_honestly(monkeypatch):
    _orchestrator(monkeypatch, [])
    result = cs.apply_sms_decision("yes")
    assert result["state"] == "NOT_FOUND"
    assert result["applied"] is False


def test_a_task_no_longer_awaiting_approval_is_not_touched(monkeypatch):
    approved = []
    _orchestrator(monkeypatch, [_Task("task-1", status="completed")], approved=approved)
    result = cs.apply_sms_decision("yes")
    assert result["state"] == "NOT_FOUND"
    assert approved == []


def test_the_decision_goes_through_the_existing_gate_not_around_it():
    import inspect
    source = inspect.getsource(cs.apply_sms_decision)
    assert "orchestrator.approve_task" in source
    assert "orchestrator.reject_task" in source
    # No direct status writes that would sidestep the gate.
    assert "status =" not in source


def test_an_approval_decision_is_recorded_as_an_outcome(monkeypatch):
    from actions import ceo_decision
    recorded = []
    monkeypatch.setattr(ceo_decision, "record_outcome",
                        lambda item, **k: recorded.append((item, k)) or {"ok": True})
    _orchestrator(monkeypatch, [_Task("task-1")])
    cs.apply_sms_decision("yes")
    assert recorded, "the approval outcome never reached memory"
    assert recorded[0][1]["ok"] is True


def test_a_failing_orchestrator_is_reported_not_swallowed(monkeypatch):
    from actions import agent_orchestrator
    orch = agent_orchestrator.orchestrator
    monkeypatch.setattr(orch, "list_tasks", lambda **k: [_Task("task-1")])
    monkeypatch.setattr(orch, "approve_task",
                        lambda tid: (_ for _ in ()).throw(RuntimeError("db locked")))
    result = cs.apply_sms_decision("yes")
    assert result["ok"] is False and result["state"] == "FAILED"
    assert "locked" in result["detail"]


# ══ RESUME REACHES MATCHING ══════════════════════════════════════════════

def test_a_new_candidate_is_matched_through_the_existing_matcher(monkeypatch):
    from actions import buildpro_matching
    monkeypatch.setattr(buildpro_matching, "generate_matches_for_candidate",
                        lambda cid, **k: [{"job_id": 1, "score": 0.9},
                                          {"job_id": 2, "score": 0.4}])
    out = cs.match_new_candidate(7)
    assert out["ok"] is True
    assert [m["job_id"] for m in out["matches"]] == [1, 2]   # ranked
    assert out["match_count"] == 2


def test_no_matches_is_reported_as_no_matches(monkeypatch):
    from actions import buildpro_matching
    monkeypatch.setattr(buildpro_matching, "generate_matches_for_candidate",
                        lambda cid, **k: [])
    out = cs.match_new_candidate(7)
    assert out["match_count"] == 0
    assert "no job currently matches" in out["detail"]


def test_a_failing_matcher_is_reported_not_faked(monkeypatch):
    from actions import buildpro_matching
    monkeypatch.setattr(buildpro_matching, "generate_matches_for_candidate",
                        lambda cid, **k: (_ for _ in ()).throw(RuntimeError("no jobs table")))
    out = cs.match_new_candidate(7)
    assert out["ok"] is False and out["state"] == cs.FAILED
    assert out["matches"] == []


def test_an_uploaded_resume_reaches_matching(tmp_path, monkeypatch):
    from actions import resume_intake as ri, buildpro_data, gmail_integration
    monkeypatch.setattr(buildpro_data, "DB_PATH", tmp_path / "bp.db")
    monkeypatch.setattr(gmail_integration, "_service",
                        lambda: (_ for _ in ()).throw(RuntimeError("no gmail here")))
    monkeypatch.setattr(cs, "match_new_candidate",
                        lambda cid, **k: {"ok": True, "matches": [{"job_id": 3, "score": 0.8}],
                                          "match_count": 1})

    result = ri.process_upload(
        b"Jane Doe\njane@example.com\nSuperintendent.\n", "jane.txt", tmp_path / "r")
    assert result["candidate_id"] is not None
    assert result["match_count"] == 1, "intake never reached the matcher"
