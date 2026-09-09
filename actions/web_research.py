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
    # The question chooses the extractor — a job search and a price check
    # need different fields off the page. detect_domain() picks the SHAPE
    # of the answer, never the answer.
    domain = detect_domain(question)
    extractor = extractor or EXTRACTORS.get(domain) or extract_page

    # What JARVIS already knows about this subject. Carried alongside the
    # live findings, never instead of them: prior knowledge is context for
    # reading the result, not a substitute for looking.
    prior = []
    try:
        from actions import brain_memory
        prior = brain_memory.recall_for(question, limit=3)
    except Exception:
        logger.debug("prior knowledge lookup failed", exc_info=True)
    found = search(question, max_results=max(max_sources * 2, 6))
    if not found.get("ok") or not found.get("sources"):
        outcome = {"ok": False, "state": found.get("state", NO_RESULTS),
                   "question": question, "detail": found.get("detail"),
                   "sources_read": [], "sources_failed": [], "results": [],
                   "confidence": 0.0}
        if record:
            _remember(outcome)
        return outcome

    # The same page reached through two tracking URLs is one source, and
    # counting it twice inflates confidence in exactly the way confidence
    # must not be inflated.
    candidates = deduplicate_sources(found["sources"])

    read, failed, results = [], [], []
    for source in candidates[:max_sources]:
        page = fetch_page(source["url"], fetcher=fetcher)
        if not page.get("ok"):
            failed.append({"url": source["url"], "title": source["title"],
                           "state": page.get("state"), "detail": page.get("detail")})
            continue
        source["read"] = True
        read.append({
            "url": page["url"], "title": page["title"],
            "observed_at": page["observed_at"],
            "source_type": source_type(page["url"]),
            "reliability": source_reliability(page["url"]),
            "freshness": freshness(published_at(page.get("html") or ""),
                                   page["observed_at"]),
        })
        try:
            results.append(extractor(page))
        except Exception as exc:
            logger.warning("extraction failed for %s: %s", page["url"], exc)
            failed.append({"url": page["url"], "state": FAILED, "detail": str(exc)[:200]})

    outcome = {
        "ok": bool(read),
        "state": OK if read else UNAVAILABLE,
        "question": question,
        "domain": domain,
        "prior_knowledge": prior,
        "sources_considered": len(candidates),
        "duplicates_removed": len(found["sources"]) - len(candidates),
        "sources_read": read,
        "sources_failed": failed,
        "results": results,
        # Confidence is the share of intended sources actually read. One
        # source out of three is a third, and says so.
        "confidence": round(len(read) / max(max_sources, 1), 2),
        "duration_ms": int((time.time() - started) * 1000),
        "detail": ("no source could be read" if not read else
                   f"read {len(read)} of {min(max_sources, len(candidates))} source(s)"),
    }
    if record:
        _remember(outcome)
        # LEARN. Observed findings become durable knowledge with the URL
        # that carried them, so the next run on the same subject starts
        # from what was already established instead of from zero.
        try:
            from actions import brain_memory
            brain_memory.learn_from_research(outcome, subject=question)
        except Exception:
            logger.debug("could not distil the research into knowledge", exc_info=True)
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
        age = (source.get("freshness") or {}).get("state")
        # A stale page reported without its age is the "old result presented
        # as current" error, so the age rides with every line.
        marker = {"CURRENT": "", "STALE": "  [STALE — ",
                  UNKNOWN: "  [age unknown]"}.get(age, "")
        if age == "STALE":
            marker += f"{(source.get('freshness') or {}).get('age_days')} days old]"
        lines.append(f"  - {source['title'] or source['url']} ({source['url']}) "
                     f"observed {source['observed_at']}"
                     f" [{source.get('source_type', 'unknown')}]{marker}")
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

# ══ SOURCE CHARACTER, FRESHNESS AND DEDUPLICATION ════════════════════════
# Everything below completes the record around a finding: what KIND of
# source it came from, how much weight that source's own claims carry,
# whether the page is current or stale, and whether two "different"
# sources are the same page twice.

