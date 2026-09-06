"""actions/executive_brief.py — Phase 3 additions: the RISKS and DDF
sections the CEO/COS morning brief spec explicitly requires, on top of
the existing (already-working) buildpro/opportunities/gmail/calendar/
approvals sections. Every assertion here traces a claim back to data the
test itself put in, never a fabricated default.
"""
import pytest

from actions import executive_brief
from actions import agent_orchestrator as ao
from actions import business_intelligence as bi
from actions import opportunity_engine as oe
from actions import strategic_objective as so
from actions import daily_deal_finders as ddf
from actions import buffer_integration as buf
from actions import buildpro_data as bd
from core.headless import config as _hc


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(bi, "DB_PATH", tmp_path / "bi.db")
    monkeypatch.setattr(oe, "DB_PATH", tmp_path / "opp.db")
    monkeypatch.setattr(so, "CONFIG_FILE", tmp_path / "strategic_objective.json")
    monkeypatch.setattr(ddf, "DB_PATH", tmp_path / "ddf.db")
    monkeypatch.setattr(buf, "DB_PATH", tmp_path / "buffer.db")
    monkeypatch.setattr(buf, "CONFIG_PATH", tmp_path / "api_keys.json")
    monkeypatch.setattr(_hc, "BUFFER_TOKEN", None)
    monkeypatch.setattr(bd, "DB_PATH", tmp_path / "buildpro.db")


# ── risks ─────────────────────────────────────────────────────────────

def test_no_risks_when_nothing_is_wrong():
    assert executive_brief._operational_risks() == []


def test_stalled_approval_over_24h_is_flagged_as_a_risk():
    orch = ao.orchestrator
    orch._agents["test_execute_risk"] = ao.AgentDefinition(
        id="test_execute_risk", name="Test Execute Risk", description="x",
        nucleus_id="system", permission_level=ao.PermissionLevel.EXECUTE,
        handler=lambda task: {"ok": True},
    )
    try:
        task = orch.assign_task("test_execute_risk", "do a real thing")
        assert task.status == ao.TaskStatus.PENDING_APPROVAL
        task.updated_ts -= 25 * 3600  # simulate 25h of waiting
        orch._tasks[task.id] = task

        risks = executive_brief._operational_risks()
        kinds = [r["kind"] for r in risks]
        assert "stalled_approval" in kinds
    finally:
        del orch._agents["test_execute_risk"]
        del orch._tasks[task.id]


def test_recent_task_failure_is_flagged_as_a_risk():
    orch = ao.orchestrator
    orch._agents["test_failing_agent"] = ao.AgentDefinition(
        id="test_failing_agent", name="Test Failing Agent", description="x",
        nucleus_id="system", permission_level=ao.PermissionLevel.OBSERVE,
        handler=lambda task: (_ for _ in ()).throw(RuntimeError("simulated failure")),
    )
    try:
        task = orch.assign_task("test_failing_agent", "run it")
        risks = executive_brief._operational_risks()
        kinds = [r["kind"] for r in risks]
        assert "recent_task_failure" in kinds
    finally:
        del orch._agents["test_failing_agent"]
        del orch._tasks[task.id]


def test_agent_error_state_is_flagged_as_a_risk():
    orch = ao.orchestrator
    agent = orch.get_agent("system_monitor_agent")
    agent.last_error = "simulated ongoing error"
    try:
        risks = executive_brief._operational_risks()
        kinds = [r["kind"] for r in risks]
        assert "agent_error_state" in kinds
    finally:
        agent.last_error = None


# ── DDF snapshot ──────────────────────────────────────────────────────

def test_ddf_snapshot_is_honestly_empty_with_no_products():
    snapshot = executive_brief._ddf_snapshot()
    assert snapshot["high_ticket_picks"] == []
    assert snapshot["trending"] == []
    assert snapshot["todays_deals_count"] == 0


def test_ddf_snapshot_reports_real_high_ticket_picks():
    ddf.save_product({
        "name": "Premium Grill", "source": "amazon", "category": "outdoor",
        "price": 300.0, "current_price": 300.0, "url": "https://x.com",
        "retailer": "amazon", "product_id": "grill-1", "demand": 80,
        "trend_strength": 0.7, "commission_rate": 0.1, "approved": True,
    })
    snapshot = executive_brief._ddf_snapshot()
    assert len(snapshot["high_ticket_picks"]) == 1
    assert snapshot["high_ticket_picks"][0]["product_id"] == "grill-1"


# ── full brief shape ──────────────────────────────────────────────────

def test_generate_brief_includes_risks_and_ddf_sections(gmail_not_authorized):
    brief = executive_brief.generate_brief()
    assert "risks" in brief
    assert "daily_deal_finders" in brief
    assert brief["risks"] == []
    assert brief["daily_deal_finders"]["high_ticket_picks"] == []


def test_generate_brief_recommends_looking_at_flagged_risks(gmail_not_authorized):
    orch = ao.orchestrator
    orch._agents["test_execute_risk2"] = ao.AgentDefinition(
        id="test_execute_risk2", name="Test Execute Risk 2", description="x",
        nucleus_id="system", permission_level=ao.PermissionLevel.EXECUTE,
        handler=lambda task: {"ok": True},
    )
    try:
        task = orch.assign_task("test_execute_risk2", "do a real thing")
        task.updated_ts -= 25 * 3600
        orch._tasks[task.id] = task

        brief = executive_brief.generate_brief()
        assert any("risk" in a.lower() for a in brief["recommended_actions"])
    finally:
        del orch._agents["test_execute_risk2"]
        del orch._tasks[task.id]
