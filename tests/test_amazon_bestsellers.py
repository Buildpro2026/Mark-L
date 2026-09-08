"""actions/amazon_bestsellers.py — reading Amazon's own Best Sellers
ranking as a DDF discovery strategy.

The behaviour these guard, in order of how badly each would hurt:

  * A blocked scrape must never be reported as a successful one. If Amazon
    serves a CAPTCHA, the run says ACCESS_BLOCKED and saves nothing — an
    "empty result" would look identical to a genuinely quiet day and would
    put a fabricated silence into the catalogue.
  * "Top 10 across all categories combined" means exactly that: one unified
    ranked list. Not 10 per category, not one per category, not the first
    10 encountered, not keyword relevance.
  * Nothing is invented. A page without a price yields a product without a
    price, never a plausible-looking default.
"""
import pytest

from actions import amazon_bestsellers as ab
from actions import daily_deal_finders as ddf


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(ddf, "DB_PATH", tmp_path / "ddf.db")


# ══ FIXTURES: HTML shaped like the real pages ════════════════════════════

ROOT_HTML = """
<html><body>
  <div id="zg_left_col">
    <ul>
      <li><a href="/Best-Sellers-Tools/zgbs/hi">Tools &amp; Home Improvement</a></li>
      <li><a href="/Best-Sellers-Kitchen/zgbs/kitchen">Kitchen &amp; Dining</a></li>
      <li><a href="/gp/bestsellers/electronics/zgbs/electronics?ref=x">Electronics</a></li>
    </ul>
  </div>
  <div class="breadcrumb"><a href="https://www.amazon.com/Best-Sellers/zgbs">Any Department</a></div>
</body></html>
"""


def _card(asin, title, badge=None, price=None, rating=None, count=None, image=True):
    badge_html = f'<span class="zg-bdg-text">#{badge}</span>' if badge else ""
    price_html = f'<span class="a-price">${price}</span>' if price is not None else ""
    rating_html = (f'<a title="{rating} out of 5 stars"><span class="a-icon-alt">'
                   f'{rating} out of 5 stars</span></a>') if rating else ""
    count_html = f'<a class="a-size-small"><span>{count:,}</span></a>' if count else ""
    img_html = (f'<img alt="{title}" src="https://m.media-amazon.com/images/{asin}.jpg"/>'
                if image else f'<div class="p13n-sc-truncate-desktop-type2">{title}</div>')
    return f"""
    <div id="gridItemRoot">
      {badge_html}
      <a href="/Some-Product-Name/dp/{asin}/ref=zg_bs_hi_1">{img_html}</a>
      {rating_html}{count_html}{price_html}
    </div>"""


def _category_page(cards):
    return f"<html><body><div class='p13n-gridRow'>{''.join(cards)}</div></body></html>"


CAPTCHA_HTML = """
<html><body><h4>Enter the characters you see below</h4>
<p>Sorry, we just need to make sure you're not a robot.</p></body></html>
"""


class _FakePage:
    """Stands in for a Playwright Page. Production never constructs one of
    these — discover_top_sellers() only ever uses a page_factory a caller
    passed in, so there is no fake-scrape path inside the shipped code."""

    def __init__(self, pages: dict, fail_urls=(), final_url=None):
        self._pages = pages
        self._fail_urls = set(fail_urls)
        self.url = "about:blank"
        self.visited: list[str] = []
        self._final_url = final_url

    async def goto(self, url, **kwargs):
        self.visited.append(url)
        if url in self._fail_urls:
            raise RuntimeError("net::ERR_CONNECTION_RESET")
        self.url = self._final_url or url

    async def content(self):
        for key, html in self._pages.items():
            if key in self.url:
                return html
        return "<html><body>nothing here</body></html>"

    async def evaluate(self, script):
        return None

    async def wait_for_timeout(self, ms):
        return None


def _factory(page):
    class _CM:
        async def __aenter__(self_inner):
            return page

        async def __aexit__(self_inner, *a):
            return False
    return lambda: _CM()


# ══ CATEGORY DISCOVERY ═══════════════════════════════════════════════════