SOURCE_RETAILER = "retailer"
SOURCE_JOB_BOARD = "job_board"
SOURCE_NEWS = "news"
SOURCE_REFERENCE = "reference"
SOURCE_COMPANY = "company_site"
SOURCE_SOCIAL = "social"
SOURCE_FORUM = "forum"
SOURCE_UNKNOWN_TYPE = "unknown"

# Host fragments that identify a KIND of site, never a specific answer.
# This shapes how a claim is weighted; it never supplies a claim.
_SOURCE_TYPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (SOURCE_JOB_BOARD, ("indeed.", "linkedin.com/jobs", "ziprecruiter.", "glassdoor.",
                        "monster.", "dice.", "builtin.", "lever.co", "greenhouse.io",
                        "workday", "jobs.", "careers.")),
    (SOURCE_RETAILER, ("amazon.", "walmart.", "target.", "homedepot.", "lowes.",
                       "bestbuy.", "ebay.", "etsy.", "newegg.", "shop")),
    (SOURCE_NEWS, ("reuters.", "bloomberg.", "wsj.", "nytimes.", "cnbc.", "bbc.",
                   "apnews.", "forbes.", "enr.com", "constructiondive.")),
    (SOURCE_REFERENCE, ("wikipedia.", ".gov", ".edu", "sec.gov", "bls.gov")),
    (SOURCE_SOCIAL, ("twitter.", "x.com", "facebook.", "instagram.", "tiktok.",
                     "youtube.", "linkedin.com/in", "linkedin.com/posts")),
    (SOURCE_FORUM, ("reddit.", "quora.", "stackexchange.", "stackoverflow.")),
)

# How much a source's own assertions are worth when sources disagree.
# Deliberately coarse: this decides which claim to LEAD with, never which
# claim to discard, and every observation survives in the record either way.
_RELIABILITY = {
    SOURCE_REFERENCE: 0.9,
    SOURCE_NEWS: 0.8,
    SOURCE_COMPANY: 0.75,
    SOURCE_RETAILER: 0.7,      # authoritative on its OWN price, nothing else
    SOURCE_JOB_BOARD: 0.7,
    SOURCE_FORUM: 0.4,
    SOURCE_SOCIAL: 0.35,
    SOURCE_UNKNOWN_TYPE: 0.5,
}

# Past this, a page's own stated date makes it history rather than news.
STALE_AFTER_DAYS = 30

_DATE_META = ("article:published_time", "article:modified_time", "datePublished",
              "dateModified", "og:updated_time", "date", "pubdate")
_DATE_TEXT_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2})\b|\b(\d{1,2}\s+"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})\b", re.I)


def source_type(url: str) -> str:
    """What KIND of site this is. Shapes how a claim is weighted; never
    supplies a claim."""
    host = _host(url)
    if not host:
        return SOURCE_UNKNOWN_TYPE
    lowered = url.lower()
    for kind, fragments in _SOURCE_TYPES:
        if any(fragment in host or fragment in lowered for fragment in fragments):
            return kind
    return SOURCE_UNKNOWN_TYPE


def source_reliability(url: str) -> float:
    return _RELIABILITY.get(source_type(url), 0.5)


def published_at(html: str) -> Optional[str]:
    """The page's own stated publication/update date, or None.

    None is a real answer and the common one: most pages do not say. It is
    never substituted with the retrieval time, because "when we looked" and
    "when it was written" are different facts and conflating them is how a
    five-year-old page gets reported as today's news."""
    soup = _soup(html or "")
    for key in _DATE_META:
        node = (soup.find("meta", property=key) or soup.find("meta", attrs={"name": key})
                or soup.find("meta", attrs={"itemprop": key}))
        if node and node.get("content"):
            return str(node["content"]).strip()
    node = soup.find("time")
    if node and node.get("datetime"):
        return str(node["datetime"]).strip()
    match = _DATE_TEXT_RE.search(soup.get_text(" ", strip=True)[:2000])
    return (match.group(1) or match.group(2)) if match else None


