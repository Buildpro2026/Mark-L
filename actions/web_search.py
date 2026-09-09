#web_search.py
import json
import sys
import threading
import time
from pathlib import Path

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _grounding_sources(response) -> list[dict]:
    """Real citation URLs Gemini's own google_search grounding actually
    used — never the model's own prose, which can name a URL from memory
    that was never read. candidate.grounding_metadata.grounding_chunks[]
    is the API's own record of what it looked at; anything not present
    there is not a verified source, so this returns [] rather than
    inventing one."""
    try:
        candidate = response.candidates[0]
        meta = getattr(candidate, "grounding_metadata", None)
        chunks = getattr(meta, "grounding_chunks", None) or []
    except (AttributeError, IndexError):
        return []

    sources, seen = [], set()
    for chunk in chunks:
        web = getattr(chunk, "web", None)
        if web is None or not getattr(web, "uri", None):
            continue
        uri = web.uri
        if uri in seen:
            continue
        seen.add(uri)
        sources.append({"url": uri, "title": getattr(web, "title", "") or ""})
    return sources


def _gemini_grounded_response(query: str):
    """The raw Gemini grounded-search API response — shared by _gemini_
    search (prose + real citations) and gemini_grounded_sources (just the
    structured citations, for callers like web_research.py that need
    source discovery without the prose)."""
    from core.headless.gemini_client import get_client

    client = get_client(_get_api_key())
    return client.models.generate_content(
        model="gemini-3.8-flash",
        contents=query,
        config={"tools": [{"google_search": {}}]},
    )


def gemini_grounded_sources(query: str) -> list[dict]:
    """Real, API-verified source URLs for `query` via Gemini's own grounded
    search, structured (url/title) rather than prose — the fallback
    source-discovery path web_research.py's search() uses when DDG is
    unreachable. Never raises and never invents a citation: any failure,
    including an empty grounding response, returns []."""
    try:
        response = _gemini_grounded_response(query)
    except Exception as exc:
        print(f"[WebSearch] ⚠️ Gemini grounded source discovery failed: {exc}")
        return []
    return _grounding_sources(response)


def _gemini_search(query: str) -> str:
    from datetime import datetime, timezone

    response = _gemini_grounded_response(query)

    text = ""
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            text += part.text

    text = text.strip()
    if not text:
        raise ValueError("Gemini returned an empty response.")

    # Real, API-verified citations only — never invented. A grounded
    # search with no chunks at all (a rare but real response shape) just
    # gets the timestamp, honestly, with no fabricated "Sources:" list.
    sources = _grounding_sources(response)
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [text, "", f"(checked {checked_at})"]
    if sources:
        lines.append("Sources:")
        for s in sources[:5]:
            lines.append(f"  - {s['title'] or s['url']}: {s['url']}")
    return "\n".join(lines).strip()


def _ddg_search(query: str, max_results: int = 6) -> list[dict]:
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            results.append({
                "title":   r.get("title",  ""),
                "snippet": r.get("body",   ""),
                "url":     r.get("href",   ""),
            })
    return results


def _ddg_news(query: str, max_results: int = 8) -> list[dict]:
    """DDG news search — returns actual articles, not website homepages."""
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.news(query, max_results=max_results):
                results.append({
                    "title":   r.get("title",  ""),
                    "snippet": r.get("body",   ""),
                    "url":     r.get("url",    ""),
                    "source":  r.get("source", ""),
                })
    except Exception as e:
        print(f"[WebSearch] ⚠️ DDG news() failed ({e}) — falling back to text search")
        results = _ddg_search(query, max_results=max_results)
    return results


def _could_not_verify(query: str, reason: str = "") -> str:
    """The one honest failure message for every web_search mode: live
    search could not be completed. Deliberately states the non-existence
    caveat INSIDE the tool result itself, not only in the system prompt —
    a caveat sitting right next to the failure a model is about to
    describe is a much stronger, harder-to-miss signal than general
    guidance stated once earlier in context, and this exact gap (a bare
    "Search failed: <raw exception>" with no such caveat) is what let
    "the iPhone 16 has not been released" get fabricated from a genuine
    search outage."""
    detail = f" ({reason})" if reason else ""
    return (
        f"I could not verify this right now — live web search is currently "
        f"unavailable{detail}. This is NOT evidence that '{query}' does not "
        f"exist, was never released, or is unavailable; it only means the "
        f"check could not be completed. Tell the user plainly that this "
        f"could not be verified — never guess or invent an explanation."
    )