def test_categories_are_read_from_the_best_sellers_page_including_the_sidebar():
    categories = ab.parse_categories(ROOT_HTML)
    names = [c["name"] for c in categories]
    assert "Tools & Home Improvement" in names
    assert "Kitchen & Dining" in names
    assert "Electronics" in names


def test_category_urls_are_absolute_and_query_stripped():
    by_name = {c["name"]: c["url"] for c in ab.parse_categories(ROOT_HTML)}
    assert by_name["Tools & Home Improvement"] == "https://www.amazon.com/Best-Sellers-Tools/zgbs/hi"
    assert by_name["Electronics"].startswith("https://www.amazon.com/")
    assert "?" not in by_name["Electronics"]


def test_the_root_listing_is_not_traversed_as_one_of_its_own_categories():
    # The breadcrumb links back to /zgbs; following it would just re-read
    # page one and double-count everything on it.
    assert not any(c["url"].rstrip("/").endswith("/zgbs")
                   for c in ab.parse_categories(ROOT_HTML))


def test_categories_are_deduplicated_by_url():
    doubled = ROOT_HTML + ROOT_HTML
    urls = [c["url"] for c in ab.parse_categories(doubled)]
    assert len(urls) == len(set(urls))


# ══ PRODUCT + ASIN EXTRACTION ════════════════════════════════════════════

def test_asin_is_extracted_from_every_amazon_url_shape():
    assert ab.extract_asin("/Some-Name/dp/B0ABCD1234/ref=zg_bs_1") == "B0ABCD1234"
    assert ab.extract_asin("https://www.amazon.com/dp/B01234WXYZ") == "B01234WXYZ"
    assert ab.extract_asin("https://www.amazon.com/gp/product/B0000AAAAA/") == "B0000AAAAA"


def test_a_url_without_an_asin_returns_none_rather_than_a_guess():
    assert ab.extract_asin("https://www.amazon.com/Best-Sellers/zgbs") is None
    assert ab.extract_asin("") is None
    assert ab.extract_asin("/dp/TOOSHORT") is None


def test_products_are_extracted_with_every_available_field():
    html = _category_page([_card("B0ABCD1234", "Rotary Hammer XR", badge=1,
                                 price="429.00", rating=4.7, count=2841)])
    products = ab.parse_products(html, category="Tools")

    assert len(products) == 1
    p = products[0]
    assert p["asin"] == "B0ABCD1234"
    assert p["name"] == "Rotary Hammer XR"
    assert p["price"] == 429.00
    assert p["url"] == "https://www.amazon.com/dp/B0ABCD1234"
    assert p["category"] == "Tools"
    assert p["position"] == 1
    assert p["position_source"] == "badge"
    assert p["image_url"].endswith("B0ABCD1234.jpg")
    assert p["rating"] == 4.7
    assert p["rating_count"] == 2841


def test_grid_order_is_used_as_the_position_when_amazon_shows_no_badge():
    html = _category_page([_card("B000000001", "First"), _card("B000000002", "Second")])
    products = ab.parse_products(html, category="Tools")
    assert [p["position"] for p in products] == [1, 2]
    assert {p["position_source"] for p in products} == {"grid_order"}


def test_missing_optional_fields_are_omitted_never_defaulted():
    html = _category_page([_card("B0NOPRICE1", "No Price Here", badge=3, image=False)])
    product = ab.parse_products(html, category="Tools")[0]
    for optional in ("price", "image_url", "rating", "rating_count"):
        assert optional not in product, f"{optional} was invented"
    assert product["name"] == "No Price Here"
    assert product["asin"] == "B0NOPRICE1"


def test_a_card_without_an_asin_is_skipped_not_saved_under_a_made_up_id():
    html = "<html><body><div id='gridItemRoot'><a href='/promo/deals'>A Banner</a></div></body></html>"
    assert ab.parse_products(html, category="Tools") == []


def test_pagination_offset_continues_the_ranking_rather_than_restarting_it():
    html = _category_page([_card("B000000051", "Page two item")])
    product = ab.parse_products(html, category="Tools", position_offset=50)[0]
    assert product["position"] == 51


# ══ DEDUPLICATION ════════════════════════════════════════════════════════

