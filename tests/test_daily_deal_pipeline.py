import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pipeline_mod():
    return load_module("jarvis_daily_deal_pipeline", "actions/daily_deal_finders.py")


def test_product_scoring_prioritizes_trending_and_margin(pipeline_mod):
    product = {
        "name": "Portable Charger",
        "price": 19.99,
        "source": "Amazon",
        "sales_signal": 92,
        "demand": 88,
        "margin": 0.22,
        "trend_strength": 0.84,
        "competition": 0.31,
        "content_potential": 0.9,
        "repeatability": 0.8,
        "historical_performance": 0.74,
    }
    score = pipeline_mod.score_product(product)
    assert score > 80


def test_deduplication_avoids_duplicate_products(pipeline_mod):
    first = {"product_id": "abc123", "name": "Desk Lamp"}
    second = {"product_id": "abc123", "name": "Desk Lamp"}
    assert pipeline_mod.is_duplicate(first, second) is True
    assert pipeline_mod.is_duplicate(first, {"product_id": "xyz999", "name": "Another"}) is False


def test_prepare_post_contains_required_fields(pipeline_mod):
    asset = {
        "product": {"name": "Mini Blender", "price": 29.99, "url": "https://example.com/product"},
        "copy": "Tiny power. Big results.",
        "platform": "instagram",
    }
    post = pipeline_mod.prepare_post(asset)
    assert post["platform"] == "instagram"
    assert post["caption"].startswith("Tiny")
    assert post["affiliate_url"] == "https://example.com/product"
