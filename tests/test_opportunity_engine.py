import pytest

from actions import opportunity_engine as oe


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(oe, "DB_PATH", tmp_path / "opp_test.db")


def test_score_opportunity_is_50_for_all_neutral_factors():
    # every factor defaulting to 3/5 = 60% of max on every weighted term
    assert oe.score_opportunity({}) == 60.0


def test_score_opportunity_is_100_for_all_max_factors():
    all_max = {f: 5 for f in oe._WEIGHTS}
    assert oe.score_opportunity(all_max) == 100.0


def test_score_opportunity_is_20_for_all_min_factors():
    all_min = {f: 1 for f in oe._WEIGHTS}
    assert oe.score_opportunity(all_min) == 20.0


def test_score_clamps_out_of_range_and_invalid_values():
    assert oe.score_opportunity({"revenue_potential": 99}) == oe.score_opportunity({"revenue_potential": 5})
    assert oe.score_opportunity({"revenue_potential": -5}) == oe.score_opportunity({"revenue_potential": 1})
    assert oe.score_opportunity({"revenue_potential": "not a number"}) == oe.score_opportunity({"revenue_potential": 3})


def test_add_opportunity_rejects_invalid_type(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        oe.add_opportunity("buildpro", "medium_term", "Bad type test")


def test_add_and_list_opportunity_round_trips(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    result = oe.add_opportunity(
        "ddf", "quick_cash", "Affiliate blitz for holiday deals",
        description="Push top 10 deals across channels this week",
        revenue_potential=4, time_to_revenue=5, probability=4,
    )
    assert result["id"] > 0
    assert result["score"] > 60   # above-neutral inputs should score above the all-3 baseline

    opps = oe.list_opportunities(opp_type="quick_cash", business="ddf")
    assert opps[0]["title"] == "Affiliate blitz for holiday deals"


def test_rank_opportunities_orders_highest_score_first(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    oe.add_opportunity("buildpro", "long_term", "Low potential idea", revenue_potential=1, probability=1, alignment=1)
    oe.add_opportunity("buildpro", "long_term", "High potential idea", revenue_potential=5, probability=5, alignment=5)

    ranked = oe.rank_opportunities(opp_type="long_term", business="buildpro")
    assert ranked[0]["title"] == "High potential idea"
    assert ranked[0]["score"] > ranked[1]["score"]


def test_rank_opportunities_separates_quick_cash_from_long_term(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    oe.add_opportunity("careerrocket", "quick_cash", "Resume review sprint")
    oe.add_opportunity("careerrocket", "long_term", "Career coaching subscription")

    quick = oe.rank_opportunities(opp_type="quick_cash", business="careerrocket")
    long_term = oe.rank_opportunities(opp_type="long_term", business="careerrocket")
    assert [o["title"] for o in quick] == ["Resume review sprint"]
    assert [o["title"] for o in long_term] == ["Career coaching subscription"]


def test_update_status_changes_status_and_rejects_invalid_value(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    result = oe.add_opportunity("ddf", "quick_cash", "Test opp")
    oe.update_status(result["id"], "active")
    opp = oe.list_opportunities(business="ddf")[0]
    assert opp["status"] == "active"

    with pytest.raises(ValueError):
        oe.update_status(result["id"], "not_a_real_status")