def test_duplicate_asins_across_categories_merge_into_one_record():
    combined = ab.deduplicate([
        {"asin": "B0DUPE0001", "name": "Air Fryer", "position": 7, "category": "Kitchen"},
        {"asin": "B0DUPE0001", "name": "Air Fryer", "position": 2, "category": "Home"},
        {"asin": "B0OTHER001", "name": "Drill", "position": 1, "category": "Tools"},
    ])
    assert len(combined) == 2
    air_fryer = next(c for c in combined if c["asin"] == "B0DUPE0001")
    # The best position it achieved anywhere, and every list it charted on.
    assert air_fryer["position"] == 2
    assert sorted(air_fryer["categories"]) == ["Home", "Kitchen"]


def test_merging_fills_in_a_field_one_page_showed_and_another_did_not():
    combined = ab.deduplicate([
        {"asin": "B0DUPE0001", "name": "Air Fryer", "position": 7, "category": "Kitchen"},
        {"asin": "B0DUPE0001", "name": "Air Fryer", "position": 9,
         "category": "Home", "price": 89.99, "rating": 4.5},
    ])
    assert combined[0]["price"] == 89.99
    assert combined[0]["rating"] == 4.5


def test_a_candidate_without_an_asin_cannot_enter_the_combined_set():
    assert ab.deduplicate([{"name": "Mystery", "position": 1}]) == []


# ══ COMBINED RANKING ═════════════════════════════════════════════════════

def _c(asin, position, categories=("Tools",), **extra):
    return {"asin": asin, "name": asin, "position": position,
            "categories": list(categories), **extra}


def test_ranking_is_one_combined_list_led_by_the_strongest_sales_rank():
    ranked = ab.combined_rank([_c("B0000000C3", 30), _c("B0000000A1", 1), _c("B0000000B2", 5)])
    assert [p["asin"] for p in ranked] == ["B0000000A1", "B0000000B2", "B0000000C3"]
    assert ranked[0]["sales_rank_score"] > ranked[-1]["sales_rank_score"]


def test_charting_in_several_categories_outranks_a_single_list_appearance():
    broad = _c("B0BROAD001", 6, categories=("Tools", "Home", "Kitchen"))
    narrow = _c("B0NARROW01", 6, categories=("Tools",))
    ranked = ab.combined_rank([narrow, broad])
    assert ranked[0]["asin"] == "B0BROAD001"


def test_missing_signals_score_zero_rather_than_an_assumed_average():
    bare = ab.combined_rank([_c("B0BARE0001", 4)])[0]
    rated = ab.combined_rank([_c("B0RATED001", 4, rating=4.8, rating_count=9000)])[0]
    assert bare["sales_rank_score"] < rated["sales_rank_score"]


def test_ranking_is_deterministic_for_the_same_input():
    items = [_c("B0000000A1", 3), _c("B0000000B2", 3), _c("B0000000C3", 3)]
    assert ([p["asin"] for p in ab.combined_rank(items)]
            == [p["asin"] for p in ab.combined_rank(list(reversed(items)))])


# ══ SELECTING EXACTLY TEN ════════════════════════════════════════════════

def _spread(n_categories=5, per_category=8):
    """Products across several categories, positions 1..per_category in each
    — the shape that catches "top 10 per category" and "one per category"."""
    out = []
    for c in range(n_categories):
        for pos in range(1, per_category + 1):
            out.append({"asin": f"B{c}{pos:08d}", "name": f"cat{c}-{pos}",
                        "position": pos, "category": f"Category {c}"})
    return out


def test_exactly_ten_are_selected_from_forty_across_five_categories():
    top = ab.select_top(_spread(), limit=10)
    assert len(top) == 10


def test_the_top_ten_is_combined_not_ten_from_each_category():
    top = ab.select_top(_spread(), limit=10)
    assert len({p["asin"] for p in top}) == 10
    # Every #1 across the five categories must be in the combined ten;
    # "10 from each category" would have returned only Category 0.
    assert sum(1 for p in top if p["position"] == 1) == 5


def test_the_top_ten_is_not_one_product_per_category():
    top = ab.select_top(_spread(), limit=10)
    categories = [p["category"] for p in top]
    assert len(categories) > len(set(categories)), "one-per-category, not a combined ranking"


