"""Amazon Best Sellers discovery — an additional FIND strategy for Daily
Deal Finders.

The objective this serves is "find the top 10 selling products on Amazon",
and the distinction that shapes the whole module is: that is NOT a keyword
search. actions/ddf_discovery.py asks a product-data API "show me things
matching 'trending gadgets'" and gets back relevance-ranked results, which
answers a different question. Amazon's Best Sellers pages are the ranking
itself — position on that page IS sales rank, published by the retailer.
So this reads the ranking rather than guessing at it.

It is a strategy, not a replacement. ddf_discovery/Rainforest stays exactly
where it was and remains the fallback: this path needs a working browser,
and a server without one must degrade to the API rather than fail the
objective.

Browser: actions/browser_control.automation_page() — the repo's existing
Playwright automation, used through the structured surface added there for
this. No second browser framework exists here; there is no Playwright
import in this file at all.

Two boundaries that are not negotiable:

  * Nothing here attempts to defeat a CAPTCHA, a bot check, a login wall or
    any other access control. detect_access_block() looks for exactly those
    and stops, and the run reports ACCESS_BLOCKED with the marker it saw.
    A blocked scrape reported as a successful one would put invented
    products into the catalogue.
  * No field is ever invented. A missing price, rating or image is omitted
    from the record; only a name and an ASIN are required, because a
    product without those is not a product. Where a value is derived rather
    than read (see sales_signal below), it says so in notes.

ON RANKING ACROSS CATEGORIES
Amazon publishes a Best Sellers Rank *within* a category. It does not
publish a global cross-category one, and no honest reading of these pages
can produce it — #1 in Tools and #1 in Kitchen are both #1. combined_rank()
is therefore a documented heuristic over the real signals available:
position within its category, how many separate Best Sellers lists the ASIN
appears on at all (breadth is genuine cross-category strength), and the
review volume and score Amazon shows. It is not Amazon's own number and is
never presented as one.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger("jarvis.amazon_bestsellers")

BESTSELLERS_URL = "https://www.amazon.com/Best-Sellers/zgbs"
_AMAZON_ROOT = "https://www.amazon.com"

# Run outcomes, mirroring ddf_discovery's vocabulary so a caller can treat
# both discovery strategies the same way.
STATE_RAN = "RAN"
STATE_ACCESS_BLOCKED = "ACCESS_BLOCKED"
STATE_UNAVAILABLE = "UNAVAILABLE"
STATE_FAILED = "FAILED"

# An ASIN is exactly ten uppercase alphanumerics. Anchored on both sides so
# a longer token in a URL path can never be sliced down into a false match.
_ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:[/?#]|$)")
_ASIN_BARE_RE = re.compile(r"^[A-Z0-9]{10}$")
_PRICE_RE = re.compile(r"\$\s?([\d,]+(?:\.\d{1,2})?)")
_BADGE_RE = re.compile(r"#\s?([\d,]+)")
_RATING_RE = re.compile(r"([\d.]+)\s+out of\s+5", re.I)
_COUNT_RE = re.compile(r"^\(?([\d,]{1,12})\)?$")

# Text Amazon serves instead of content when it has decided a client is a
# robot, or when a page is gated. Matched case-insensitively against the
# page body. This list exists to STOP, never to work around any of them.
_ACCESS_BLOCK_MARKERS: tuple[tuple[str, str], ...] = (
    ("enter the characters you see below", "CAPTCHA"),
    ("type the characters you see in this image", "CAPTCHA"),
    ("sorry, we just need to make sure you're not a robot", "BOT_CHECK"),
    ("api-services-support@amazon.com", "BOT_CHECK"),
    ("robot check", "BOT_CHECK"),
    ("to discuss automated access to amazon data", "BOT_CHECK"),
    ("sign in for the best experience", "AUTH_WALL"),
    ("enter your email or mobile phone number", "AUTH_WALL"),
    ("request was throttled", "THROTTLED"),
)
_BLOCK_URL_MARKERS: tuple[tuple[str, str], ...] = (
    ("/errors/validatecaptcha", "CAPTCHA"),
    ("/ap/signin", "AUTH_WALL"),
)


# ══ PURE PARSING ═════════════════════════════════════════════════════════
# Everything below this line is a pure function over HTML. That is
# deliberate: it is the part that must be regression-tested, and a pure
# function can be tested against a real saved page without a browser.

def _soup(html: str):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html or "", "html.parser")


def extract_asin(url: str) -> Optional[str]:
    """The ASIN out of any Amazon product URL shape, or None. None is a
    real answer here — a card without one is skipped, never saved under a
    generated id."""
    if not url:
        return None
    match = _ASIN_RE.search(str(url))
    if match:
        return match.group(1)
    return None


def detect_access_block(html: str, url: str = "") -> Optional[str]:
    """The kind of access block this page represents, or None if it is a
    normal page. Detection only — nothing anywhere in this module tries to
    get past what this finds."""
    lowered = (url or "").lower()
    for marker, kind in _BLOCK_URL_MARKERS:
        if marker in lowered:
            return kind
    body = (html or "").lower()
    for marker, kind in _ACCESS_BLOCK_MARKERS:
        if marker in body:
            return kind
    return None


def _absolute(href: str) -> str:
    href = (href or "").strip()
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return _AMAZON_ROOT + href
    return href


def parse_categories(html: str) -> list[dict[str, str]]:
    """Best Sellers categories from a Best Sellers page, sidebar included.

    Amazon's class names are hashed and rotate, so this anchors on the one
    thing that does not change: a category link's href goes through /zgbs/.
    Deduplicated by URL, order preserved — the sidebar order is Amazon's own
    department order, and reordering it would be inventing a priority."""
    seen: set[str] = set()
    categories: list[dict[str, str]] = []
    for anchor in _soup(html).find_all("a", href=True):
        href = anchor["href"]
        if "/zgbs/" not in href:
            continue
        url = _absolute(href).split("?")[0]
        name = " ".join(anchor.get_text(" ", strip=True).split())
        if not name or url in seen:
            continue
        # The root listing links to itself from the breadcrumb; it is not a
        # sub-category and traversing it again would just repeat page one.
        if url.rstrip("/").endswith("/zgbs"):
            continue
        seen.add(url)
        categories.append({"name": name, "url": url})
    return categories


def _card_containers(soup) -> list:
    """The product cards on a Best Sellers grid.

    Three strategies, most specific first, because Amazon ships more than
    one grid template and rotates the hashed class names on all of them.
    The last one needs no Amazon-specific markup at all: any anchor that
    points at /dp/<ASIN>, lifted to a sensible ancestor."""
    for selector in ("div#gridItemRoot", "div.zg-grid-general-faceout",
                     "div[data-asin]", "li.zg-item-immersion"):
        found = soup.select(selector)
        if found:
            return found

    cards, seen = [], set()
    for anchor in soup.find_all("a", href=True):
        if not extract_asin(anchor["href"]):
            continue
        node = anchor
        for _ in range(3):
            parent = node.parent
            if parent is None or parent.name in ("body", "html", "[document]"):
                break
            node = parent
        if id(node) in seen:
            continue
        seen.add(id(node))
        cards.append(node)
    return cards


def _text_of(card) -> str:
    return " ".join(card.get_text(" ", strip=True).split())


def _card_name(card, asin: str) -> Optional[str]:
    """A product's title. Amazon puts the full title in the grid image's
    alt text far more reliably than in any text node, so that is tried
    first; the visible truncated title is the fallback."""
    image = card.find("img", alt=True)
    if image:
        alt = " ".join(image["alt"].split())
        if len(alt) > 3 and not _ASIN_BARE_RE.match(alt):
            return alt
    for node in card.select("div[class*='truncate'], span[class*='truncate'], div._cDEzb_p13n-sc-css-line-clamp-3_g3dy1"):
        text = " ".join(node.get_text(" ", strip=True).split())
        if len(text) > 3:
            return text
    for anchor in card.find_all("a", href=True):
        if extract_asin(anchor["href"]):
            text = " ".join(anchor.get_text(" ", strip=True).split())
            if len(text) > 3 and not _BADGE_RE.fullmatch(text):
                return text
    return None


def _card_price(card) -> Optional[float]:
    match = _PRICE_RE.search(_text_of(card))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _card_position(card) -> Optional[int]:
    """The '#3' rank badge, when the template shows one."""
    for node in card.select("span[class*='zg-bdg'], span.zg-badge-text, span[class*='badge']"):
        match = _BADGE_RE.search(node.get_text(" ", strip=True))
        if match:
            return int(match.group(1).replace(",", ""))
    match = _BADGE_RE.search(_text_of(card)[:24])
    return int(match.group(1).replace(",", "")) if match else None


def _card_rating(card) -> tuple[Optional[float], Optional[int]]:
    text = _text_of(card)
    rating = None
    match = _RATING_RE.search(text)
    if match:
        try:
            rating = float(match.group(1))
        except ValueError:
            rating = None
    if rating is None:
        for node in card.select("[title]"):
            match = _RATING_RE.search(node.get("title", ""))
            if match:
                rating = float(match.group(1))
                break

    count = None
    for node in card.find_all(["a", "span"]):
        candidate = node.get_text(" ", strip=True)
        match = _COUNT_RE.match(candidate)
        if match:
            try:
                value = int(match.group(1).replace(",", ""))
            except ValueError:
                continue
            # A bare "5" next to stars is the rating, not a review count.
            if value >= 10:
                count = value
                break
    return rating, count


def _card_image(card) -> Optional[str]:
    image = card.find("img", src=True)
    return image["src"] if image else None


def parse_products(html: str, category: Optional[str] = None,
                   category_url: Optional[str] = None,
                   position_offset: int = 0) -> list[dict[str, Any]]:
    """Best-selling products from one Best Sellers category page.

    Position: the rank badge when Amazon renders one, otherwise the card's
    place in the grid — which is the same thing, because the grid IS the
    ranking. Which of the two was used is recorded in position_source, so
    nothing downstream has to guess how solid the number is.

    Optional fields (price, image, rating, review count) are left out when
    the page does not show them. They are never defaulted."""
    soup = _soup(html)
    products: list[dict[str, Any]] = []
    ordinal = position_offset

    for card in _card_containers(soup):
        asin = None
        for anchor in card.find_all("a", href=True):
            asin = extract_asin(anchor["href"])
            if asin:
                break
        if not asin and card.get("data-asin"):
            candidate = str(card.get("data-asin")).strip()
            asin = candidate if _ASIN_BARE_RE.match(candidate) else None
        if not asin:
            continue
        name = _card_name(card, asin)
        if not name:
            continue

        ordinal += 1
        badge = _card_position(card)
        rating, rating_count = _card_rating(card)
        product: dict[str, Any] = {
            "asin": asin,
            "name": name,
            "url": f"{_AMAZON_ROOT}/dp/{asin}",
            "category": category,
            "category_url": category_url,
            "position": badge if badge is not None else ordinal,
            "position_source": "badge" if badge is not None else "grid_order",
        }
        for key, value in (("price", _card_price(card)), ("image_url", _card_image(card)),
                           ("rating", rating), ("rating_count", rating_count)):
            if value is not None:
                product[key] = value
        products.append(product)

    return products


# ══ COMBINE, DEDUPE, RANK ════════════════════════════════════════════════

def deduplicate(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """One record per ASIN, merged rather than dropped.

    A product that appears on three Best Sellers lists is not a duplicate to
    be thrown away — the fact that it charts in three categories is the
    single most useful cross-category signal these pages carry, so the
    merged record keeps its best position and every category it charted in.
    Optional fields fill forward: whichever page happened to show a price
    wins over the ones that did not."""
    merged: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        asin = candidate.get("asin")
        if not asin:
            continue
        existing = merged.get(asin)
        if existing is None:
            record = dict(candidate)
            record["categories"] = [c for c in [candidate.get("category")] if c]
            merged[asin] = record
            continue

        if candidate.get("category") and candidate["category"] not in existing["categories"]:
            existing["categories"].append(candidate["category"])
        new_position = candidate.get("position")
        if new_position is not None and (existing.get("position") is None
                                         or new_position < existing["position"]):
            existing["position"] = new_position
            existing["position_source"] = candidate.get("position_source")
            existing["category"] = candidate.get("category")
            existing["category_url"] = candidate.get("category_url")
        for field in ("price", "image_url", "rating", "rating_count"):
            if existing.get(field) is None and candidate.get(field) is not None:
                existing[field] = candidate[field]
    return list(merged.values())


def combined_rank(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """One ranked list across every category, strongest seller first.

    See the module docstring: Amazon has no published global rank, so this
    is a transparent weighted formula over the signals that ARE real, in
    the same style as daily_deal_finders.rank_products():

      position  0.60  1/position — #1 scores 1.0, #10 scores 0.1. Rank is
                      the strongest evidence on the page and dominates.
      breadth   0.20  charting in several categories at once, capped at 3.
      reviews   0.15  log-compressed volume, so one enormous product does
                      not flatten every other candidate to zero.
      rating    0.05  quality, weakest because bestsellers cluster at 4.5.

    A missing signal scores 0 for that term — honestly unmeasured, not
    assumed average. Ties break on raw position and then ASIN, so the same
    input always produces the same order."""
    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        position = candidate.get("position")
        position_score = 1.0 / float(position) if position and position > 0 else 0.0
        breadth = min(len(candidate.get("categories") or []) / 3.0, 1.0)
        count = float(candidate.get("rating_count") or 0)
        reviews = min(math.log1p(count) / math.log1p(50_000), 1.0) if count > 0 else 0.0
        rating = candidate.get("rating")
        quality = min(max(float(rating) / 5.0, 0.0), 1.0) if rating else 0.0

        record = dict(candidate)
        record["sales_rank_score"] = round(
            0.60 * position_score + 0.20 * breadth + 0.15 * reviews + 0.05 * quality, 6)
        ranked.append(record)

    ranked.sort(key=lambda p: (-p["sales_rank_score"],
                               p.get("position") or 10**6,
                               p.get("asin") or ""))
    return ranked


def select_top(candidates: Iterable[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    """The overall top N of the combined list — not N per category, and not
    the first N encountered. Returns fewer than N without complaint when
    fewer were found; a short list is a real result, not a failure."""
    return combined_rank(deduplicate(candidates))[:max(int(limit), 0)]


# ══ MAPPING INTO THE EXISTING DDF CATALOG ════════════════════════════════

def to_ddf_product(candidate: dict[str, Any]) -> dict[str, Any]:
    """A candidate in the shape daily_deal_finders.save_product() accepts.

    sales_signal is the one derived value here, and it is derived from real
    published rank rather than guessed: a #1 Best Seller genuinely carries a
    stronger sales signal than a #80 one. notes.discovery_confidence records
    that it came from rank so nothing downstream mistakes it for a measured
    conversion. Every other scoring dimension this page cannot see stays 0 —
    'not measured yet', which is honest, rather than a plausible number."""
    position = candidate.get("position")
    record: dict[str, Any] = {
        "name": candidate["name"],
        "source": "amazon_bestsellers",
        "category": candidate.get("category"),
        "url": candidate["url"],
        "affiliate_url": candidate["url"],
        "product_id": candidate["asin"],
        "retailer": "amazon",
        "merchant": "amazon",
        "affiliate_network": "amazon_associates",
        "discovery_date": datetime.now(timezone.utc).isoformat(),
        "sales_signal": round(1.0 / float(position), 4) if position else 0,
        "demand": 0, "trend_strength": 0, "competition": 0,
        "content_potential": 0, "repeatability": 0, "historical_performance": 0,
        "notes": {
            "discovery_confidence": "amazon_bestsellers_rank",
            "bestseller_position": position,
            "position_source": candidate.get("position_source"),
            "bestseller_categories": candidate.get("categories") or [],
            "sales_rank_score": candidate.get("sales_rank_score"),
        },
    }
    if candidate.get("price") is not None:
        record["price"] = float(candidate["price"])
        record["current_price"] = float(candidate["price"])
    if candidate.get("image_url"):
        record["image_url"] = candidate["image_url"]
    if candidate.get("rating") is not None:
        record["product_rating"] = candidate["rating"]
    return record


# ══ BROWSER LAYER ════════════════════════════════════════════════════════

class _Log:
    """The execution record. Every event the run is required to account for
    lands here whether it went well or not — a log containing only the
    successful categories is how a half-finished traversal gets read as a
    complete one."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def add(self, event: str, **fields: Any) -> None:
        self.events.append({"event": event, **fields})
        logger.info("amazon_bestsellers %s %s", event,
                    " ".join(f"{k}={v}" for k, v in fields.items()))