def _format_ddg(query: str, results: list[dict]) -> str:
    if not results:
        return (f"No results found for: {query}. This does not mean it doesn't "
                f"exist — only that this particular search did not surface it.")

    lines = [f"Search results for: {query}\n"]
    for i, r in enumerate(results, 1):
        if r.get("title"):   lines.append(f"{i}. {r['title']}")
        if r.get("snippet"): lines.append(f"   {r['snippet']}")
        if r.get("url"):     lines.append(f"   Source: {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


def _format_news(query: str, results: list[dict]) -> str:
    if not results:
        return f"No news found for: {query}"

    lines = [f"Latest news: {query}\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        if not title:
            continue
        src = f"  [{r['source']}]" if r.get("source") else ""
        lines.append(f"{i}. {title}{src}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet'][:140]}")
        if r.get("url"):
            lines.append(f"   {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


# ── Briefing helper ────────────────────────────────────────────────

def _gemini_headlines(n: int = 5) -> tuple[list[str], str]:
    """
    Fetches current headlines via Gemini grounded search.
    Optimised for speed: minimal prompt + strict token cap.
    Returns (headline_list, raw_text_for_display).
    """
    import re
    from core.headless.gemini_client import get_client

    client = get_client(_get_api_key())
    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=f"Current world news: {n} headlines. Numbered list, titles only.",
        config={"tools": [{"google_search": {}}]},
    )

    raw = ""
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            raw += part.text

    headlines = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        # Only accept lines that begin with a number — skips preamble/closing sentences
        if not re.match(r'^[\d]+[.\)\-]', line):
            continue
        clean = re.sub(r'^[\d]+[.\)\-]\s*', '', line)
        clean = re.sub(r'^\*+\s*',          '', clean).strip()
        if clean and len(clean) > 10:
            headlines.append(clean)

    return headlines[:n], raw.strip()


# ── Reliability primitive ──────────────────────────────────────────────

def _log_search_error(source: str, exc: Exception) -> None:
    """Distinguishes a quota/rate-limit failure (expected, recoverable via
    fallback, not actionable by the user) from any other error, so logs
    don't cry wolf on the routine case."""
    text = str(exc)
    if "429" in text or "RESOURCE_EXHAUSTED" in text.upper():
        print(f"[WebSearch] {source} quota exhausted: {text}")
    else:
        print(f"[WebSearch] {source} error: {text}")


def _race(primary, fallback, timeout: float = 10.0, min_len: int = 40):
    """Runs `primary` and `fallback` concurrently and returns whichever
    produces a valid (>= min_len chars) string result first.

    This is the actual fix for a hung/slow primary (e.g. Gemini stuck in
    an internal retry loop on a 429): a plain try/except fallback only
    helps once the primary actually raises — if it just hangs, the user
    waits for it regardless. Racing means a fast, healthy fallback is
    never held hostage by a slow primary. Primary still wins any genuine
    tie (both resolve within the short grace window) so a working Gemini
    is preferred over DDG whenever it isn't actually the bottleneck."""
    box: dict = {}

    def _valid(r) -> bool:
        return isinstance(r, str) and len(r) >= min_len

    def _run(fn, key: str) -> None:
        try:
            box[key] = fn()
        except Exception as e:
            _log_search_error(key, e)
            box[key] = None

    threading.Thread(target=_run, args=(primary, "primary"), daemon=True).start()
    threading.Thread(target=_run, args=(fallback, "fallback"), daemon=True).start()

    grace_deadline = time.monotonic() + min(0.3, timeout)
    while time.monotonic() < grace_deadline:
        if "primary" in box:
            if _valid(box["primary"]):
                return box["primary"]
            break
        time.sleep(0.01)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if "primary" in box and _valid(box["primary"]):
            return box["primary"]
        if "fallback" in box:
            if _valid(box["fallback"]):
                return box["fallback"]
            break
        time.sleep(0.01)

    if _valid(box.get("primary")):
        return box["primary"]
    if _valid(box.get("fallback")):
        return box["fallback"]
    return None


# ── Modes ─────────────────────────────────────────────────────────────

def _search(query: str) -> str:
    """Default search — Gemini grounded, DDG fallback, raced so a slow/
    hung Gemini call never delays a healthy DDG result. min_len=1 here
    (not the _race default): DDG's own formatter always returns a
    meaningful, non-empty string even for a legitimate "no results"
    answer, which should still win over a hung Gemini call rather than
    being discarded as "too short" and re-fetched a second time."""
    result = _race(
        lambda: _gemini_search(query),
        lambda: _format_ddg(query, _ddg_search(query)),
        min_len=1,
    )
    if result is not None:
        return result
    try:
        return _format_ddg(query, _ddg_search(query))
    except Exception as e:
        _log_search_error("search-retry", e)
        return _could_not_verify(query, "search backends are unreachable right now")


def _news(query: str) -> str:
    """
    Runs Gemini grounded search AND DDG news in parallel.
    Returns whichever delivers a valid result first; cancels the other.
    """
    import threading

    gemini_query = f"latest news today: {query}" if query else "top world news today"
    ddg_query    = query if query else "world news today"

    result_box  = [None]   # first valid result lands here
    lock        = threading.Lock()
    done_evt    = threading.Event()
    failures    = [0]

    def _store(r: str) -> None:
        if r and len(r) > 60:
            with lock:
                if result_box[0] is None:
                    result_box[0] = r
            done_evt.set()
        else:
            with lock:
                failures[0] += 1
                if failures[0] >= 2:   # both failed — unblock caller
                    done_evt.set()

    def _try_gemini():
        try:
            _store(_gemini_search(gemini_query))
        except Exception as e:
            print(f"[WebSearch] ⚠️ Gemini news failed ({e})")
            _store("")

    def _try_ddg():
        try:
            results = _ddg_news(ddg_query, max_results=8)
            _store(_format_news(ddg_query, results))
        except Exception as e:
            print(f"[WebSearch] ⚠️ DDG news failed ({e})")
            _store("")

    threading.Thread(target=_try_gemini, daemon=True).start()
    threading.Thread(target=_try_ddg,    daemon=True).start()

    done_evt.wait(timeout=10.0)
    return result_box[0] or f"No news found for: {query}"


def _research(query: str) -> str:
    """
    Deep dive — asks Gemini for a comprehensive answer with context.
    Falls back to a wider DDG fetch.
    """
    research_query = (
        f"Comprehensive, detailed explanation of: {query}. "
        "Include background context, key facts, current state, and important nuances."
    )
    result = _race(
        lambda: _gemini_search(research_query),
        lambda: _format_ddg(query, _ddg_search(query, max_results=10)),
        min_len=1,
    )
    if result is not None:
        return result
    try:
        return _format_ddg(query, _ddg_search(query, max_results=10))
    except Exception as e:
        _log_search_error("research-retry", e)
        return _could_not_verify(query, "search backends are unreachable right now")


def _price(query: str) -> str:
    """Product price lookup — searches for current market prices."""
    price_query = f"current price of {query} — how much does it cost today"
    result = _race(
        lambda: _gemini_search(price_query),
        lambda: _format_ddg(query, _ddg_search(f"{query} price buy", max_results=6)),
        min_len=1,
    )
    if result is not None:
        return result
    try:
        return _format_ddg(query, _ddg_search(f"{query} price buy", max_results=6))
    except Exception as e:
        _log_search_error("price-retry", e)
        return _could_not_verify(query, "search backends are unreachable right now")


def _compare(items: list[str], aspect: str) -> str:
    query = (
        f"Compare {', '.join(items)} in terms of {aspect}. "
        "Give specific facts and data."
    )
    try:
        return _gemini_search(query)
    except Exception as e:
        print(f"[WebSearch] ⚠️ Gemini compare failed: {e} — falling back to DDG")

    all_results: dict[str, list] = {}
    for item in items:
        try:
            all_results[item] = _ddg_search(f"{item} {aspect}", max_results=3)
        except Exception:
            all_results[item] = []

    lines = [f"Comparison — {aspect.upper()}", "─" * 40]
    for item in items:
        lines.append(f"\n▸ {item}")
        for r in all_results.get(item, [])[:2]:
            if r.get("snippet"):
                lines.append(f"  • {r['snippet']}")
            if r.get("url"):
                lines.append(f"    {r['url']}")
    return "\n".join(lines)


# ── Public entry point ───────────────────────────────────────────────

def web_search(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    query  = params.get("query", "").strip()
    mode   = params.get("mode",  "search").lower().strip()
    items  = params.get("items", [])
    aspect = params.get("aspect", "general").strip() or "general"

    if not query and not items:
        return "Please provide a search query."

    if items and mode not in ("compare",):
        mode = "compare"

    if player:
        player.write_log(f"[Search:{mode}] {query or ', '.join(items)}")

    print(f"[WebSearch] 🔍 mode={mode!r}  query={query!r}")

    try:
        if mode == "compare" and items:
            return _compare(items, aspect)
        if mode == "news":
            return _news(query)
        if mode == "research":
            return _research(query)
        if mode == "price":
            return _price(query)
        return _search(query)

    except Exception as e:
        print(f"[WebSearch] ❌ All backends failed: {e}")
        return _could_not_verify(query or ", ".join(items), str(e))