def test_the_top_ten_is_not_simply_the_first_ten_encountered():
    ordered = _spread()
    first_ten = {p["asin"] for p in ordered[:10]}
    assert {p["asin"] for p in ab.select_top(ordered, limit=10)} != first_ten


def test_fewer_than_ten_results_returns_what_was_actually_found():
    top = ab.select_top([_c("B0000000A1", 1), _c("B0000000B2", 2)], limit=10)
    assert len(top) == 2


def test_no_results_returns_an_empty_list_not_an_error():
    assert ab.select_top([], limit=10) == []


# ══ ACCESS BLOCKS ════════════════════════════════════════════════════════

def test_a_captcha_page_is_detected():
    assert ab.detect_access_block(CAPTCHA_HTML) == "CAPTCHA"


def test_a_bot_check_and_a_sign_in_wall_are_detected():
    assert ab.detect_access_block("<p>Robot Check</p>") == "BOT_CHECK"
    assert ab.detect_access_block("", "https://www.amazon.com/ap/signin?x=1") == "AUTH_WALL"


def test_a_normal_page_is_not_mistaken_for_a_block():
    assert ab.detect_access_block(ROOT_HTML, "https://www.amazon.com/Best-Sellers/zgbs") is None


def test_a_captcha_stops_the_run_and_saves_nothing():
    page = _FakePage({"zgbs": CAPTCHA_HTML})
    result = ab.discover_top_sellers(limit=10, page_factory=_factory(page))

    assert result["ok"] is False
    assert result["state"] == ab.STATE_ACCESS_BLOCKED
    assert result["block_kind"] == "CAPTCHA"
    assert result["saved"] == 0
    assert "bypass" in result["detail"].lower()
    # And it must not read as a quiet day with no bestsellers.
    assert "CAPTCHA" in result["detail"]


def test_a_captcha_part_way_through_stops_immediately_rather_than_hammering():
    page = _FakePage({
        "Best-Sellers/zgbs": ROOT_HTML,
        "zgbs/hi": CAPTCHA_HTML,
        "zgbs/kitchen": _category_page([_card("B0KITCHEN1", "Air Fryer", badge=1)]),
    })
    result = ab.discover_top_sellers(limit=10, page_factory=_factory(page))
    assert result["state"] == ab.STATE_ACCESS_BLOCKED
    assert not any("kitchen" in url for url in page.visited), "kept scraping after a block"


# ══ NAVIGATION AND TRANSIENT FAILURE ═════════════════════════════════════

def test_the_run_fails_honestly_when_the_best_sellers_page_never_loads():
    page = _FakePage({}, fail_urls={ab.BESTSELLERS_URL})
    result = ab.discover_top_sellers(limit=10, page_factory=_factory(page))
    assert result["ok"] is False
    assert result["state"] == ab.STATE_FAILED
    assert result["saved"] == 0
    assert any(e["event"] == "navigation_failed" for e in result["log"])


def test_a_category_that_fails_to_load_does_not_sink_the_whole_run():
    page = _FakePage({
        "Best-Sellers/zgbs": ROOT_HTML,
        "zgbs/kitchen": _category_page([_card("B0KITCHEN1", "Air Fryer", badge=1, price="89.99")]),
        "zgbs/electronics": _category_page([_card("B0ELEC0001", "Earbuds", badge=1, price="59.00")]),
    }, fail_urls={"https://www.amazon.com/Best-Sellers-Tools/zgbs/hi"})

    result = ab.discover_top_sellers(limit=10, page_factory=_factory(page))
    assert result["ok"] is True
    assert result["categories_failed"] == 1
    assert {p["asin"] for p in result["products"]} == {"B0KITCHEN1", "B0ELEC0001"}
    assert any(e["event"] == "category_failed" for e in result["log"])


def test_a_page_with_no_recognisable_categories_reports_that_exactly():
    page = _FakePage({"zgbs": "<html><body><p>Nothing familiar</p></body></html>"})
    result = ab.discover_top_sellers(limit=10, page_factory=_factory(page))
    assert result["state"] == ab.STATE_FAILED
    assert "categor" in result["detail"].lower()


