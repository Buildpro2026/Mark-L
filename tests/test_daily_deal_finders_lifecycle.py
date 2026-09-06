"""Phase 3 DDF commerce-platform extension: lifecycle status, performance
ranking, daily high-ticket picks, and "You Might Have Missed" discovery.
"""
from datetime import datetime, timedelta, timezone

import pytest

from actions import daily_deal_finders as ddf


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch, tmp_path):
    monkeypatch.setattr(ddf, "DB_PATH", tmp_path / "test_ddf_lifecycle.db")


def _sample(**overrides):
    base = {
        "name": "Wireless Earbuds Pro", "source": "amazon", "category": "electronics",
        "price": 39.99, "current_price": 39.99, "original_price": 59.99,
        "url": "https://amazon.com/dp/TEST1", "affiliate_url": "https://amzn.to/abc123",
        "retailer": "amazon", "affiliate_source": "Amazon Associates",
        "product_id": "earbuds-001",
    }
    base.update(overrides)
    return base


# ── lifecycle status ────────────────────────────────────────────────────

def test_new_product_defaults_to_discovered_status():
    p = ddf.save_product(_sample())
    assert p["status"] == ddf.STATUS_DISCOVERED
    assert p["approved"] == 0


def test_approved_true_is_backward_compatible_with_published_status():
    p = ddf.save_product(_sample(approved=True))
    assert p["status"] == ddf.STATUS_PUBLISHED
    assert p["approved"] == 1


def test_status_walks_forward_through_the_full_lifecycle():
    ddf.save_product(_sample())
    for status in (ddf.STATUS_EVALUATED, ddf.STATUS_SCORED, ddf.STATUS_APPROVED, ddf.STATUS_PUBLISHED, ddf.STATUS_TRACKING, ddf.STATUS_WINNER):
        result = ddf.set_product_status("earbuds-001", status)
        assert result["ok"] is True, result
        assert result["status"] == status
    product = ddf.get_product("earbuds-001")
    assert product["status"] == ddf.STATUS_WINNER
    assert product["approved"] == 1


def test_status_refuses_an_invalid_skip_ahead_transition():
    ddf.save_product(_sample())
    result = ddf.set_product_status("earbuds-001", ddf.STATUS_WINNER)
    assert result["ok"] is False
    assert "discovered" in result["detail"]
    product = ddf.get_product("earbuds-001")
    assert product["status"] == ddf.STATUS_DISCOVERED


def test_status_refuses_an_unknown_status_name():
    ddf.save_product(_sample())
    with pytest.raises(ValueError, match="Unknown product status"):
        ddf.set_product_status("earbuds-001", "not-a-real-status")


def test_status_reports_honestly_for_a_missing_product():
    result = ddf.set_product_status("does-not-exist", ddf.STATUS_EVALUATED)
    assert result["ok"] is False
    assert "does-not-exist" in result["detail"]


def test_published_date_is_stamped_once_and_never_moved():
    ddf.save_product(_sample())
    ddf.set_product_status("earbuds-001", ddf.STATUS_EVALUATED)
    ddf.set_product_status("earbuds-001", ddf.STATUS_SCORED)
    ddf.set_product_status("earbuds-001", ddf.STATUS_APPROVED)
    first = ddf.set_product_status("earbuds-001", ddf.STATUS_PUBLISHED)
    published_date_1 = ddf.get_product("earbuds-001")["published_date"]
    assert published_date_1

    ddf.set_product_status("earbuds-001", ddf.STATUS_TRACKING)
    published_date_2 = ddf.get_product("earbuds-001")["published_date"]
    assert published_date_2 == published_date_1


def test_resaving_a_published_product_does_not_revert_its_status():
    # INSERT OR REPLACE risk: updating price on an already-published
    # product must not silently reset status/views/clicks back to zero.
    ddf.save_product(_sample(approved=True))
    ddf.record_view("earbuds-001")
    ddf.record_affiliate_click("earbuds-001")
    ddf.set_product_status("earbuds-001", ddf.STATUS_TRACKING)

    ddf.save_product(_sample(current_price=34.99))  # simulate a price-update re-save, no approved/status passed

    product = ddf.get_product("earbuds-001")
    assert product["status"] == ddf.STATUS_TRACKING
    assert product["approved"] == 1
    assert product["views"] == 1
    assert product["affiliate_clicks"] == 1
    assert product["current_price"] == 34.99


def test_estimated_commission_is_computed_not_fabricated():
    p = ddf.save_product(_sample(current_price=100.0, commission_rate=0.08))
    assert p["estimated_commission"] == 8.0


def test_estimated_commission_stays_none_without_a_commission_rate():
    p = ddf.save_product(_sample(current_price=100.0))
    assert p["estimated_commission"] is None


def test_tags_list_is_stored_and_retrievable():
    ddf.save_product(_sample(tags=["gift-idea", "trending", "under-50"]))
    product = ddf.get_product("earbuds-001")
    assert "gift-idea" in product["tags"]
    assert "trending" in product["tags"]


# ── performance ranking ─────────────────────────────────────────────────

