"""actions/ddf_discovery.py — real, pluggable DDF product discovery
(Lee's autonomous-CEO/COS spec, Section FIFTH). Never fabricates a
product: honestly NOT_CONFIGURED with no API key, and only ever saves
what an adapter's search() genuinely returned.
"""
import pytest

from actions import daily_deal_finders as ddf
from actions import ddf_discovery
from core.headless import config as hc


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(ddf, "DB_PATH", tmp_path / "ddf.db")
    monkeypatch.setattr(hc, "PRODUCT_DATA_API_KEY", None)
    monkeypatch.setattr(hc, "PRODUCT_DATA_API_PROVIDER", "rainforest")


def test_not_configured_without_an_api_key():
    assert ddf_discovery.is_configured() is False
    result = ddf_discovery.discover_new_products()
    assert result == {
        "ok": False, "state": "NOT_CONFIGURED", "provider": "rainforest",
        "detail": result["detail"], "discovered": [], "saved": 0, "errors": [],
    }
    assert "PRODUCT_DATA_API_KEY" in result["detail"]


def test_unknown_provider_is_treated_as_not_configured(monkeypatch):
    monkeypatch.setattr(hc, "PRODUCT_DATA_API_KEY", "fake-key")
    monkeypatch.setattr(hc, "PRODUCT_DATA_API_PROVIDER", "some_provider_nobody_wrote_an_adapter_for")
    assert ddf_discovery.is_configured() is False


def test_configured_source_saves_real_candidates_as_discovered(monkeypatch):
    monkeypatch.setattr(hc, "PRODUCT_DATA_API_KEY", "fake-key")

    class _FakeSource:
        name = "fake"

        def search(self, query, limit):
            return [{
                "name": f"Widget for {query}", "source": "fake_api", "category": query,
                "price": 42.0, "current_price": 42.0, "url": "https://example.com/widget",
                "product_id": f"ASIN-{query[:4]}", "retailer": "amazon",
                "sales_signal": 0, "demand": 0, "trend_strength": 0, "competition": 0,
                "content_potential": 0, "repeatability": 0, "historical_performance": 0,
            }]

    monkeypatch.setattr(ddf_discovery, "_active_source", lambda: _FakeSource())
    result = ddf_discovery.discover_new_products(queries=["testcat"], limit_per_query=5)

    assert result["ok"] is True
    assert result["state"] == "RAN"
    assert result["saved"] == 1
    assert result["errors"] == []

    saved = ddf.get_product(f"ASIN-test")
    assert saved is not None
    assert saved["status"] == ddf.STATUS_DISCOVERED
    assert saved["approved"] == 0  # never auto-published


def test_source_returning_nothing_saves_nothing_and_reports_no_error(monkeypatch):
    monkeypatch.setattr(hc, "PRODUCT_DATA_API_KEY", "fake-key")

    class _EmptySource:
        name = "fake"

        def search(self, query, limit):
            return []

    monkeypatch.setattr(ddf_discovery, "_active_source", lambda: _EmptySource())
    result = ddf_discovery.discover_new_products(queries=["nothing here"])
    assert result["ok"] is True
    assert result["saved"] == 0
    assert result["errors"] == []


def test_a_bad_candidate_is_reported_as_an_error_not_silently_dropped(monkeypatch):
    monkeypatch.setattr(hc, "PRODUCT_DATA_API_KEY", "fake-key")

    class _BadRetailerSource:
        name = "fake"

        def search(self, query, limit):
            return [{
                "name": "Bad Retailer Widget", "price": 10.0, "current_price": 10.0,
                "product_id": "bad-1", "retailer": "walmart",  # not an approved retailer
            }]

    monkeypatch.setattr(ddf_discovery, "_active_source", lambda: _BadRetailerSource())
    result = ddf_discovery.discover_new_products(queries=["x"])
    assert result["saved"] == 0
    assert len(result["errors"]) == 1


def test_rainforest_source_search_handles_network_failure_gracefully(monkeypatch):
    import requests

    def _raise(*a, **kw):
        raise requests.RequestException("boom")

    monkeypatch.setattr(requests, "get", _raise)
    source = ddf_discovery.RainforestApiSource("key", "https://example.com/request")
    assert source.search("anything", 5) == []


def test_rainforest_source_skips_results_missing_asin_or_price(monkeypatch):
    import requests

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"search_results": [
                {"title": "No ASIN", "price": {"value": 9.99}},
                {"asin": "B000123", "title": "No price"},
                {"asin": "B000456", "title": "Complete", "price": {"value": 19.99}, "link": "https://x"},
            ]}

    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResp())
    source = ddf_discovery.RainforestApiSource("key", "https://example.com/request")
    results = source.search("gadgets", 10)
    assert len(results) == 1
    assert results[0]["product_id"] == "B000456"
    assert results[0]["price"] == 19.99