async def _goto(page, url: str, log: _Log, attempts: int = 2) -> Optional[str]:
    """Navigate and return the page HTML, or None if it never loaded.

    Amazon redirects (country/locale interstitials) are followed by the
    browser itself; what this adds is one retry for a genuinely transient
    failure. It does not retry an access block — that is a decision, not a
    blip, and hammering it would be exactly the behaviour the block exists
    to stop."""
    for attempt in range(1, attempts + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await _autoscroll(page)
            return await page.content()
        except Exception as exc:
            log.add("navigation_failed", url=url, attempt=attempt, error=str(exc)[:200])
            if attempt < attempts:
                await asyncio.sleep(1.5 * attempt)
    return None


async def _autoscroll(page, steps: int = 6) -> None:
    """Amazon's Best Sellers grid renders its lower rows only once they are
    scrolled toward, so a page read straight after load is genuinely half
    empty. This walks down it. Any failure here is non-fatal: fewer
    products, not a failed run."""
    for _ in range(steps):
        try:
            await page.evaluate("window.scrollBy(0, window.innerHeight);")
            await page.wait_for_timeout(350)
        except Exception:
            return


def _next_page_url(html: str) -> Optional[str]:
    for anchor in _soup(html).select("li.a-last a[href], a[aria-label='Next page'], a[href*='pg=2']"):
        href = anchor.get("href")
        if href:
            return _absolute(href)
    return None


async def _scrape(page, limit: int, max_categories: int,
                  max_pages_per_category: int, log: _Log) -> dict[str, Any]:
    log.add("amazon_discovery_started", url=BESTSELLERS_URL, limit=limit)

    root_html = await _goto(page, BESTSELLERS_URL, log)
    if root_html is None:
        return {"state": STATE_FAILED,
                "detail": "Amazon Best Sellers did not load after retries."}

    block = detect_access_block(root_html, getattr(page, "url", "") or "")
    if block:
        return {"state": STATE_ACCESS_BLOCKED, "block_kind": block,
                "detail": (f"Amazon returned a {block} on the Best Sellers page. "
                           "Discovery stopped — no attempt was made to bypass it.")}

    log.add("bestsellers_page_reached", url=getattr(page, "url", BESTSELLERS_URL))
    categories = parse_categories(root_html)
    log.add("categories_discovered", count=len(categories),
            names=[c["name"] for c in categories[:max_categories]])

    if not categories:
        return {"state": STATE_FAILED,
                "detail": ("Reached Amazon Best Sellers but found no category links — "
                           "the page layout did not match anything this parser knows.")}

    all_candidates: list[dict[str, Any]] = []
    processed, failed = 0, 0
    for category in categories[:max_categories]:
        page_url: Optional[str] = category["url"]
        pages_done, extracted = 0, 0
        while page_url and pages_done < max_pages_per_category:
            html = await _goto(page, page_url, log)
            if html is None:
                failed += 1
                log.add("category_failed", category=category["name"], reason="navigation")
                break
            block = detect_access_block(html, getattr(page, "url", "") or "")
            if block:
                failed += 1
                log.add("category_failed", category=category["name"], reason=block)
                # A block on one category means the session is being
                # challenged; continuing to hammer other categories is
                # exactly what we must not do.
                return {"state": STATE_ACCESS_BLOCKED, "block_kind": block,
                        "candidates": all_candidates,
                        "detail": (f"Amazon returned a {block} while reading "
                                   f"'{category['name']}'. Discovery stopped — no bypass attempted.")}
            found = parse_products(html, category=category["name"],
                                   category_url=category["url"],
                                   position_offset=pages_done * 50)
            all_candidates.extend(found)
            extracted += len(found)
            pages_done += 1
            page_url = _next_page_url(html) if pages_done < max_pages_per_category else None
        processed += 1
        log.add("category_processed", category=category["name"],
                pages=pages_done, products=extracted)

    log.add("categories_processed", processed=processed, failed=failed)
    return {"state": STATE_RAN, "candidates": all_candidates,
            "categories_discovered": len(categories),
            "categories_processed": processed, "categories_failed": failed}


# ══ ENTRY POINT ══════════════════════════════════════════════════════════

def is_available() -> tuple[bool, Optional[str]]:
    from actions import browser_control
    return browser_control.automation_available()


def _run_sync(coro):
    """Run an async scrape from sync code. asyncio.run() is correct on a
    worker thread (which is where the tool executor puts this), but raises
    if a loop is already running here — so that case gets its own thread
    with its own loop rather than failing the run."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: dict[str, Any] = {}

    def _target():
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # re-raised on the calling thread below
            box["error"] = exc

    thread = threading.Thread(target=_target, name="amazon-bestsellers", daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def discover_top_sellers(limit: int = 10, max_categories: int = 6,
                         max_pages_per_category: int = 1,
                         page_factory: Optional[Callable[[], Any]] = None,
                         save: bool = True) -> dict[str, Any]:
    """Open Amazon, traverse Best Sellers categories, and return the overall
    top `limit` sellers across all of them combined.

    `page_factory` is the seam the tests drive: a zero-argument callable
    returning an async context manager that yields a page. Production passes
    nothing and gets browser_control.automation_page(). Production code has
    no fake-page path in it — a test double is supplied from outside or the
    real browser runs.

    max_pages_per_category defaults to 1 because Amazon puts ranks 1-50 on
    page one, and a top-10-across-categories objective is decided long
    before rank 50 — page two is wired up and offsets its positions
    correctly (see _scrape), it is simply not worth an extra request by
    default. Raise it when an objective genuinely needs the deeper ranks.

    Saving goes through daily_deal_finders.save_product() at its default
    DISCOVERED status. Nothing here publishes anything."""
    from actions import daily_deal_finders as ddf

    log = _Log()
    if page_factory is None:
        ok, reason = is_available()
        if not ok:
            log.add("amazon_discovery_unavailable", reason=reason)
            return {"ok": False, "state": STATE_UNAVAILABLE, "detail": reason,
                    "products": [], "saved": 0, "log": log.events}
        from actions import browser_control
        page_factory = browser_control.automation_page

    async def _drive() -> dict[str, Any]:
        async with page_factory() as page:
            return await _scrape(page, limit, max_categories, max_pages_per_category, log)

    try:
        result = _run_sync(_drive())
    except Exception as exc:
        logger.exception("Amazon Best Sellers discovery raised")
        log.add("amazon_discovery_failed", error=str(exc)[:300])
        return {"ok": False, "state": STATE_FAILED, "detail": str(exc)[:300],
                "products": [], "saved": 0, "log": log.events}

    raw = result.get("candidates") or []
    unique = deduplicate(raw)
    log.add("duplicates_removed", extracted=len(raw), unique=len(unique))
    top = combined_rank(unique)[:max(int(limit), 0)]
    log.add("final_candidates", count=len(unique))
    log.add("final_top", requested=limit, selected=len(top),
            asins=[p["asin"] for p in top])

    if result.get("state") != STATE_RAN:
        log.add("amazon_discovery_incomplete", state=result.get("state"),
                detail=result.get("detail"))
        return {"ok": False, "state": result.get("state"),
                "block_kind": result.get("block_kind"),
                "detail": result.get("detail"), "products": top,
                "saved": 0, "log": log.events}

    saved: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if save:
        for candidate in top:
            try:
                record = ddf.save_product(to_ddf_product(candidate))
                saved.append({"product_id": record.get("product_id") or candidate["asin"],
                              "id": record.get("id"), "name": candidate["name"]})
            except Exception as exc:
                errors.append({"asin": candidate["asin"], "detail": str(exc)[:200]})
        log.add("products_saved", saved=len(saved), failed=len(errors))

    return {
        "ok": True, "state": STATE_RAN, "provider": "amazon_bestsellers",
        "products": top, "saved": len(saved), "discovered": saved, "errors": errors,
        "categories_discovered": result.get("categories_discovered"),
        "categories_processed": result.get("categories_processed"),
        "categories_failed": result.get("categories_failed"),
        "log": log.events,
    }