# ══ END TO END THROUGH THE REAL CATALOG ══════════════════════════════════

def _full_site():
    return _FakePage({
        "Best-Sellers/zgbs": ROOT_HTML,
        "zgbs/hi": _category_page([
            _card("B0TOOL0001", "Rotary Hammer XR", badge=1, price="429.00", rating=4.7, count=2841),
            _card("B0SHARED01", "Shop Vacuum", badge=4, price="119.00", rating=4.4, count=900),
            _card("B0TOOL0003", "Socket Set", badge=9, price="79.00"),
        ]),
        "zgbs/kitchen": _category_page([
            _card("B0KITCHEN1", "Air Fryer", badge=1, price="89.99", rating=4.6, count=51000),
            _card("B0SHARED01", "Shop Vacuum", badge=2, price="119.00"),
        ]),
        "zgbs/electronics": _category_page([
            _card("B0ELEC0001", "Wireless Earbuds", badge=1, price="59.00", rating=4.3, count=12000),
            _card("B0ELEC0002", "Streaming Stick", badge=6, price="39.99"),
        ]),
    })


def test_a_full_traversal_saves_the_combined_top_products_to_the_real_catalog():
    result = ab.discover_top_sellers(limit=10, page_factory=_factory(_full_site()))

    assert result["ok"] is True
    assert result["state"] == ab.STATE_RAN
    assert result["categories_discovered"] == 3
    assert result["categories_processed"] == 3
    # 7 rows scraped, one ASIN charting twice → 6 unique products.
    assert len(result["products"]) == 6
    assert result["saved"] == 6

    stored = ddf.get_product("B0TOOL0001")
    assert stored is not None
    assert stored["name"] == "Rotary Hammer XR"
    assert stored["retailer"] == "amazon"
    assert stored["source"] == "amazon_bestsellers"
    assert stored["current_price"] == 429.00
    # Saved as DISCOVERED — discovery never publishes.
    assert stored["status"] == ddf.STATUS_DISCOVERED
    assert stored["approved"] == 0


def test_the_cross_category_product_is_ranked_on_its_best_position_and_breadth():
    result = ab.discover_top_sellers(limit=10, page_factory=_factory(_full_site()))
    vacuum = next(p for p in result["products"] if p["asin"] == "B0SHARED01")
    assert vacuum["position"] == 2              # its best, not the #4 it also held
    assert sorted(vacuum["categories"]) == ["Kitchen & Dining", "Tools & Home Improvement"]


def test_selecting_exactly_ten_when_more_than_ten_products_exist():
    page = _FakePage({
        "Best-Sellers/zgbs": ROOT_HTML,
        "zgbs/hi": _category_page([_card(f"B0T{i:07d}", f"Tool {i}", badge=i) for i in range(1, 9)]),
        "zgbs/kitchen": _category_page([_card(f"B0K{i:07d}", f"Kitchen {i}", badge=i) for i in range(1, 9)]),
        "zgbs/electronics": _category_page([_card(f"B0E{i:07d}", f"Elec {i}", badge=i) for i in range(1, 9)]),
    })
    result = ab.discover_top_sellers(limit=10, page_factory=_factory(page))
    assert len(result["products"]) == 10
    assert result["saved"] == 10
    # All three category leaders survive into the combined ten.
    assert {"B0T0000001", "B0K0000001", "B0E0000001"} <= {p["asin"] for p in result["products"]}


def test_the_log_accounts_for_every_required_stage():
    result = ab.discover_top_sellers(limit=10, page_factory=_factory(_full_site()))
    events = [e["event"] for e in result["log"]]
    for required in ("amazon_discovery_started", "bestsellers_page_reached",
                     "categories_discovered", "category_processed",
                     "categories_processed", "duplicates_removed",
                     "final_candidates", "final_top", "products_saved"):
        assert required in events, f"missing log event: {required}"

    dedupe = next(e for e in result["log"] if e["event"] == "duplicates_removed")
    assert dedupe["extracted"] == 7 and dedupe["unique"] == 6


