import pytest

from actions import daily_deal_finders as ddf


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch, tmp_path):
    monkeypatch.setattr(ddf, "DB_PATH", tmp_path / "test_ddf.db")


def _sample(**overrides):
    base = {
        "name": "Wireless Earbuds Pro", "source": "amazon", "category": "electronics",
        "price": 39.99, "current_price": 39.99, "original_price": 59.99,
        "url": "https://amazon.com/dp/TEST1", "affiliate_url": "https://amzn.to/abc123",
        "retailer": "amazon", "affiliate_source": "Amazon Associates",
        "product_id": "earbuds-001", "approved": True,
    }
    base.update(overrides)
    return base


# ── retailer enforcement — Target/Walmart never allowed ───────────────────

def test_approved_retailers_are_amazon_and_tiktok_shop_only():
    assert ddf.APPROVED_RETAILERS == {"amazon", "tiktok_shop"}


def test_save_product_accepts_amazon():
    p = ddf.save_product(_sample(retailer="amazon"))
    assert p["retailer"] == "amazon"


def test_save_product_accepts_tiktok_shop():
    p = ddf.save_product(_sample(retailer="tiktok_shop", product_id="tt-001"))
    assert p["retailer"] == "tiktok_shop"


def test_save_product_rejects_target():
    with pytest.raises(ValueError, match="not approved"):
        ddf.save_product(_sample(retailer="target"))


def test_save_product_rejects_walmart():
    with pytest.raises(ValueError, match="not approved"):
        ddf.save_product(_sample(retailer="walmart"))


def test_save_product_rejected_retailer_is_never_persisted():
    try:
        ddf.save_product(_sample(retailer="walmart", product_id="walmart-001"))
    except ValueError:
        pass
    assert ddf.get_product("walmart-001") is None


def test_validate_retailer_allows_none():
    ddf.validate_retailer(None)
    ddf.validate_retailer("")


# ── never fabricate: unset fields stay unset ───────────────────────────────

def test_save_product_never_fabricates_missing_fields():
    p = ddf.save_product({"name": "Bare Minimum Product", "source": "amazon", "product_id": "bare-1"})
    assert p.get("image_url") is None
    assert p.get("description") is None
    assert p.get("affiliate_url") is None or p.get("affiliate_url") == p.get("url")
    stored = ddf.get_product("bare-1")
    assert stored["image_url"] is None
    assert stored["description"] is None


def test_save_product_does_not_invent_a_discount_without_both_prices():
    p = ddf.save_product(_sample(original_price=None, current_price=39.99, product_id="nodisc-1"))
    assert p["discount_pct"] is None


def test_save_product_computes_real_discount_from_both_prices():
    p = ddf.save_product(_sample(original_price=100.0, current_price=75.0, product_id="disc-1"))
    assert p["discount_pct"] == 25.0


# ── slugs ───────────────────────────────────────────────────────────────

def test_save_product_auto_generates_slug():
    p = ddf.save_product(_sample(product_id="slug-test-1"))
    assert p["slug"] == "wireless-earbuds-pro-slug-test-1"


def test_save_product_respects_explicit_slug():
    p = ddf.save_product(_sample(product_id="slug-test-2", slug="custom-slug"))
    assert p["slug"] == "custom-slug"


def test_get_product_by_slug_finds_the_right_record():
    ddf.save_product(_sample(product_id="findme-1"))
    found = ddf.get_product_by_slug("wireless-earbuds-pro-findme-1")
    assert found is not None
    assert found["product_id"] == "findme-1"


def test_get_product_by_slug_returns_none_for_unknown_slug():
    assert ddf.get_product_by_slug("does-not-exist") is None


# ── catalog queries ────────────────────────────────────────────────────────

def test_list_products_filters_by_category():
    ddf.save_product(_sample(product_id="p1", category="electronics"))
    ddf.save_product(_sample(product_id="p2", category="kitchen", name="Blender"))
    results = ddf.list_products(category="kitchen")
    assert len(results) == 1
    assert results[0]["product_id"] == "p2"


def test_list_products_filters_by_retailer():
    ddf.save_product(_sample(product_id="p1", retailer="amazon"))
    ddf.save_product(_sample(product_id="p2", retailer="tiktok_shop", name="TikTok Find"))
    results = ddf.list_products(retailer="tiktok_shop")
    assert len(results) == 1
    assert results[0]["product_id"] == "p2"


def test_list_products_excludes_unapproved_by_default():
    ddf.save_product(_sample(product_id="p1", approved=True))
    ddf.save_product(_sample(product_id="p2", approved=False, name="Pending Review"))
    results = ddf.list_products()
    assert len(results) == 1
    assert results[0]["product_id"] == "p1"


def test_list_products_can_include_unapproved():
    ddf.save_product(_sample(product_id="p1", approved=False))
    results = ddf.list_products(approved_only=False)
    assert len(results) == 1


