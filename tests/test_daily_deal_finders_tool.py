"""main.py's "daily_deal_finders" voice/chat tool — add_product / publish
(approval-gated) / status / high_ticket_picks / you_might_have_missed /
this_weeks_hottest, wired into actions/daily_deal_finders.py.
"""
import asyncio
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_live(name="jarvis_main_ddf_tool"):
    main = load_module(name, "main.py")
    live = object.__new__(main.JarvisLive)
    return main, live


def _ui_stub():
    return type("UIStub", (), {
        "muted": False,
        "set_state": lambda self, s: None,
        "write_log": lambda self, m: None,
    })()


def _live(main):
    live = object.__new__(main.JarvisLive)
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    return live


def _make_fc(**args):
    return type("FC", (), {"id": "call-1", "name": "daily_deal_finders", "args": args})()


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch, tmp_path):
    from actions import daily_deal_finders as ddf
    monkeypatch.setattr(ddf, "DB_PATH", tmp_path / "test_ddf_tool.db")
    from actions import audit_log
    monkeypatch.setattr(audit_log, "DB_PATH", tmp_path / "test_audit.db")


def test_tool_declared_for_gemini():
    main, _ = _new_live()
    names = [t["name"] for t in main.TOOL_DECLARATIONS]
    assert "daily_deal_finders" in names


def test_add_product_without_name_or_price_is_refused():
    main, live = _new_live()
    l = _live(main)
    response = _run(l._execute_tool(_make_fc(action="add_product")))
    assert "name" in response.response["result"].lower() or "price" in response.response["result"].lower()


def test_add_product_creates_a_discovered_record_never_public():
    main, live = _new_live()
    l = _live(main)
    response = _run(l._execute_tool(_make_fc(
        action="add_product", name="Test Gadget", price=29.99, category="gadgets",
        url="https://example.com/x", retailer="amazon",
    )))
    result = response.response["result"].lower()
    assert "added to ddf" in result
    assert "not visible on the site yet" in result

    from actions import daily_deal_finders as ddf
    products = ddf.list_products(approved_only=False, limit=50)
    assert len(products) == 1
    assert products[0]["status"] == ddf.STATUS_DISCOVERED
    assert products[0]["approved"] == 0


def test_add_product_rejects_unapproved_retailer():
    main, live = _new_live()
    l = _live(main)
    response = _run(l._execute_tool(_make_fc(
        action="add_product", name="Test Gadget", price=29.99, retailer="walmart",
    )))
    assert "not approved yet" in response.response["result"].lower()


def test_publish_without_approval_stops_and_asks():
    main, live = _new_live()
    l = _live(main)
    _run(l._execute_tool(_make_fc(action="add_product", name="Gadget", price=50.0, product_id="p1")))

    response = _run(l._execute_tool(_make_fc(action="publish", product_id="p1")))
    result = response.response["result"].lower()
    assert "needs your explicit approval" in result

    from actions import daily_deal_finders as ddf
    product = ddf.get_product("p1")
    assert product["status"] != ddf.STATUS_PUBLISHED
    assert product["approved"] == 0


def test_publish_with_approval_actually_goes_live():
    main, live = _new_live()
    l = _live(main)
    _run(l._execute_tool(_make_fc(action="add_product", name="Gadget", price=50.0, product_id="p1")))

    response = _run(l._execute_tool(_make_fc(action="publish", product_id="p1", approved=True)))
    result = response.response["result"].lower()
    assert "live on the site now" in result

    from actions import daily_deal_finders as ddf
    product = ddf.get_product("p1")
    assert product["status"] == ddf.STATUS_PUBLISHED
    assert product["approved"] == 1


def test_publish_missing_product_is_reported_honestly():
    main, live = _new_live()
    l = _live(main)
    response = _run(l._execute_tool(_make_fc(action="publish", product_id="ghost", approved=True)))
    assert "couldn't publish" in response.response["result"].lower()


def test_status_reports_current_lifecycle_stage():
    main, live = _new_live()
    l = _live(main)
    _run(l._execute_tool(_make_fc(action="add_product", name="Gadget", price=50.0, product_id="p1")))
    response = _run(l._execute_tool(_make_fc(action="status", product_id="p1")))
    assert "discovered" in response.response["result"].lower()


def test_high_ticket_picks_reports_honestly_when_none_ready():
    main, live = _new_live()
    l = _live(main)
    response = _run(l._execute_tool(_make_fc(action="high_ticket_picks")))
    assert "no high-ticket" in response.response["result"].lower()


def test_you_might_have_missed_reports_honestly_when_empty():
    main, live = _new_live()
    l = _live(main)
    response = _run(l._execute_tool(_make_fc(action="you_might_have_missed")))
    assert "nothing published" in response.response["result"].lower()


def test_unknown_action_reports_a_clear_error():
    main, live = _new_live()
    l = _live(main)
    response = _run(l._execute_tool(_make_fc(action="not_a_real_action")))
    assert "unknown daily_deal_finders action" in response.response["result"].lower()


# ── trending (2026-09-03: "what's on Deals Trending" used to have no ────
# tool action at all — get_trending_deals() existed but nothing exposed it) ─

def test_trending_reports_honestly_when_nothing_is_trending():
    main, live = _new_live()
    l = _live(main)
    response = _run(l._execute_tool(_make_fc(action="trending")))
    assert "nothing is trending yet" in response.response["result"].lower()


def test_trending_surfaces_a_published_product():
    main, live = _new_live()
    l = _live(main)
    _run(l._execute_tool(_make_fc(action="add_product", name="Trendy Gadget", price=19.99, product_id="tg1")))
    _run(l._execute_tool(_make_fc(action="publish", product_id="tg1", approved=True)))

    response = _run(l._execute_tool(_make_fc(action="trending")))
    assert "trendy gadget" in response.response["result"].lower()


# ── discover (2026-09-03: real product discovery, replaces "JARVIS can't ──
# pull Amazon products, build a CSV" for the configured case) ────────────

def test_discover_reports_not_configured_without_an_api_key(monkeypatch):
    from core.headless import config as hc
    monkeypatch.setattr(hc, "PRODUCT_DATA_API_KEY", None)
    main, live = _new_live()
    l = _live(main)
    response = _run(l._execute_tool(_make_fc(action="discover")))
    result = response.response["result"].lower()
    assert "product_data_api_key" in result or "not configured" in result or "no product-data api key" in result


def test_discover_saves_candidates_when_a_source_is_wired(monkeypatch):
    from core.headless import config as hc
    from actions import ddf_discovery
    monkeypatch.setattr(hc, "PRODUCT_DATA_API_KEY", "fake-key")

    class _FakeSource:
        name = "fake"

        def search(self, query, limit):
            return [{
                "name": "Discovered Gadget", "source": "fake_api", "price": 15.0, "current_price": 15.0,
                "product_id": "disc-1", "retailer": "amazon",
                "sales_signal": 0, "demand": 0, "trend_strength": 0, "competition": 0,
                "content_potential": 0, "repeatability": 0, "historical_performance": 0,
            }]

    monkeypatch.setattr(ddf_discovery, "_active_source", lambda: _FakeSource())
    main, live = _new_live()
    l = _live(main)
    response = _run(l._execute_tool(_make_fc(action="discover", queries="testcat")))
    result = response.response["result"].lower()
    assert "1 new candidate" in result

    from actions import daily_deal_finders as ddf
    product = ddf.get_product("disc-1")
    assert product is not None
    assert product["status"] == ddf.STATUS_DISCOVERED
