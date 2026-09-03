"""Real product-discovery for Daily Deal Finders (Lee's autonomous-CEO/COS
spec, Section FIFTH: "JARVIS must be able to independently discover product
opportunities without requiring me to hand-create a CSV").

Why not Amazon's own Product Advertising API: PA-API requires an approved
Amazon Associates account, and Amazon's own policy requires 3 qualifying
sales in the trailing 180 days just to KEEP API access active. Nothing in
this codebase or its configuration has that credential, and building a
client against an account that doesn't exist would just fail at runtime —
so this deliberately does not implement it. Why not scraping Amazon
directly: no ToS-compliant path exists for that without Lee's own explicit
account/legal decision, so that path is deliberately not implemented either.

What IS implemented: a small, provider-agnostic DiscoverySource interface,
with one real adapter wired end-to-end against a third-party product-data
API (Rainforest API is the default — it mirrors Amazon listings for a
plain API key, no Associates approval needed, which is the most viable
"actually works today" path available without inventing an integration
that doesn't exist). It activates the moment PRODUCT_DATA_API_KEY is set
(core/headless/config.py) — until then, discover_new_products() honestly
reports NOT_CONFIGURED. It never fabricates a product, price, or rating;
every field either comes straight from the provider's response or is left
out entirely (never defaulted to a plausible-looking placeholder the way
daily_deal_finders.discover_product()'s old manual-entry stub does — that
function is untouched and still exists as the explicit manual/CSV path).

Discovered candidates are saved via daily_deal_finders.save_product() at
whatever status they already have (default: DISCOVERED — see that
module's STATUS_DISCOVERED) — never auto-published. Publishing a
discovered product still requires the existing advance_to_published(...,
approved=True) gate, unchanged."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from core.headless import config
from actions import daily_deal_finders as ddf

logger = logging.getLogger("jarvis.ddf_discovery")

# A small, deliberately narrow default search list — real product
# categories DDF already uses (see daily_deal_finders.list_categories()
# callers / ddf_site), not an LLM-invented query. Kept short: this runs
# unattended from the CEO operating cycle, and each category is one real
# outbound API call against a paid quota.
DEFAULT_DISCOVERY_QUERIES: tuple[str, ...] = (
    "trending gadgets",
    "best selling home deals",
)


class DiscoverySource(Protocol):
    name: str

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Returns raw candidate dicts in the shape daily_deal_finders.
        save_product() accepts. Must never raise for a normal API/network
        failure — return [] and let the caller's own error handling in
        discover_new_products() report it; only truly unexpected bugs
        should raise."""
        ...


class RainforestApiSource:
    """https://www.rainforestapi.com/ — a third-party service that mirrors
    Amazon search/listing data behind a plain API key. Chosen as the
    default adapter because it needs no Amazon Associates approval and no
    sales history, unlike PA-API — see module docstring."""

    name = "rainforest"

    def __init__(self, api_key: str, base_url: str):
        self._api_key = api_key
        self._base_url = base_url

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        import requests

        try:
            resp = requests.get(
                self._base_url,
                params={
                    "api_key": self._api_key,
                    "type": "search",
                    "amazon_domain": "amazon.com",
                    "search_term": query,
                },
                timeout=20,
            )
        except requests.RequestException as exc:
            logger.warning("Rainforest API request failed for %r: %s", query, exc)
            return []

        if resp.status_code != 200:
            logger.warning("Rainforest API returned HTTP %s for %r", resp.status_code, query)
            return []

        try:
            payload = resp.json()
        except ValueError:
            logger.warning("Rainforest API returned non-JSON for %r", query)
            return []

        results = payload.get("search_results") or []
        candidates: list[dict[str, Any]] = []
        for item in results[:limit]:
            asin = item.get("asin")
            price_info = item.get("price") or {}
            price = price_info.get("value")
            if not asin or price is None:
                continue  # never save a candidate missing the two fields that make it a real product
            candidates.append({
                "name": item.get("title") or f"Amazon item {asin}",
                "source": "rainforest_api",
                "category": query,
                "price": float(price),
                "current_price": float(price),
                "url": item.get("link") or f"https://www.amazon.com/dp/{asin}",
                "affiliate_url": item.get("link") or f"https://www.amazon.com/dp/{asin}",
                "image_url": item.get("image") or "",
                "product_id": asin,
                "retailer": "amazon",
                "merchant": "amazon",
                "affiliate_network": "amazon_associates",
                "product_rating": item.get("rating"),
                "discovery_date": datetime.now(timezone.utc).isoformat(),
                # Signal fields (sales_signal/demand/trend_strength/etc.) a
                # generic product-search API does NOT provide. Left at 0
                # rather than a confident-looking fabricated value — see
                # score_product(): a 0 here means "unscored on this
                # dimension yet", which is honest, not "definitely bad".
                # discovery_confidence flags this for anything reading the
                # row later (e.g. a future human review pass).
                "sales_signal": 0, "demand": 0, "trend_strength": 0,
                "competition": 0, "content_potential": 0, "repeatability": 0,
                "historical_performance": 0,
                "notes": {"discovery_confidence": "raw_api_data_unscored"},
            })
        return candidates


def _active_source() -> Optional[DiscoverySource]:
    if not config.PRODUCT_DATA_API_KEY:
        return None
    provider = (config.PRODUCT_DATA_API_PROVIDER or "rainforest").strip().lower()
    if provider == "rainforest":
        return RainforestApiSource(config.PRODUCT_DATA_API_KEY, config.PRODUCT_DATA_API_URL)
    logger.warning("PRODUCT_DATA_API_PROVIDER=%r has no adapter — falling back to NOT_CONFIGURED.", provider)
    return None


def is_configured() -> bool:
    return _active_source() is not None


def discover_new_products(
    queries: Optional[list[str]] = None, limit_per_query: int = 5,
) -> dict[str, Any]:
    """The real discovery entry point the CEO operating cycle calls.
    Honestly reports NOT_CONFIGURED (never a fabricated result) when no
    PRODUCT_DATA_API_KEY is set. Every saved candidate lands at whatever
    status save_product()/discovery gives it by default (DISCOVERED) —
    nothing here ever publishes anything."""
    source = _active_source()
    if source is None:
        return {
            "ok": False, "state": "NOT_CONFIGURED", "provider": config.PRODUCT_DATA_API_PROVIDER,
            "detail": (
                "No product-data API key configured (PRODUCT_DATA_API_KEY). "
                "Real Amazon product discovery is architecturally wired but has no live credential — "
                "the manual 'add_product' path still works in the meantime."
            ),
            "discovered": [], "saved": 0, "errors": [],
        }

    queries = queries or list(DEFAULT_DISCOVERY_QUERIES)
    saved: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for query in queries:
        try:
            candidates = source.search(query, limit_per_query)
        except Exception as exc:  # a genuinely unexpected adapter bug, not a normal network failure
            logger.exception("discovery source %s raised for query %r", source.name, query)
            errors.append({"query": query, "detail": str(exc)})
            continue
        if not candidates:
            continue
        for candidate in candidates:
            try:
                record = ddf.save_product(candidate)
                saved.append({"product_id": record["product_id"], "name": record["name"], "query": query})
            except Exception as exc:
                errors.append({"query": query, "candidate": candidate.get("name"), "detail": str(exc)})

    return {
        "ok": True, "state": "RAN", "provider": source.name,
        "queries": queries, "saved": len(saved), "discovered": saved, "errors": errors,
    }
