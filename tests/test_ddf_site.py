import pytest
from fastapi.testclient import TestClient

from actions import daily_deal_finders as ddf
from ddf_site.server import DDFSiteServer


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch, tmp_path):
    monkeypatch.setattr(ddf, "DB_PATH", tmp_path / "test_ddf_site.db")


def _client():
    return TestClient(DDFSiteServer().app)


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


# ── all 12 required pages exist and render ─────────────────────────────

@pytest.mark.parametrize("path", [
    "/", "/todays-deals", "/trending", "/this-week", "/best-sellers", "/high-ticket",
    "/you-might-have-missed", "/categories", "/amazon", "/tiktok-shop",
    "/about", "/affiliate-disclosure", "/contact",
])
def test_static_page_returns_200(path):
    client = _client()
    r = client.get(path)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_deal_detail_page_returns_200_even_before_data_loads_client_side():
    """The page shell renders immediately; product-detail.html data loads
    async via JS/fetch — this just confirms the route itself works."""
    client = _client()
    r = client.get("/deal/anything")
    assert r.status_code == 200


def test_category_page_returns_200():
    client = _client()
    r = client.get("/category/electronics")
    assert r.status_code == 200


def test_home_page_links_to_all_required_sections():
    client = _client()
    html = client.get("/").text
    assert "Today's Deals" in html
    assert "Trending" in html


def test_affiliate_disclosure_mentions_no_target_or_walmart():
    client = _client()
    html = client.get("/affiliate-disclosure").text
    normalized = " ".join(html.split())
    assert "Target" in html and "Walmart" in html
    assert "do not currently have an affiliate relationship" in normalized


def test_pages_include_prominent_cta_wiring():
    """CTA buttons are rendered client-side (site.js) — confirm the shared
    JS that builds them ships GET DEAL / SHOP NOW text."""
    client = _client()
    js = client.get("/static/js/site.js").text
    assert "GET DEAL" in js
    assert "SHOP NOW" in js


# ── JSON API — honest empty states, never fabricated ────────────────────

def test_api_deals_today_empty_state():
    client = _client()
    r = client.get("/api/deals/today")
    assert r.status_code == 200
    assert r.json() == {"deals": []}


def test_api_categories_empty_state():
    client = _client()
    r = client.get("/api/categories")
    assert r.json() == {"categories": []}


def test_api_deal_detail_404_for_unknown_slug():
    client = _client()
    r = client.get("/api/deal/does-not-exist")
    assert r.status_code == 404


# ── JSON API with real data ──────────────────────────────────────────────

def test_api_deals_today_returns_saved_product():
    ddf.save_product(_sample())
    client = _client()
    r = client.get("/api/deals/today")
    deals = r.json()["deals"]
    assert len(deals) == 1
    assert deals[0]["name"] == "Wireless Earbuds Pro"
    assert deals[0]["retailer"] == "amazon"


def test_api_deals_filters_by_retailer():
    ddf.save_product(_sample(product_id="p1", retailer="amazon"))
    ddf.save_product(_sample(product_id="p2", retailer="tiktok_shop", name="TikTok Find"))
    client = _client()
    r = client.get("/api/deals?retailer=tiktok_shop")
    deals = r.json()["deals"]
    assert len(deals) == 1
    assert deals[0]["product_id"] == "p2"


def test_amazon_page_api_only_shows_amazon_deals():
    ddf.save_product(_sample(product_id="p1", retailer="amazon"))
    ddf.save_product(_sample(product_id="p2", retailer="tiktok_shop", name="TikTok Find"))
    client = _client()
    r = client.get("/api/deals?retailer=amazon")
    deals = r.json()["deals"]
    assert len(deals) == 1
    assert deals[0]["retailer"] == "amazon"


def test_api_deal_detail_returns_full_record():
    p = ddf.save_product(_sample())
    client = _client()
    r = client.get(f"/api/deal/{p['slug']}")
    assert r.status_code == 200
    assert r.json()["deal"]["affiliate_url"] == "https://amzn.to/abc123"


def test_unapproved_product_does_not_appear_in_listings():
    ddf.save_product(_sample(approved=False))
    client = _client()
    r = client.get("/api/deals/today")
    assert r.json()["deals"] == []


# ── click/view tracking round-trip through the real HTTP API ───────────

def test_track_view_via_api_increments_real_record():
    ddf.save_product(_sample())
    client = _client()
    r = client.post("/api/track/view/earbuds-001")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert ddf.get_product("earbuds-001")["views"] == 1


def test_track_click_via_api_increments_real_record():
    ddf.save_product(_sample())
    client = _client()
    r = client.post("/api/track/click/earbuds-001")
    assert r.json()["ok"] is True
    assert ddf.get_product("earbuds-001")["affiliate_clicks"] == 1


def test_track_view_unknown_product_reports_false_not_error():
    client = _client()
    r = client.post("/api/track/view/does-not-exist")
    assert r.status_code == 200
    assert r.json()["ok"] is False


# ── static assets served ─────────────────────────────────────────────────

def test_static_css_is_served():
    client = _client()
    r = client.get("/static/css/style.css")
    assert r.status_code == 200


def test_static_js_is_served():
    client = _client()
    r = client.get("/static/js/site.js")
    assert r.status_code == 200


# ── high-ticket / best-sellers / trending endpoints ─────────────────────

def test_high_ticket_endpoint_respects_min_price():
    ddf.save_product(_sample(product_id="cheap", current_price=10.0, name="Cheap"))
    ddf.save_product(_sample(product_id="pricey", current_price=300.0, name="Pricey"))
    client = _client()
    r = client.get("/api/deals/high-ticket?min_price=100")
    ids = {d["product_id"] for d in r.json()["deals"]}
    assert ids == {"pricey"}


def test_trending_and_best_sellers_endpoints_return_200():
    client = _client()
    assert client.get("/api/deals/trending").status_code == 200
    assert client.get("/api/deals/best-sellers").status_code == 200


# ── Phase 3: high-ticket picks / You Might Have Missed / this week ──────

def test_high_ticket_picks_endpoint_returns_at_most_two_by_default():
    for i in range(5):
        ddf.save_product(_sample(product_id=f"p{i}", name=f"Product {i}", current_price=150.0 + i, approved=True))
    client = _client()
    r = client.get("/api/deals/high-ticket-picks")
    assert r.status_code == 200
    assert len(r.json()["deals"]) == 2


def test_home_page_shows_high_ticket_picks_and_missed_sections():
    client = _client()
    html = client.get("/").text
    assert "High-Ticket Picks" in html
    assert "You Might Have Missed" in html
    assert "This Week's Hottest" in html


def test_you_might_have_missed_endpoint_excludes_named_product():
    from datetime import datetime, timedelta, timezone
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    ddf.save_product(_sample(product_id="p1", published_date=yesterday, approved=True))
    ddf.save_product(_sample(product_id="p2", name="Other", published_date=yesterday, approved=True))
    client = _client()
    r = client.get("/api/deals/you-might-have-missed?exclude=p1")
    ids = {d["product_id"] for d in r.json()["deals"]}
    assert "p1" not in ids
    assert "p2" in ids


def test_this_week_endpoint_returns_200_with_empty_catalog():
    client = _client()
    r = client.get("/api/deals/this-week")
    assert r.status_code == 200
    assert r.json() == {"deals": []}


def test_product_detail_js_renders_missed_strip():
    js = _client().get("/static/js/site.js").text
    assert "renderMissedStrip" in js
    assert "you-might-have-missed" in js
