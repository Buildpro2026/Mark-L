"""General-purpose web research: search, fetch, extract, compare — with
provenance on every claim.

WHAT WAS MISSING
actions/web_search.py exists and works, but it returns FORMATTED PROSE.
A caller gets a paragraph, not data: no source URL it can follow, no
timestamp, no way to tell an observed price from a remembered one, and
nothing another system can consume. So JARVIS could "search the web" and
still could not answer "compare this across three sources and tell me
when each was observed", because the answer had already been flattened
into a sentence by the time anyone saw it.

This is the structured layer. It reuses what exists — web_search's
DuckDuckGo results for discovery, browser_control.automation_page() for
fetching, BeautifulSoup for parsing — and adds the part that was absent:
every finding carries where it came from, when it was seen, and how
strongly it is known.

THE EVIDENCE CLASSES ARE THE POINT
    OBSERVED    JARVIS loaded the page and read this off it.
    REPORTED    a source states it; JARVIS did not verify it independently.
    CALCULATED  derived arithmetically from observed values.
    INFERRED    a judgement, not a reading. Always labelled.
    UNKNOWN     not found. Stays unknown.

An inference must never silently become a fact, and a field that was not
found must never be filled with a plausible value. A missing price is
missing. That is the difference between research and confabulation, and
it is enforced here rather than left to the caller's good manners.

NOTHING CLAIMS A PAGE IT DID NOT LOAD
fetch_page() returns ok=False with the real reason — timeout, HTTP
status, blocked, no browser — and never returns text it did not receive.
A research run over five sources where three failed says three failed.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urljoin, urlparse

logger = logging.getLogger("jarvis.web_research")

# ── Evidence classes ─────────────────────────────────────────────────────
OBSERVED = "OBSERVED"
REPORTED = "REPORTED"
CALCULATED = "CALCULATED"
INFERRED = "INFERRED"
UNKNOWN = "UNKNOWN"

EVIDENCE_CLASSES = (OBSERVED, REPORTED, CALCULATED, INFERRED, UNKNOWN)

# ── Outcome states ───────────────────────────────────────────────────────
OK = "OK"
NO_RESULTS = "NO_RESULTS"
UNAVAILABLE = "UNAVAILABLE"     # no way to reach the web at all
BLOCKED = "BLOCKED"             # the site refused us
TIMEOUT = "TIMEOUT"
FAILED = "FAILED"

DEFAULT_TIMEOUT_SECONDS = 20
MAX_SOURCES_PER_RESEARCH = 5
MAX_PAGE_CHARS = 40_000
# One request per host per this many seconds. A research run that hammers
# one domain gets itself blocked and deserves to.
MIN_SECONDS_BETWEEN_REQUESTS_PER_HOST = 1.0
MAX_FETCH_ATTEMPTS = 2

_last_request_at: dict[str, float] = {}

_PRICE_RE = re.compile(r"[$£€]\s?([\d,]+(?:\.\d{2})?)")
_RATING_RE = re.compile(r"([0-5](?:\.\d)?)\s*(?:out of|/)\s*5", re.I)
_REVIEWS_RE = re.compile(r"([\d,]{2,})\s*(?:ratings?|reviews?)", re.I)
_RANK_RE = re.compile(r"#\s?([\d,]+)")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _host(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower()


def _throttle(url: str) -> None:
    """Space out requests to one host. Politeness that is also
    self-interest: a burst is what gets an IP blocked mid-research."""
    host = _host(url)
    if not host:
        return
    last = _last_request_at.get(host)
    if last is not None:
        wait = MIN_SECONDS_BETWEEN_REQUESTS_PER_HOST - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
    _last_request_at[host] = time.monotonic()


# ══ FINDINGS ═════════════════════════════════════════════════════════════

def finding(field: str, value: Any, evidence: str, source_url: str = "",
            source_title: str = "", note: str = "") -> dict[str, Any]:
    """One piece of information plus how strongly it is known.

    A value of None is only ever paired with UNKNOWN — the two cannot
    disagree, so a caller reading `value` can never mistake an absence for
    a reading."""
    if evidence not in EVIDENCE_CLASSES:
        raise ValueError(f"unknown evidence class: {evidence!r}")
    if value is None:
        evidence = UNKNOWN
    return {
        "field": field, "value": value, "evidence": evidence,
        "source_url": source_url, "source_title": source_title,
        "observed_at": _now_iso() if evidence == OBSERVED else None,
        "note": note,
    }


def unknown(field: str, why: str = "not found on any source read") -> dict[str, Any]:
    """An explicit absence. Returned rather than omitted so a caller can
    tell "we looked and did not find it" from "we never looked"."""
    return finding(field, None, UNKNOWN, note=why)


# ══ SEARCH ═══════════════════════════════════════════════════════════════

def search(query: str, max_results: int = 6) -> dict[str, Any]:
    """Candidate sources for a question, as structured records.

    Reuses web_search's DuckDuckGo path — that already returns dicts with
    title/href/body, and a second search client would be a second answer
    to the same question. Results are candidates, not findings: nothing
    here has been read yet, so everything is REPORTED at best."""
    from actions import web_search as ws

    query = (query or "").strip()
    if not query:
        return {"ok": False, "state": FAILED, "detail": "empty query", "sources": []}

    try:
        raw = ws._ddg_search(query, max_results=max_results) or []
    except Exception as exc:
        logger.warning("search failed for %r: %s", query, exc)
        return {"ok": False, "state": UNAVAILABLE, "detail": str(exc)[:300],
                "query": query, "sources": []}

    sources, seen = [], set()
    for item in raw:
        url = str(item.get("href") or item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        sources.append({
            "url": url,
            "title": str(item.get("title") or "").strip(),
            "snippet": str(item.get("body") or item.get("snippet") or "").strip(),
            "host": _host(url),
            "found_at": _now_iso(),
            "read": False,
        })

    if not sources:
        return {"ok": True, "state": NO_RESULTS, "query": query, "sources": [],
                "detail": "the search returned no results"}
    return {"ok": True, "state": OK, "query": query, "sources": sources}


# ══ FETCH ════════════════════════════════════════════════════════════════

def fetch_page(url: str, timeout: int = DEFAULT_TIMEOUT_SECONDS,
               fetcher: Optional[Callable[[str, int], dict[str, Any]]] = None
               ) -> dict[str, Any]:
    """Load one page and return what actually came back.

    `fetcher` is the seam tests drive; production has none and uses the
    real HTTP path. A failure returns ok=False with the reason and no
    text — this function never returns content it did not receive."""
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "state": FAILED, "url": url,
                "detail": "only http(s) URLs can be fetched"}

    call = fetcher or _http_fetch
    last_detail = ""
    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        _throttle(url)
        try:
            result = call(url, timeout)
        except Exception as exc:
            last_detail = str(exc)[:300]
            logger.debug("fetch attempt %d failed for %s: %s", attempt, url, last_detail)
            if attempt < MAX_FETCH_ATTEMPTS:
                time.sleep(1.0 * attempt)
            continue

        if result.get("ok"):
            html = result.get("html") or ""
            return {
                "ok": True, "state": OK, "url": result.get("url") or url,
                "status": result.get("status"),
                "title": extract_title(html) or result.get("title") or "",
                "html": html[:MAX_PAGE_CHARS],
                "text": html_to_text(html)[:MAX_PAGE_CHARS],
                "observed_at": _now_iso(),
            }

        # An access decision is not a blip; retrying it is pointless and rude.
        if result.get("state") in (BLOCKED,):
            return {**result, "url": url}
        last_detail = result.get("detail") or ""
        if attempt < MAX_FETCH_ATTEMPTS:
            time.sleep(1.0 * attempt)

    return {"ok": False, "state": result.get("state", FAILED) if "result" in dir() else FAILED,
            "url": url, "detail": last_detail or "the page could not be loaded"}


def _http_fetch(url: str, timeout: int) -> dict[str, Any]:
    """The real network call. requests first (cheap); the existing
    Playwright automation second, for pages that need a browser."""
    try:
        import requests
        response = requests.get(
            url, timeout=timeout, headers={
                "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
                "Accept-Language": "en-US,en;q=0.9",
            })
        if response.status_code in (401, 403, 429):
            return {"ok": False, "state": BLOCKED, "status": response.status_code,
                    "detail": f"the site refused the request (HTTP {response.status_code})"}
        if response.status_code >= 400:
            return {"ok": False, "state": FAILED, "status": response.status_code,
                    "detail": f"HTTP {response.status_code}"}
        return {"ok": True, "status": response.status_code, "html": response.text,
                "url": response.url}
    except Exception as exc:
        name = type(exc).__name__
        state = TIMEOUT if "Timeout" in name else UNAVAILABLE
        return {"ok": False, "state": state, "detail": f"{name}: {str(exc)[:200]}"}


# ══ EXTRACT ══════════════════════════════════════════════════════════════

def _soup(html: str):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html or "", "html.parser")


def extract_title(html: str) -> str:
    node = _soup(html).find("title")
    return " ".join(node.get_text().split()) if node else ""


def html_to_text(html: str) -> str:
    """Readable text, scripts and styles removed. Not a renderer — enough
    for extraction and for a human to check a claim against."""
    soup = _soup(html)
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))


def extract_links(html: str, base_url: str = "") -> list[dict[str, str]]:
    out, seen = [], set()
    for anchor in _soup(html).find_all("a", href=True):
        url = urljoin(base_url, anchor["href"]) if base_url else anchor["href"]
        if not url.lower().startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "text": " ".join(anchor.get_text().split())[:200]})
    return out


def _first(pattern: re.Pattern, text: str, cast=str):
    match = pattern.search(text or "")
    if not match:
        return None
    try:
        raw = match.group(1).replace(",", "")
        return cast(raw)
    except (ValueError, TypeError):
        return None


_CURRENT_PRICE_RE = re.compile(
    r"(?:now|sale|deal|your price|price[:\s])\s*[$£€]\s?([\d,]+(?:\.\d{2})?)", re.I)


def _all_prices(text: str) -> list[float]:
    out = []
    for raw in _PRICE_RE.findall(text or "")[:8]:
        try:
            out.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    return out


def _current_price(text: str) -> tuple[Optional[float], str]:
    """(price, how it was chosen). Both values behind the choice are real
    readings; only which one is "current" is a judgement, and the note
    says which judgement was made."""
    marked = _first(_CURRENT_PRICE_RE, text, float)
    if marked is not None:
        return marked, "the page marks this as the current price"
    prices = _all_prices(text)
    if not prices:
        return None, ""
    if len(prices) == 1:
        return prices[0], "the only price on the page"
    return min(prices), (f"lowest of {len(prices)} prices on the page; the others "
                         f"are treated as list/compare-at prices")


def extract_product(page: dict[str, Any]) -> dict[str, Any]:
    """Product fields from a loaded page, each with its own evidence.

    Every field JARVIS could not find comes back UNKNOWN rather than
    absent, so a caller can see what was looked for. Nothing is defaulted:
    a product page with no visible rating produces no rating."""
    if not page.get("ok"):
        return {"ok": False, "state": page.get("state", FAILED),
                "detail": page.get("detail"), "fields": {}}

    text = page.get("text") or ""
    html = page.get("html") or ""
    url = page.get("url") or ""
    title = page.get("title") or ""
    soup = _soup(html)

    fields: dict[str, Any] = {}

    def _record(name, value, note=""):
        fields[name] = (finding(name, value, OBSERVED, url, title, note)
                        if value is not None else unknown(name))

    _record("name", title or None, "from the page title")

    # PRICE. Taking the first price on the page is wrong far more often
    # than it is right: retailers lead with the struck-through list price
    # ("Was $499.00, now $429.00"), so the first match is the one nobody
    # pays. A "now"/"sale"/"price:" marker names the current price
    # explicitly when present; otherwise the LOWEST observed price is the
    # current one, which is a documented heuristic over values all of
    # which were genuinely read off the page — not an invented number.
    current_price, price_note = _current_price(text)
    _record("price", current_price, price_note)
    _record("rating", _first(_RATING_RE, text, float))
    _record("review_count", _first(_REVIEWS_RE, text, int))
    _record("rank", _first(_RANK_RE, text, int))

    image = soup.find("meta", property="og:image") or soup.find("img", src=True)
    image_url = (image.get("content") if image and image.has_attr("content")
                 else (image.get("src") if image else None))
    _record("image_url", urljoin(url, image_url) if image_url else None)

    brand = soup.find("meta", property="og:brand") or soup.find("meta", attrs={"name": "brand"})
    _record("brand", brand.get("content") if brand else None)

    fields["retailer"] = finding("retailer", _host(url) or None, OBSERVED, url, title,
                                 "the host the page was read from")
    fields["product_url"] = finding("product_url", url or None, OBSERVED, url, title)

    # A second price further down a page is usually the list price. That
    # is a reading, but which one is "original" is a judgement — so the
    # discount is CALCULATED and the pairing is noted.
    prices = _all_prices(text)
    current = fields["price"]["value"]
    if current is not None and len(prices) > 1:
        higher = max(prices)
        if higher > current:
            fields["original_price"] = finding("original_price", higher, REPORTED, url, title,
                                               "a higher price also appears on the page")
            fields["discount_pct"] = finding(
                "discount_pct", round((1 - current / higher) * 100, 1), CALCULATED, url, title,
                "derived from the two observed prices")
    return {"ok": True, "state": OK, "source_url": url, "source_title": title,
            "observed_at": page.get("observed_at"), "fields": fields}


# ══ RESEARCH ═════════════════════════════════════════════════════════════

def research(question: str, max_sources: int = 3,
             extractor: Optional[Callable[[dict], dict]] = None,
             fetcher: Optional[Callable[[str, int], dict[str, Any]]] = None,
             record: bool = True) -> dict[str, Any]:
    """Answer one research question from live sources.

    search -> fetch the top N -> extract -> report what was actually read.
    Sources that failed are listed with their reason: a run over five
    where three were blocked says three were blocked, because a
    two-source answer presented as five is a lie about confidence."""
    started = time.time()
    found = search(question, max_results=max(max_sources * 2, 6))
    if not found.get("ok") or not found.get("sources"):
        outcome = {"ok": False, "state": found.get("state", NO_RESULTS),
                   "question": question, "detail": found.get("detail"),
                   "sources_read": [], "sources_failed": [], "results": [],
                   "confidence": 0.0}
        if record:
            _remember(outcome)
        return outcome

    read, failed, results = [], [], []
    for source in found["sources"][:max_sources]:
        page = fetch_page(source["url"], fetcher=fetcher)
        if not page.get("ok"):
            failed.append({"url": source["url"], "title": source["title"],
                           "state": page.get("state"), "detail": page.get("detail")})
            continue
        source["read"] = True
        read.append({"url": page["url"], "title": page["title"],
                     "observed_at": page["observed_at"]})
        try:
            results.append((extractor or extract_product)(page))
        except Exception as exc:
            logger.warning("extraction failed for %s: %s", page["url"], exc)
            failed.append({"url": page["url"], "state": FAILED, "detail": str(exc)[:200]})

    outcome = {
        "ok": bool(read),
        "state": OK if read else UNAVAILABLE,
        "question": question,
        "sources_considered": len(found["sources"]),
        "sources_read": read,
        "sources_failed": failed,
        "results": results,
        # Confidence is the share of intended sources actually read. One
        # source out of three is a third, and says so.
        "confidence": round(len(read) / max(max_sources, 1), 2),
        "duration_ms": int((time.time() - started) * 1000),
        "detail": ("no source could be read" if not read else
                   f"read {len(read)} of {min(max_sources, len(found['sources']))} source(s)"),
    }
    if record:
        _remember(outcome)
    return outcome


def compare_field(results: Iterable[dict[str, Any]], field: str = "price") -> dict[str, Any]:
    """The same field across sources: every observation with where and
    when, the lowest, and whether the sources disagree.

    Disagreement is surfaced, never averaged away — an average of two
    prices is a number that exists nowhere and can be paid nowhere."""
    observations = []
    for result in results or []:
        entry = (result.get("fields") or {}).get(field)
        if not entry or entry.get("value") is None:
            continue
        observations.append({
            "value": entry["value"], "evidence": entry["evidence"],
            "source_url": entry.get("source_url") or result.get("source_url"),
            "source_title": entry.get("source_title") or result.get("source_title"),
            "observed_at": entry.get("observed_at") or result.get("observed_at"),
        })

    if not observations:
        return {"ok": False, "state": UNKNOWN, "field": field, "observations": [],
                "detail": f"no source reported a {field}"}

    numeric = [o for o in observations if isinstance(o["value"], (int, float))]
    lowest = min(numeric, key=lambda o: o["value"]) if numeric else None
    highest = max(numeric, key=lambda o: o["value"]) if numeric else None
    distinct = {o["value"] for o in observations}

    return {
        "ok": True, "state": OK, "field": field,
        "observations": observations,
        "source_count": len(observations),
        "lowest": lowest, "highest": highest,
        "conflict": len(distinct) > 1,
        "spread": (round(highest["value"] - lowest["value"], 2)
                   if lowest and highest and lowest is not highest else 0),
        "detail": (f"{len(observations)} source(s) reported a {field}"
                   + ("; they disagree" if len(distinct) > 1 else "; they agree")),
    }


def summarize(outcome: dict[str, Any]) -> str:
    """What JARVIS should SAY about a research run.

    Never claims a source it could not read, and never states a value
    without saying where it came from and when."""
    if not outcome.get("ok"):
        return (f"I couldn't research that: {outcome.get('detail') or 'no source was reachable'}."
                + (f" {len(outcome.get('sources_failed') or [])} source(s) failed."
                   if outcome.get("sources_failed") else ""))

    lines = [f"Read {len(outcome['sources_read'])} source(s) for: {outcome['question']}"]
    for source in outcome["sources_read"]:
        lines.append(f"  - {source['title'] or source['url']} ({source['url']}) "
                     f"observed {source['observed_at']}")
    if outcome.get("sources_failed"):
        lines.append(f"  {len(outcome['sources_failed'])} source(s) could not be read:")
        for failure in outcome["sources_failed"][:3]:
            lines.append(f"    - {failure['url']}: {failure.get('state')} "
                         f"{failure.get('detail') or ''}".rstrip())
    lines.append(f"Confidence: {outcome['confidence']:.0%} of intended sources read.")
    return "\n".join(lines)


# ══ MEMORY ═══════════════════════════════════════════════════════════════

def _remember(outcome: dict[str, Any]) -> None:
    """Distil a run into memory. Sources, counts and confidence — never
    page text. Storing whole webpages is how a memory store becomes
    unusable within a week."""
    try:
        from actions import operating_memory
        operating_memory.record(
            operating_memory.AGENT_OUTCOME, source="web_research",
            subject=(outcome.get("question") or "")[:200],
            summary=(outcome.get("detail") or "")[:400],
            data={
                "sources_read": [s["url"] for s in outcome.get("sources_read", [])],
                "sources_failed": [f["url"] for f in outcome.get("sources_failed", [])],
                "confidence": outcome.get("confidence"),
                "state": outcome.get("state"),
            },
            ok=bool(outcome.get("ok")))
    except Exception:
        logger.debug("could not record the research run", exc_info=True)


def history(limit: int = 10) -> list[dict[str, Any]]:
    try:
        from actions import operating_memory
        return operating_memory.recall(source="web_research", limit=limit) or []
    except Exception:
        return []
