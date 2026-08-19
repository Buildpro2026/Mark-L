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