def _parse_date(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    for parse in (lambda t: datetime.fromisoformat(t),
                  lambda t: datetime.strptime(t[:10], "%Y-%m-%d")):
        try:
            parsed = parse(text)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def freshness(published: Optional[str], retrieved_at: Optional[str] = None) -> dict[str, Any]:
    """Whether a page's content is current, stale, or of unknown age.

    UNKNOWN is not a failure. A page with no stated date is genuinely of
    unknown age, and saying so is the honest answer — treating it as
    current because it was fetched today is exactly the "old cached result
    reported as current" error."""
    parsed = _parse_date(published)
    if parsed is None:
        return {"state": UNKNOWN, "published_at": published,
                "age_days": None,
                "detail": "the page does not state when it was published or updated"}
    reference = _parse_date(retrieved_at) or datetime.now(timezone.utc)
    age_days = max((reference - parsed).days, 0)
    return {
        "state": "CURRENT" if age_days <= STALE_AFTER_DAYS else "STALE",
        "published_at": parsed.isoformat(), "age_days": age_days,
        "detail": (f"the page states it was published/updated {age_days} day(s) ago"
                   + ("" if age_days <= STALE_AFTER_DAYS
                      else f" — older than the {STALE_AFTER_DAYS}-day freshness window")),
    }


def _canonical_url(url: str) -> str:
    """A URL stripped of tracking noise, for duplicate detection only. The
    real URL is always what gets reported."""
    parsed = urlparse(url or "")
    path = (parsed.path or "/").rstrip("/") or "/"
    return f"{(parsed.hostname or '').lower()}{path}"


def deduplicate_sources(sources: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per real page. The same URL with different tracking
    parameters is one source, and counting it twice inflates confidence in
    exactly the way confidence must not be inflated."""
    seen, out = set(), []
    for source in sources or []:
        key = _canonical_url(source.get("url", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(source)
    return out


def contradictions(results: Iterable[dict[str, Any]], field: str) -> dict[str, Any]:
    """Where sources disagree on one field, with each claim attributed.

    The most reliable source's value is offered as the one to lead with —
    but every value is kept, because "lead with" is not "the others were
    wrong", and a retailer is authoritative on its own price and on
    nothing else."""
    comparison = compare_field(results, field)
    if not comparison.get("ok") or not comparison.get("conflict"):
        return {**comparison, "contradiction": False}

    ranked = sorted(comparison["observations"],
                    key=lambda o: -source_reliability(o.get("source_url") or ""))
    return {
        **comparison, "contradiction": True,
        "lead_with": ranked[0],
        "lead_reason": (f"{_host(ranked[0].get('source_url') or '')} is a "
                        f"{source_type(ranked[0].get('source_url') or '')} source"),
        "all_claims": ranked,
        "detail": (f"{len(comparison['observations'])} source(s) disagree on {field}; "
                   f"every value is kept and attributed rather than averaged"),
    }


# ══ DOMAIN EXTRACTORS ════════════════════════════════════════════════════
# The engine is not product-specific. extract_product() handles commerce;
# these handle the other two shapes JARVIS is asked about most. All three
# obey the same rule: a field that was not found stays UNKNOWN.

_SALARY_RE = re.compile(
    r"[$£€]\s?([\d,]{2,})(?:\s?[kK])?(?:\s*(?:-|–|to)\s*[$£€]?\s?([\d,]{2,})(?:\s?[kK])?)?")
# Restricted to non-newline whitespace and a bounded word count so a city
# can never swallow an unrelated capitalized phrase across a line break —
# found live: "...Construction Operations\n\nPhoenix, AZ" matched
# "Construction Operations Phoenix" as one city before this fix.
_LOCATION_RE = re.compile(
    r"\b([A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){0,2}),[ \t]*([A-Z]{2})\b")


def extract_job(page: dict[str, Any]) -> dict[str, Any]:
    """Job-posting fields from a loaded page."""
    if not page.get("ok"):
        return {"ok": False, "state": page.get("state", FAILED),
                "detail": page.get("detail"), "fields": {}}

    text, html = page.get("text") or "", page.get("html") or ""
    url, title = page.get("url") or "", page.get("title") or ""
    soup = _soup(html)
    fields: dict[str, Any] = {}

    def _record(name, value, note=""):
        fields[name] = (finding(name, value, OBSERVED, url, title, note)
                        if value is not None else unknown(name))

    _record("job_title", title or None, "from the page title")

    company = (soup.find("meta", property="og:site_name")
               or soup.find("meta", attrs={"name": "company"}))
    _record("company", company.get("content") if company else None)

    location = _LOCATION_RE.search(text)
    _record("location", f"{location.group(1)}, {location.group(2)}" if location else None)

    salary = _SALARY_RE.search(text)
    if salary:
        low = salary.group(1).replace(",", "")
        high = (salary.group(2) or "").replace(",", "")
        _record("compensation", f"${low}" + (f"-${high}" if high else ""),
                "as stated in the posting")
    else:
        _record("compensation", None)

    _record("source_url", url or None)
    fields["source_type"] = finding("source_type", source_type(url), OBSERVED, url, title)
    return _domain_result(page, fields)


_OWNERSHIP_RE = re.compile(
    r"\b(?:owned by|a subsidiary of|acquired by|parent company(?: is)?)\s+"
    r"([A-Z][\w&.,'-]*(?:\s+[A-Z][\w&.,'-]*){0,4})")
_FOUNDED_RE = re.compile(r"\b(?:founded|established)\s+in\s+(\d{4})\b", re.I)
_EMPLOYEES_RE = re.compile(r"\b([\d,]{2,})\s*(?:\+\s*)?employees\b", re.I)


def extract_company(page: dict[str, Any]) -> dict[str, Any]:
    """Company facts from a loaded page.

    Ownership is REPORTED, never OBSERVED: the page asserts it and JARVIS
    has not verified it against a filing. That distinction is the whole
    point of the evidence classes."""
    if not page.get("ok"):
        return {"ok": False, "state": page.get("state", FAILED),
                "detail": page.get("detail"), "fields": {}}

    text, html = page.get("text") or "", page.get("html") or ""
    url, title = page.get("url") or "", page.get("title") or ""
    soup = _soup(html)
    fields: dict[str, Any] = {}

    description = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", property="og:description")
    fields["name"] = (finding("name", title, OBSERVED, url, title, "from the page title")
                      if title else unknown("name"))
    fields["description"] = (
        finding("description", description["content"][:500], REPORTED, url, title,
                "the page's own description of itself")
        if description and description.get("content") else unknown("description"))

    owner = _OWNERSHIP_RE.search(text)
    fields["owner"] = (finding("owner", owner.group(1).strip(), REPORTED, url, title,
                               "stated by this source; not verified against a filing")
                       if owner else unknown("owner"))

    founded = _first(_FOUNDED_RE, text, int)
    fields["founded"] = (finding("founded", founded, REPORTED, url, title)
                         if founded else unknown("founded"))

    employees = _first(_EMPLOYEES_RE, text, int)
    fields["employees"] = (finding("employees", employees, REPORTED, url, title)
                           if employees else unknown("employees"))

    location = _LOCATION_RE.search(text)
    fields["location"] = (finding("location", f"{location.group(1)}, {location.group(2)}",
                                  REPORTED, url, title)
                          if location else unknown("location"))
    return _domain_result(page, fields)


def _domain_result(page: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    """The common envelope: fields plus the source's character and age."""
    url = page.get("url") or ""
    return {
        "ok": True, "state": OK,
        "source_url": url,
        "source_title": page.get("title") or "",
        "source_type": source_type(url),
        "source_reliability": source_reliability(url),
        "observed_at": page.get("observed_at"),
        "freshness": freshness(published_at(page.get("html") or ""),
                               page.get("observed_at")),
        "fields": fields,
    }


EXTRACTORS = {
    "product": None,      # bound below, once extract_product exists
    "job": extract_job,
    "company": extract_company,
    "page": None,         # bound below
}


def extract_page(page: dict[str, Any]) -> dict[str, Any]:
    """The general case: no domain assumed. Title, summary and links, so a
    non-product, non-job, non-company question still returns structure
    rather than a wall of text."""
    if not page.get("ok"):
        return {"ok": False, "state": page.get("state", FAILED),
                "detail": page.get("detail"), "fields": {}}
    url, title = page.get("url") or "", page.get("title") or ""
    text = page.get("text") or ""
    summary = " ".join(text.split())[:800]
    fields = {
        "title": finding("title", title or None, OBSERVED, url, title),
        "summary": finding("summary", summary or None, REPORTED, url, title,
                           "the opening text of the page, not a paraphrase"),
    }
    return _domain_result(page, fields)


EXTRACTORS["product"] = extract_product
EXTRACTORS["page"] = extract_page


# What kind of question is this? Chooses an extractor; never an answer.
_DOMAIN_HINTS = (
    ("job", ("job", "jobs", "hiring", "role", "position", "salary", "vacancy",
             "vp of", "director of", "recruit", "career")),
    ("product", ("price", "cheapest", "buy", "product", "deal", "best seller",
                 "bestseller", "review", "rating", "cost of")),
    ("company", ("company", "who owns", "ownership", "founded", "headquarters",
                 "revenue", "employees", "acquired")),
)


def detect_domain(question: str) -> str:
    lowered = f" {(question or '').lower()} "
    for domain, hints in _DOMAIN_HINTS:
        if any(f" {hint} " in lowered or lowered.strip().startswith(hint)
               for hint in hints):
            return domain
    return "page"


# ══ WHEN A QUESTION NEEDS THE LIVE WEB ═══════════════════════════════════
# JARVIS answering "what does this cost right now" from training data is
# the same failure as inventing a price: the number is stated with
# confidence and is not a reading of anything. needs_live_research() is
# the trigger that routes those questions to the engine instead.

_LIVE_MARKERS = (
    "current", "currently", "right now", "today", "latest", "this week",
    "up to date", "up-to-date", "recent", "recently", "as of", "still",
    "price", "cost", "cheapest", "in stock", "availability", "available",
    "news", "happening", "trending", "who owns", "market", "hiring",
    "open roles", "job openings", "compare", "versus", " vs ",
    "look up", "search the web", "search for", "research", "find out",
    "what are people saying", "reviews",
)
# Questions about the system's own state are answered from the system, not
# the web — routing those to a search engine is worse than useless.
_INTERNAL_MARKERS = (
    "my calendar", "my email", "my inbox", "my tasks", "our candidates",
    "our jobs", "today's matches", "buildpro matches", "needs my approval",
    "system health", "what did you do", "our pipeline",
)


def needs_live_research(question: str) -> dict[str, Any]:
    """Whether answering this honestly requires reading the live web.

    Returns the decision AND the marker that produced it, so a wrong
    routing decision can be traced to the word that caused it rather than
    being an unexplained behaviour."""
    lowered = f" {(question or '').lower().strip()} "
    for marker in _INTERNAL_MARKERS:
        if marker in lowered:
            return {"needed": False, "reason": f"asks about JARVIS's own state ({marker.strip()!r})",
                    "domain": None}
    for marker in _LIVE_MARKERS:
        if marker in lowered:
            return {"needed": True, "reason": f"asks for live information ({marker.strip()!r})",
                    "domain": detect_domain(question)}
    return {"needed": False, "reason": "no live-information marker in the request",
            "domain": None}
