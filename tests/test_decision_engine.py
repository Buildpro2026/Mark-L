import pytest

from actions import decision_engine as de
from actions import business_intelligence as bi
from actions import strategic_objective as so


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(bi, "DB_PATH", tmp_path / "de_bi_test.db")
    monkeypatch.setattr(so, "CONFIG_FILE", tmp_path / "de_objective_test.json")


def test_propose_decision_logs_it_unauthorized_by_default(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    r = de.propose_decision(
        "ddf", "Run a paid ad test", "Analysis text",
        alternatives="Organic only", recommendation="Try $50 test budget",
        upside="Faster validation", downside="Could lose $50",
    )
    assert "decision_id" in r
    assert r["objective"]["target_amount_usd"] == 1_000_000

    entry = bi.get_entry(r["decision_id"])
    assert entry["data"]["authorized"] is False
    assert entry["data"]["requires_authorization"] is True


def test_propose_decision_not_requiring_authorization_is_pre_authorized(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    r = de.propose_decision("ddf", "Log a market observation", "x", requires_authorization=False)
    entry = bi.get_entry(r["decision_id"])
    assert entry["data"]["authorized"] is True


def test_authorize_decision_flips_the_flag(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    r = de.propose_decision("ddf", "Spend $200 on ads", "x")
    assert de.is_authorized(r["decision_id"]) is False

    de.authorize_decision(r["decision_id"])
    assert de.is_authorized(r["decision_id"]) is True


def test_authorize_unknown_decision_raises(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        de.authorize_decision(999999)


def test_record_decision_outcome_links_back_to_the_decision(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    r = de.propose_decision("ddf", "Run a paid ad test", "x", requires_authorization=False)
    de.authorize_decision(r["decision_id"])

    outcome = de.record_decision_outcome(
        r["decision_id"], result="Generated 3 leads", revenue_usd=150, cost_usd=50,
        lesson="Targeting X performed best", recommendation="Scale that segment",
    )
    assert "outcome_id" in outcome

    lessons = bi.get_lessons_for("ddf")
    assert "Targeting X" in lessons[0]["content"]

    revenue_entries = bi.list_entries(category="revenue", business="ddf")
    assert revenue_entries[0]["data"]["amount_usd"] == 150


def test_record_outcome_for_unknown_decision_raises(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        de.record_decision_outcome(999999, result="x")