def test_derived_sales_signal_is_labelled_as_rank_derived_not_measured():
    record = ab.to_ddf_product({"asin": "B0TOOL0001", "name": "Rotary Hammer XR",
                                "url": "https://www.amazon.com/dp/B0TOOL0001",
                                "position": 1, "categories": ["Tools"]})
    assert record["notes"]["discovery_confidence"] == "amazon_bestsellers_rank"
    assert record["notes"]["bestseller_position"] == 1
    # Dimensions these pages genuinely cannot see stay at 0.
    assert record["demand"] == 0 and record["competition"] == 0


def test_an_unavailable_browser_reports_unavailable_rather_than_pretending():
    from actions import browser_control
    import actions.amazon_bestsellers as module
    original = browser_control.automation_available
    try:
        browser_control.automation_available = lambda: (False, "playwright is not installed")
        result = module.discover_top_sellers(limit=10)
    finally:
        browser_control.automation_available = original
    assert result["ok"] is False
    assert result["state"] == ab.STATE_UNAVAILABLE
    assert result["saved"] == 0


# ══ THROUGH THE REAL TOOL EXECUTOR ═══════════════════════════════════════
# Not a stubbed executor: the actual ToolExecutor the headless service uses,
# so a wiring mistake between the tool schema, the executor branch and the
# workflow cannot pass unnoticed.

def test_the_objective_runs_end_to_end_through_the_real_tool_executor(monkeypatch):
    import asyncio
    from core.headless.tool_executor import ToolExecutor, ToolContext
    from actions import amazon_bestsellers as module, ddf_workflow, buffer_integration as buf

    real = module.discover_top_sellers
    monkeypatch.setattr(module, "discover_top_sellers",
                        lambda **kwargs: real(limit=kwargs.get("limit", 10),
                                              page_factory=_factory(_full_site())))

    def _must_not_publish(*args, **kwargs):
        raise AssertionError("published without approval")
    monkeypatch.setattr(buf, "publish_to_buffer", _must_not_publish)

    result = asyncio.run(ToolExecutor(ToolContext()).execute("daily_deal_finders", {
        "action": "run_workflow",
        "objective": "Find the top 10 selling products on Amazon for Daily Deal Finders",
    }))

    assert "amazon_bestsellers" in result
    assert "smaller steps" not in result.lower()
    # It stops at the approval gate rather than posting anything publicly.
    assert "approval" in result.lower()
    assert ddf_workflow.select_find_strategy(
        "Find the top 10 selling products on Amazon for Daily Deal Finders"
    ) == "amazon_bestsellers"


def test_the_tool_executor_reports_an_amazon_block_instead_of_claiming_success(monkeypatch):
    import asyncio
    from core.headless.tool_executor import ToolExecutor, ToolContext
    from actions import amazon_bestsellers as module

    page = _FakePage({"zgbs": CAPTCHA_HTML})
    real = module.discover_top_sellers
    monkeypatch.setattr(module, "discover_top_sellers",
                        lambda **kwargs: real(limit=kwargs.get("limit", 10),
                                              page_factory=_factory(page)))

    result = asyncio.run(ToolExecutor(ToolContext()).execute("daily_deal_finders", {
        "action": "run_workflow",
        "objective": "Find the top 10 selling products on Amazon",
    }))
    assert "CAPTCHA" in result


def test_pagination_continues_the_ranking_into_the_second_page():
    page_two = ("<html><body>"
                + _category_page([_card("B0PAGE0051", "Rank fifty one")])
                + "</body></html>")
    page = _FakePage({
        "Best-Sellers/zgbs": ROOT_HTML,
        "pg=2": page_two,
        "zgbs/hi": _category_page([_card("B0PAGE0001", "Rank one", badge=1)])
                   + '<li class="a-last"><a href="/Best-Sellers-Tools/zgbs/hi?pg=2">Next page</a></li>',
        "zgbs/kitchen": _category_page([]),
        "zgbs/electronics": _category_page([]),
    })
    result = ab.discover_top_sellers(limit=10, max_pages_per_category=2,
                                     page_factory=_factory(page))
    asins = {p["asin"] for p in result["products"]}
    assert "B0PAGE0051" in asins, "page two was never fetched"
    # Page two's first card is rank 51, not a second rank 1.
    assert next(p for p in result["products"] if p["asin"] == "B0PAGE0051")["position"] == 51
