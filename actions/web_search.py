#web_search.py
import json
import sys
import threading
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


def _gemini_search(query: str) -> str:
    from google import genai

    client   = genai.Client(api_key=_get_api_key())
    response = client.models.generate_content(
        model="gemini-flash-latest",
        contents=query,
        config={"tools": [{"google_search": {}}]},
    )

    text = ""
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            text += part.text

    text = text.strip()
    if not text:
        raise ValueError("Gemini returned an empty response.")
    return text


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


def _format_ddg(query: str, results: list[dict]) -> str:
    if not results:
        return f"No results found for: {query}"

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


# ── Briefing helper ────────────────────────────────────────────────────────────

def _gemini_headlines(n: int = 5) -> tuple[list[str], str]:
    """
    Fetches current headlines via Gemini grounded search.
    Optimised for speed: minimal prompt + strict token cap.
    Returns (headline_list, raw_text_for_display).
    """
    import re
    from google import genai

    client = genai.Client(api_key=_get_api_key())
    response = client.models.generate_content(
        model="gemini-flash-latest",
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


# ── Reliability: race Gemini against DDG instead of waiting for Gemini to ──
# ── fail first (original bug: Gemini 429 RESOURCE_EXHAUSTED made every mode
#    slow/unreliable since DDG only got a turn *after* Gemini's failure —
#    which can be a slow SDK-internal retry loop, not a fast raise). Proven
#    first in _news(); now shared by search/research/price too. ────────────

def _log_search_error(label: str, exc: Exception) -> None:
    msg = str(exc)
    if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower():
        line = f"[WebSearch] WARNING: {label} quota exhausted (429) - falling back: {msg[:160]}"
    else:
        line = f"[WebSearch] WARNING: {label} failed ({msg[:160]}) - falling back"
    try:
        print(line)
    except UnicodeEncodeError:
        # Some Windows consoles (cp1252) can't encode every character that
        # might show up inside a raw exception message (msg[:160] above is
        # arbitrary third-party text, not something this code controls) —
        # this print is diagnostic only, so a fallback thread's real
        # success/failure must never hinge on stdout accepting every byte.
        print(line.encode("ascii", errors="replace").decode("ascii"))


def _race(primary_fn, fallback_fn, min_len: int = 40, timeout: float = 10.0,
          primary_label: str = "Gemini", fallback_label: str = "DDG") -> str | None:
    """Run `primary_fn` and `fallback_fn` concurrently; return whichever
    produces a valid (non-empty, sufficiently long) result first, or None if
    both fail/timeout. Neither function blocks the other — a hung or slow
    Gemini call no longer delays the DDG fallback."""
    result_box: list = [None]
    lock     = threading.Lock()
    done_evt = threading.Event()
    failures = [0]

    def _store(r: str) -> None:
        if r and len(r) >= min_len:
            with lock:
                if result_box[0] is None:
                    result_box[0] = r
            done_evt.set()
        else:
            with lock:
                failures[0] += 1
                if failures[0] >= 2:   # both failed — unblock caller
                    done_evt.set()

    def _run_primary():
        try:
            _store(primary_fn())
        except Exception as e:
            _log_search_error(primary_label, e)
            _store("")

    def _run_fallback():
        try:
            _store(fallback_fn())
        except Exception as e:
            _log_search_error(fallback_label, e)
            _store("")

    threading.Thread(target=_run_primary,  daemon=True).start()
    threading.Thread(target=_run_fallback, daemon=True).start()

    done_evt.wait(timeout=timeout)
    return result_box[0]


# ── Modes ──────────────────────────────────────────────────────────────────────

def _search(query: str) -> str:
    """Default search — Gemini grounded and DDG race concurrently."""
    result = _race(
        lambda: _gemini_search(query),
        lambda: _format_ddg(query, _ddg_search(query)),
    )
    return result or f"No results found for: {query}"


def _news(query: str) -> str:
    """Runs Gemini grounded search AND DDG news in parallel — first valid
    result wins; the original quota-resilient design this pattern is based on."""
    gemini_query = f"latest news today: {query}" if query else "top world news today"
    ddg_query    = query if query else "world news today"
    result = _race(
        lambda: _gemini_search(gemini_query),
        lambda: _format_news(ddg_query, _ddg_news(ddg_query, max_results=8)),
        min_len=60,
        primary_label="Gemini news",
        fallback_label="DDG news",
    )
    return result or f"No news found for: {query}"


def _research(query: str) -> str:
    """Deep dive — races Gemini's comprehensive answer against a wider DDG fetch."""
    research_query = (
        f"Comprehensive, detailed explanation of: {query}. "
        "Include background context, key facts, current state, and important nuances."
    )
    result = _race(
        lambda: _gemini_search(research_query),
        lambda: _format_ddg(query, _ddg_search(query, max_results=10)),
        min_len=60,
        primary_label="Research Gemini",
    )
    return result or f"No results found for: {query}"


def _price(query: str) -> str:
    """Product price lookup — races Gemini against a DDG price search."""
    price_query = f"current price of {query} — how much does it cost today"
    result = _race(
        lambda: _gemini_search(price_query),
        lambda: _format_ddg(query, _ddg_search(f"{query} price buy", max_results=6)),
        min_len=20,
        primary_label="Price Gemini",
    )
    return result or f"No pricing information found for: {query}"


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


# ── Public entry point ─────────────────────────────────────────────────────────

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
        return f"Search failed: {e}"