def test_get_todays_deals_only_returns_todays_discoveries():
    ddf.save_product(_sample(product_id="today-1"))  # discovery_date defaults to now
    ddf.save_product(_sample(product_id="old-1", name="Old Deal", discovery_date="2020-01-01T00:00:00+00:00"))
    todays = ddf.get_todays_deals()
    ids = {d["product_id"] for d in todays}
    assert "today-1" in ids
    assert "old-1" not in ids


def test_get_trending_deals_orders_by_trend_strength():
    ddf.save_product(_sample(product_id="low", trend_strength=0.2, name="Low Trend"))
    ddf.save_product(_sample(product_id="high", trend_strength=0.9, name="High Trend"))
    trending = ddf.get_trending_deals()
    assert trending[0]["product_id"] == "high"


def test_get_best_sellers_orders_by_conversions():
    ddf.save_product(_sample(product_id="p1", name="No Conversions"))
    ddf.save_product(_sample(product_id="p2", name="Some Conversions"))
    ddf.record_conversion("p2", 24.99)
    best = ddf.get_best_sellers()
    assert best[0]["product_id"] == "p2"


def test_get_high_ticket_deals_filters_by_min_price():
    ddf.save_product(_sample(product_id="cheap", current_price=15.0, name="Cheap Thing"))
    ddf.save_product(_sample(product_id="pricey", current_price=250.0, name="Pricey Thing"))
    high_ticket = ddf.get_high_ticket_deals(min_price=100.0)
    ids = {d["product_id"] for d in high_ticket}
    assert "pricey" in ids
    assert "cheap" not in ids


def test_list_categories_returns_distinct_approved_categories():
    ddf.save_product(_sample(product_id="p1", category="electronics"))
    ddf.save_product(_sample(product_id="p2", category="electronics", name="Another"))
    ddf.save_product(_sample(product_id="p3", category="kitchen", name="Third"))
    cats = ddf.list_categories()
    assert sorted(cats) == ["electronics", "kitchen"]


# ── revenue-chain tracking ─────────────────────────────────────────────────

def test_record_view_increments_and_is_idempotent_per_call():
    ddf.save_product(_sample(product_id="v1"))
    ddf.record_view("v1")
    ddf.record_view("v1")
    assert ddf.get_product("v1")["views"] == 2


def test_record_view_unknown_product_returns_false():
    assert ddf.record_view("does-not-exist") is False


def test_record_affiliate_click_increments_aggregate():
    ddf.save_product(_sample(product_id="c1"))
    ddf.record_affiliate_click("c1")
    assert ddf.get_product("c1")["affiliate_clicks"] == 1


def test_record_affiliate_click_attributes_to_specific_post_platform():
    ddf.save_product(_sample(product_id="c2"))
    ddf.create_post({"product": {"product_id": "c2"}, "platform": "instagram", "copy": "Great find."})
    ddf.record_affiliate_click("c2", platform="instagram")
    posts = ddf.get_product_posts("c2")
    assert posts[0]["clicks"] == 1


def test_record_conversion_never_fabricated_only_records_what_is_passed():
    ddf.save_product(_sample(product_id="conv1"))
    ddf.record_conversion("conv1", 42.50)
    row = ddf.get_product("conv1")
    assert row["conversions"] == 1
    assert row["revenue"] == 42.50


# ── connected data model: product <-> post ─────────────────────────────────

def test_create_post_syncs_social_platforms_posted_onto_product():
    ddf.save_product(_sample(product_id="social1"))
    ddf.create_post({"product": {"product_id": "social1"}, "platform": "tiktok", "copy": "Check this out."})
    product = ddf.get_product("social1")
    assert "tiktok" in (product["social_platforms_posted"] or "")
    assert product["date_posted"] is not None


def test_create_post_accumulates_multiple_platforms():
    ddf.save_product(_sample(product_id="social2"))
    ddf.create_post({"product": {"product_id": "social2"}, "platform": "instagram", "copy": "x"})
    ddf.create_post({"product": {"product_id": "social2"}, "platform": "facebook", "copy": "y"})
    product = ddf.get_product("social2")
    platforms = set((product["social_platforms_posted"] or "").split(","))
    assert platforms == {"instagram", "facebook"}


def test_get_product_posts_returns_all_posts_for_product():
    ddf.save_product(_sample(product_id="social3"))
    ddf.create_post({"product": {"product_id": "social3"}, "platform": "instagram", "copy": "x"})
    ddf.create_post({"product": {"product_id": "social3"}, "platform": "tiktok", "copy": "y"})
    posts = ddf.get_product_posts("social3")
    assert len(posts) == 2


# ── pre-existing behavior preserved (backward compatibility) ──────────────

def test_get_top_products_still_works_unchanged():
    ddf.save_product(_sample(product_id="top1"))
    top = ddf.get_top_products(limit=5)
    assert len(top) == 1
    assert top[0]["product_id"] == "top1"
