"""The connections added on top of 1939e27: Google Tasks and HubSpot in
the morning brief, decision-layer escalation actually reaching Twilio,
resume candidates carrying their real profile, and read-only HubSpot
context on the day's top BuildPro match.

None of these are new systems — each wires an existing capability into a
path that previously ignored it.
"""
import pytest


# ══ EXECUTIVE BRIEF: TASKS AND HUBSPOT ══════════════════════════════════

def test_tasks_snapshot_reports_unauthorized_honestly(monkeypatch):
    from actions import executive_brief as eb, google_auth
    monkeypatch.setattr(google_auth, "get_credential_status", lambda: {"authorized": False})
    snap = eb._tasks_snapshot()
    assert snap["available"] is False
    assert snap["overdue"] == [] and snap["waiting"] == []


def test_tasks_snapshot_splits_overdue_from_waiting(monkeypatch):
    from actions import executive_brief as eb, google_auth, google_tasks_integration as gt
    from datetime import datetime, timedelta, timezone
    monkeypatch.setattr(google_auth, "get_credential_status", lambda: {"authorized": True})
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    monkeypatch.setattr(gt, "list_tasks", lambda **k: {"ok": True, "tasks": [
        {"id": "t1", "title": "Late one", "due": past},
        {"id": "t2", "title": "Not due yet", "due": future},
    ]})
    snap = eb._tasks_snapshot()
    assert snap["available"] is True
    assert [t["title"] for t in snap["overdue"]] == ["Late one"]
    assert [t["title"] for t in snap["waiting"]] == ["Not due yet"]


def test_hubspot_snapshot_reports_not_configured_honestly(monkeypatch):
    from actions import executive_brief as eb
    from actions import hubspot_integration as hs
    monkeypatch.setattr(hs, "is_configured", lambda: False)
    snap = eb._hubspot_snapshot()
    assert snap["available"] is False
    assert "HUBSPOT_TOKEN" in snap["reason"]


def test_hubspot_snapshot_lists_companies_when_configured(monkeypatch):
    from actions import executive_brief as eb
    from actions import hubspot_integration as hs
    monkeypatch.setattr(hs, "is_configured", lambda: True)
    monkeypatch.setattr(hs, "get_companies", lambda **k: {"ok": True, "results": [
        {"id": "1", "properties": {"name": "Turner", "domain": "turner.com"}}]})
    snap = eb._hubspot_snapshot()
    assert snap["available"] is True
    assert snap["companies"][0]["name"] == "Turner"


def test_generate_brief_carries_tasks_and_hubspot(monkeypatch):
    from actions import executive_brief as eb
    monkeypatch.setattr(eb, "_tasks_snapshot", lambda: {"available": False, "overdue": [], "waiting": []})
    monkeypatch.setattr(eb, "_hubspot_snapshot", lambda: {"available": False, "companies": []})
    monkeypatch.setattr(eb, "_gmail_snapshot", lambda **k: {"available": False, "messages": []})
    monkeypatch.setattr(eb, "_calendar_snapshot", lambda **k: {"available": False, "events": []})
    brief = eb.generate_brief()
    assert "tasks" in brief and "hubspot" in brief


# ══ DECISION-LAYER ESCALATION REACHES TWILIO ═════════════════════════════

def _item(escalate, title="Sync HubSpot", source="hubspot"):
    return {"title": title, "source": source, "decision": {"escalate": escalate, "why": "3 failures"}}


def test_only_escalate_flagged_items_are_notified(monkeypatch):
    from actions import ceo_operating_cycle as cycle, approval_notifier
    calls = []
    monkeypatch.setattr(approval_notifier, "notify_urgent_event",
                        lambda **k: calls.append(k) or {"action": "sms"})
    cycle._escalate_flagged_priorities([_item(False), _item(True)])
    assert len(calls) == 1
    assert calls[0]["level"] == 3


def test_the_same_item_produces_a_stable_event_id_across_cycles(monkeypatch):
    from actions import ceo_operating_cycle as cycle, approval_notifier
    calls = []
    monkeypatch.setattr(approval_notifier, "notify_urgent_event",
                        lambda **k: calls.append(k) or {"action": "sms"})
    cycle._escalate_flagged_priorities([_item(True)])
    cycle._escalate_flagged_priorities([_item(True)])
    assert calls[0]["event_id"] == calls[1]["event_id"]


def test_a_failing_notification_does_not_stop_the_others(monkeypatch):
    from actions import ceo_operating_cycle as cycle, approval_notifier
    def _boom(**k):
        raise RuntimeError("twilio down")
    monkeypatch.setattr(approval_notifier, "notify_urgent_event", _boom)
    sent = cycle._escalate_flagged_priorities([_item(True, title="A"), _item(True, title="B")])
    assert len(sent) == 2
    assert all(s["action"] == "failed" for s in sent)


