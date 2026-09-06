"""core/headless/tool_executor.py's "hubspot" dispatch — create_deal,
create_task, and the five associate_* actions. Tested directly through
ToolExecutor (the same shared dispatcher both main.py/desktop and the
headless FastAPI service call), NOT through main.py — main.py can't be
imported in this sandbox (PyQt6/libEGL), but ToolExecutor itself has no
such dependency, matching test_headless_core.py's own pattern for testing
this exact module headless-only.

Never makes a live HubSpot call: every hubspot_integration.py function
used here is monkeypatched.
"""
import asyncio

import pytest

from actions import hubspot_integration
from core.headless.context import ToolContext
from core.headless.tool_executor import ToolExecutor


def _executor() -> ToolExecutor:
    return ToolExecutor(ToolContext())


def _run(coro):
    return asyncio.run(coro)


# ── create_deal ────────────────────────────────────────────

def test_create_deal_requires_properties():
    result = _run(_executor().execute("hubspot", {"action": "create_deal"}))
    assert "properties" in result.lower()


def test_create_deal_with_full_details_passes_approved_true_and_reports_id(monkeypatch):
    captured = {}

    def fake_create_deal(properties, approved=False):
        captured["properties"] = properties
        captured["approved"] = approved
        return {"ok": True, "state": "OK", "record": {"id": "deal-42"}}

    monkeypatch.setattr(hubspot_integration, "create_deal", fake_create_deal)
    result = _run(_executor().execute("hubspot", {
        "action": "create_deal",
        "properties": '{"dealname": "BuildPro <> Acme Construction", "pipeline": "default"}',
    }))
    assert captured["approved"] is True
    assert captured["properties"]["dealname"] == "BuildPro <> Acme Construction"
    assert "deal-42" in result


def test_create_deal_failure_is_reported_not_fabricated(monkeypatch):
    monkeypatch.setattr(hubspot_integration, "create_deal", lambda properties, approved=False: {
        "ok": False, "state": "ERROR", "detail": "dealname is required",
    })
    result = _run(_executor().execute("hubspot", {
        "action": "create_deal", "properties": '{"pipeline": "default"}',
    }))
    assert "couldn't create" in result.lower()
    assert "dealname is required" in result


def test_create_deal_with_invalid_json_properties_is_refused(monkeypatch):
    calls = []
    monkeypatch.setattr(hubspot_integration, "create_deal", lambda *a, **k: calls.append(1))
    result = _run(_executor().execute("hubspot", {"action": "create_deal", "properties": "not json"}))
    assert calls == []
    assert "properties" in result.lower()


# ── create_task ────────────────────────────────────────────

def test_create_task_requires_properties():
    result = _run(_executor().execute("hubspot", {"action": "create_task"}))
    assert "properties" in result.lower()


def test_create_task_with_full_details_passes_approved_true_and_reports_id(monkeypatch):
    captured = {}

    def fake_create_task(properties, approved=False):
        captured["properties"] = properties
        captured["approved"] = approved
        return {"ok": True, "state": "OK", "record": {"id": "task-7"}}

    monkeypatch.setattr(hubspot_integration, "create_task", fake_create_task)
    result = _run(_executor().execute("hubspot", {
        "action": "create_task",
        "properties": '{"hs_task_subject": "Follow up with candidate", "hs_task_status": "NOT_STARTED"}',
    }))
    assert captured["approved"] is True
    assert captured["properties"]["hs_task_subject"] == "Follow up with candidate"
    assert "task-7" in result


def test_create_task_failure_is_reported_not_fabricated(monkeypatch):
    monkeypatch.setattr(hubspot_integration, "create_task", lambda properties, approved=False: {
        "ok": False, "state": "ERROR", "detail": "hs_task_subject is required",
    })
    result = _run(_executor().execute("hubspot", {
        "action": "create_task", "properties": '{"hs_task_status": "NOT_STARTED"}',
    }))
    assert "couldn't create" in result.lower()
    assert "hs_task_subject is required" in result


# ── associate_* ─────────────────────────────────────────

@pytest.mark.parametrize("action,id_a_key,id_b_key,fn_name", [
    ("associate_contact_company", "contact_id", "company_id", "associate_contact_with_company"),
    ("associate_deal_contact", "deal_id", "contact_id", "associate_deal_with_contact"),
    ("associate_deal_company", "deal_id", "company_id", "associate_deal_with_company"),
    ("associate_task_contact", "task_id", "contact_id", "associate_task_with_contact"),
    ("associate_task_deal", "task_id", "deal_id", "associate_task_with_deal"),
])
def test_associate_action_calls_the_right_function_with_both_real_ids(monkeypatch, action, id_a_key, id_b_key, fn_name):
    captured = {}

    def fake_associate(id_a, id_b, approved=False):
        captured["ids"] = (id_a, id_b)
        captured["approved"] = approved
        return {"ok": True, "state": "OK"}

    monkeypatch.setattr(hubspot_integration, fn_name, fake_associate)
    result = _run(_executor().execute("hubspot", {
        "action": action, id_a_key: "real-id-a", id_b_key: "real-id-b",
    }))
    assert captured["ids"] == ("real-id-a", "real-id-b")
    assert captured["approved"] is True
    assert "created" in result.lower()


def test_associate_action_without_both_ids_is_refused_and_never_calls_hubspot(monkeypatch):
    calls = []
    monkeypatch.setattr(hubspot_integration, "associate_deal_with_contact", lambda *a, **k: calls.append(1))
    result = _run(_executor().execute("hubspot", {"action": "associate_deal_contact", "deal_id": "deal-1"}))
    assert calls == []
    assert "need" in result.lower()


def test_associate_action_failure_is_reported_honestly(monkeypatch):
    monkeypatch.setattr(hubspot_integration, "associate_contact_with_company", lambda a, b, approved=False: {
        "ok": False, "state": "ERROR", "detail": "company not found",
    })
    result = _run(_executor().execute("hubspot", {
        "action": "associate_contact_company", "contact_id": "c1", "company_id": "bad-company",
    }))
    assert "couldn't associate" in result.lower()
    assert "company not found" in result