def test_rank_products_favors_real_engagement_over_zero_engagement():
    quiet = _sample(product_id="quiet", views=0, affiliate_clicks=0)
    popular = _sample(product_id="popular", name="Popular Thing", views=500, affiliate_clicks=40, conversions=5, revenue=150.0)
    ranked = ddf.rank_products([quiet, popular])
    assert ranked[0]["product_id"] == "popular"
    assert ranked[0]["rank_score"] > ranked[1]["rank_score"]


def test_rank_products_favors_recency():
    now_iso = datetime.now(timezone.utc).isoformat()
    old_iso = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    fresh = _sample(product_id="fresh", published_date=now_iso)
    stale = _sample(product_id="stale", published_date=old_iso)
    ranked = ddf.rank_products([stale, fresh])
    assert ranked[0]["product_id"] == "fresh"


def test_rank_products_never_raises_on_missing_fields():
    bare = {"product_id": "bare-minimum"}
    ranked = ddf.rank_products([bare])
    assert ranked[0]["rank_score"] == 0.0


# ── daily high-ticket picks ──────────────────────────────────────────────

def test_high_ticket_picks_excludes_cheap_products():
    ddf.save_product(_sample(product_id="cheap", current_price=15.0, approved=True))
    ddf.save_product(_sample(product_id="pricey", name="Pricey", current_price=250.0, approved=True))
    picks = ddf.select_daily_high_ticket_picks(min_price=100.0)
    ids = {p["product_id"] for p in picks}
    assert "cheap" not in ids


def test_high_ticket_picks_excludes_raw_unvetted_discoveries():
    ddf.save_product(_sample(product_id="raw", current_price=300.0))  # still status=discovered
    picks = ddf.select_daily_high_ticket_picks(min_price=100.0)
    assert picks == []


def test_high_ticket_picks_prefers_demand_and_commission_over_raw_price():
    expensive_but_undesirable = _sample(
        product_id="expensive-dud", name="Expensive Dud", current_price=900.0,
        demand=5, trend_strength=0.05, commission_rate=0.01, approved=True,
    )
    strong_pick = _sample(
        product_id="strong-pick", name="Strong Pick", current_price=150.0,
        demand=90, trend_strength=0.8, commission_rate=0.15, product_rating=4.7, approved=True,
    )
    ddf.save_product(expensive_but_undesirable)
    ddf.save_product(strong_pick)
    picks = ddf.select_daily_high_ticket_picks(min_price=100.0, limit=2)
    assert picks[0]["product_id"] == "strong-pick"


def test_high_ticket_picks_respects_limit_of_two_by_default():
    for i in range(5):
        ddf.save_product(_sample(product_id=f"pick-{i}", name=f"Pick {i}", current_price=200.0 + i, approved=True))
    picks = ddf.select_daily_high_ticket_picks(min_price=100.0)
    assert len(picks) == 2


# ── You Might Have Missed ────────────────────────────────────────────────

def test_you_might_have_missed_excludes_todays_products():
    today_iso = datetime.now(timezone.utc).isoformat()
    ddf.save_product(_sample(product_id="today-product", published_date=today_iso, approved=True))
    missed = ddf.get_you_might_have_missed()
    ids = {p["product_id"] for p in missed}
    assert "today-product" not in ids


def test_you_might_have_missed_includes_recent_but_not_todays_products():
    yesterday_iso = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    ddf.save_product(_sample(product_id="yesterday-product", published_date=yesterday_iso, approved=True))
    missed = ddf.get_you_might_have_missed()
    ids = {p["product_id"] for p in missed}
    assert "yesterday-product" in ids


def test_you_might_have_missed_excludes_products_older_than_the_window():
    ancient_iso = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    ddf.save_product(_sample(product_id="ancient-product", published_date=ancient_iso, approved=True))
    missed = ddf.get_you_might_have_missed(days=14)
    ids = {p["product_id"] for p in missed}
    assert "ancient-product" not in ids


def test_you_might_have_missed_can_exclude_the_currently_viewed_product():
    yesterday_iso = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    ddf.save_product(_sample(product_id="p1", published_date=yesterday_iso, approved=True))
    ddf.save_product(_sample(product_id="p2", name="Other", published_date=yesterday_iso, approved=True))
    missed = ddf.get_you_might_have_missed(exclude_product_id="p1")
    ids = {p["product_id"] for p in missed}
    assert "p1" not in ids
    assert "p2" in ids


def test_you_might_have_missed_never_includes_unapproved_products():
    yesterday_iso = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    ddf.save_product(_sample(product_id="never-approved", published_date=yesterday_iso, approved=False))
    missed = ddf.get_you_might_have_missed()
    ids = {p["product_id"] for p in missed}
    assert "never-approved" not in ids


# ── this week's hottest ──────────────────────────────────────────────────

def test_this_weeks_hottest_excludes_older_than_seven_days():
    ancient_iso = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    ddf.save_product(_sample(product_id="old", published_date=ancient_iso, approved=True))
    hottest = ddf.get_this_weeks_hottest()
    ids = {p["product_id"] for p in hottest}
    assert "old" not in ids


def test_this_weeks_hottest_includes_recent_products():
    recent_iso = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    ddf.save_product(_sample(product_id="recent", published_date=recent_iso, approved=True))
    hottest = ddf.get_this_weeks_hottest()
    ids = {p["product_id"] for p in hottest}
    assert "recent" in ids
