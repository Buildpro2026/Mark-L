import pytest

from actions import business_intelligence as bi


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(bi, "DB_PATH", tmp_path / "bi_test.db")


def test_add_and_list_entry_round_trips(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    entry_id = bi.add_entry("research", "buildpro", "Recruiting SaaS competitor scan",
                             content="Three competitors found", data={"count": 3})
    entries = bi.list_entries(category="research", business="buildpro")
    assert entries[0]["id"] == entry_id
    assert entries[0]["data"]["count"] == 3


def test_unknown_category_is_rejected(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        bi.add_entry("not_a_real_category", "buildpro", "x")


def test_list_entries_filters_by_business(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    bi.add_entry("competitors", "buildpro", "Competitor A")
    bi.add_entry("competitors", "ddf", "Competitor B")
    buildpro_only = bi.list_entries(category="competitors", business="buildpro")
    assert len(buildpro_only) == 1
    assert buildpro_only[0]["title"] == "Competitor A"


def test_get_entry_returns_none_for_missing_id(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert bi.get_entry(99999) is None


def test_record_outcome_files_outcome_lesson_recommendation_and_revenue(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    result = bi.record_outcome(
        business="ddf",
        plan="Post 5 deals to affiliate channel",
        result="3 conversions, $240 in commissions",
        revenue_usd=240.0,
        cost_usd=10.0,
        lesson="Weekend posts convert better than weekday posts",
        recommendation="Shift posting schedule to Fri-Sun",
    )
    assert "outcome_id" in result

    outcomes = bi.list_entries(category="outcomes", business="ddf")
    assert outcomes[0]["data"]["revenue_usd"] == 240.0

    lessons = bi.get_lessons_for("ddf")
    assert "Weekend posts" in lessons[0]["content"]
    assert lessons[0]["related_id"] == result["outcome_id"]

    revenue_entries = bi.list_entries(category="revenue", business="ddf")
    assert revenue_entries[0]["data"]["amount_usd"] == 240.0


def test_record_outcome_without_revenue_does_not_create_a_revenue_entry(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    bi.record_outcome(business="buildpro", plan="Cold outreach test", result="No responses")
    assert bi.list_entries(category="revenue", business="buildpro") == []


def test_summary_counts_by_category_and_totals_revenue(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    bi.record_outcome(business="ddf", plan="A", result="ok", revenue_usd=100)
    bi.record_outcome(business="ddf", plan="B", result="ok", revenue_usd=50)
    bi.add_entry("research", "ddf", "Market scan")

    s = bi.summary(business="ddf")
    assert s["counts"]["outcomes"] == 2
    assert s["counts"]["research"] == 1
    assert s["total_revenue_usd"] == 150
