"""actions/business_modules.py — the plug-in architecture Section NINTH
asks for. BuildPro/DDF must report real signals from real data; CareerRocket/
Airbnb must honestly report implemented=False rather than fabricating a
working integration.
"""
import pytest

from actions import business_modules as bm
from actions import buildpro_data as bd
from actions import daily_deal_finders as ddf


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(bd, "DB_PATH", tmp_path / "buildpro.db")
    monkeypatch.setattr(ddf, "DB_PATH", tmp_path / "ddf.db")


def test_all_four_businesses_are_registered():
    assert set(bm.BUSINESS_MODULES.keys()) == {"buildpro", "ddf", "careerrocket", "airbnb"}


def test_careerrocket_and_airbnb_honestly_report_not_implemented():
    assert bm.BUSINESS_MODULES["careerrocket"].implemented is False
    assert bm.BUSINESS_MODULES["airbnb"].implemented is False
    snapshot = bm.gather_all()
    assert snapshot["careerrocket"]["implemented"] is False
    assert snapshot["airbnb"]["implemented"] is False


def test_buildpro_and_ddf_are_marked_implemented():
    assert bm.BUSINESS_MODULES["buildpro"].implemented is True
    assert bm.BUSINESS_MODULES["ddf"].implemented is True


def test_buildpro_module_surfaces_real_followups():
    cid = bd.add_candidate(name="Stale Candidate", email="stale@example.com", status="new")
    # list_candidates_needing_followup() keys off the row's own updated_ts
    # (no separate last-contacted field exists — see that function's own
    # docstring) — update_candidate() always resets updated_ts to now, so
    # backdate it directly the same way test_executive_brief.py backdates
    # a stale-approval task's updated_ts.
    import time
    conn = bd._connect()
    conn.execute("UPDATE buildpro_candidates SET updated_ts = ? WHERE id = ?", (time.time() - 8 * 86400, cid))
    conn.commit()
    conn.close()

    signals = bm.BUSINESS_MODULES["buildpro"].gather()
    followups = [s for s in signals if s.kind == "followup"]
    assert any("Stale Candidate" in s.title for s in followups)


def test_ddf_module_flags_empty_pipeline_as_a_risk():
    signals = bm.BUSINESS_MODULES["ddf"].gather()
    risks = [s for s in signals if s.kind == "risk"]
    assert any("No deals discovered today" in s.title for s in risks)


def test_ddf_module_surfaces_high_ticket_picks():
    ddf.save_product({
        "name": "Expensive Thing", "source": "manual", "price": 250.0, "current_price": 250.0,
        "product_id": "exp-1", "retailer": "amazon", "status": ddf.STATUS_SCORED,
        "demand": 90, "trend_strength": 0.8,
    })
    signals = bm.BUSINESS_MODULES["ddf"].gather()
    opportunities = [s for s in signals if s.kind == "opportunity"]
    assert any("high-ticket" in s.title.lower() for s in opportunities)


def test_gather_all_never_raises_even_if_a_module_blows_up(monkeypatch):
    def _boom():
        raise RuntimeError("data source down")
    monkeypatch.setattr(bm.BUSINESS_MODULES["buildpro"], "gather", _boom)
    snapshot = bm.gather_all()
    assert snapshot["buildpro"]["signals"][0]["kind"] == "risk"
    assert "data source down" in snapshot["buildpro"]["signals"][0]["detail"]