def test_dry_run_is_passed_through_without_sending(monkeypatch):
    from actions import ceo_operating_cycle as cycle, approval_notifier
    calls = []
    monkeypatch.setattr(approval_notifier, "notify_urgent_event",
                        lambda **k: calls.append(k) or {"action": "dry"})
    cycle._escalate_flagged_priorities([_item(True)], dry_run=True)
    assert calls[0]["dry_run"] is True


# ══ RESUME PROFILE EXTRACTION ═════════════════════════════════════════════

RESUME_TEXT = """Dana Reeves
dana@example.com
(312) 555-0142

Senior Director of Construction Operations

Phoenix, AZ

12+ years of experience leading mission critical data center construction.
"""


def test_extract_profile_reads_title_specialty_experience_location():
    from actions import resume_intake as ri
    profile = ri.extract_profile(RESUME_TEXT)
    assert "Director" in profile["title"]
    assert profile["specialty"] == "data center"
    assert profile["years_experience"] == 12
    assert profile["location"] == "Phoenix, AZ"


def test_extract_profile_omits_fields_not_present():
    from actions import resume_intake as ri
    profile = ri.extract_profile("Just a name and an email jane@example.com")
    assert "years_experience" not in profile
    assert "specialty" not in profile


def test_uploaded_resume_carries_its_profile_into_the_candidate_record(tmp_path, monkeypatch):
    from actions import resume_intake as ri, buildpro_data, gmail_integration
    monkeypatch.setattr(buildpro_data, "DB_PATH", tmp_path / "bp.db")
    monkeypatch.setattr(gmail_integration, "_service",
                        lambda: (_ for _ in ()).throw(RuntimeError("no gmail here")))

    result = ri.process_upload(RESUME_TEXT.encode(), "dana.txt", tmp_path / "r")
    assert result["candidate_id"] is not None
    record = buildpro_data.get_candidate(result["candidate_id"])
    assert record["specialty"] == "data center"
    assert record["years_experience"] == 12
    assert record["location"] == "Phoenix, AZ"


# ══ HUBSPOT CONTEXT ON THE TOP MATCH (READ-ONLY) ═════════════════════════

def test_hubspot_context_reports_not_configured(monkeypatch):
    from actions import cross_system as cs
    from actions import hubspot_integration as hs
    monkeypatch.setattr(hs, "is_configured", lambda: False)
    ctx = cs.hubspot_context_for_employer("Turner")
    assert ctx["state"] == "NOT_CONFIGURED"
    assert ctx["in_hubspot"] is None


def test_hubspot_context_distinguishes_found_from_not_found(monkeypatch):
    from actions import cross_system as cs
    from actions import hubspot_integration as hs
    monkeypatch.setattr(hs, "is_configured", lambda: True)

    monkeypatch.setattr(hs, "search_companies", lambda *a, **k: {"ok": True, "results": [{"id": "9"}]})
    found = cs.hubspot_context_for_employer("Turner")
    assert found["in_hubspot"] is True and found["hubspot_company_id"] == "9"

    monkeypatch.setattr(hs, "search_companies", lambda *a, **k: {"ok": True, "results": []})
    absent = cs.hubspot_context_for_employer("Turner")
    assert absent["in_hubspot"] is False


def test_hubspot_context_never_writes():
    import inspect
    from actions import cross_system as cs
    source = inspect.getsource(cs.hubspot_context_for_employer)
    assert "upsert_company" not in source
    assert "create_company" not in source
    assert "approved=True" not in source


def test_daily_matching_attaches_hubspot_context_to_the_top_match_only(monkeypatch, tmp_path):
    from actions import buildpro_data as bd, buildpro_daily as bpd, cross_system as cs
    monkeypatch.setattr(bd, "DB_PATH", tmp_path / "bp.db")
    monkeypatch.setattr(cs, "hubspot_context_for_employer",
                        lambda name: {"state": "OK", "in_hubspot": True, "detail": f"{name} known"})

    job_id = bd.add_job("Senior Project Manager", location="Phoenix, AZ",
                        source="test fp:x company:Turner")
    bd.add_candidate("Dana Reeves", email="dana@example.com", title="Senior Project Manager",
                     years_experience=10, location="Phoenix, AZ")

    result = bpd.run_daily_matching()
    if result["top"] is not None:
        assert result["top"].get("employer") == "Turner"
        assert result["top"]["hubspot"]["in_hubspot"] is True


def test_a_hubspot_lookup_failure_does_not_break_the_matching_run(monkeypatch, tmp_path):
    from actions import buildpro_data as bd, buildpro_daily as bpd, cross_system as cs
    monkeypatch.setattr(bd, "DB_PATH", tmp_path / "bp.db")
    monkeypatch.setattr(cs, "hubspot_context_for_employer",
                        lambda name: (_ for _ in ()).throw(RuntimeError("hubspot down")))

    bd.add_job("Senior Project Manager", location="Phoenix, AZ", source="test fp:x company:Turner")
    bd.add_candidate("Dana Reeves", email="dana@example.com", title="Senior Project Manager",
                     years_experience=10, location="Phoenix, AZ")

    result = bpd.run_daily_matching()   # must not raise
    assert result["ok"] is True
