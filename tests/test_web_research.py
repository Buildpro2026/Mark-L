"""The web research engine.

actions/web_search.py returns FORMATTED PROSE — a paragraph, not data:
no source URL to follow, no timestamp, no way to tell an observed price
from a remembered one. So JARVIS could "search the web" and still not
answer "compare this across three sources and say when each was
observed", because the answer was already flattened into a sentence.

Deliberately minimal, per the brief. Four things: the engine executes,
it returns structured evidence, it distinguishes unavailable from
fabricated, and the tool path reaches it.
"""
import pytest

from actions import web_research as wr


# One deterministic fixture. Live web access is blocked in this
# environment (see the report), so the fetcher seam is driven directly —
# production passes no fetcher and uses the real HTTP path.
PAGE_A = """<html><head><title>Rotary Hammer XR — ToolShop</title>
<meta property="og:image" content="/img/hammer.jpg"></head>
<body><h1>Rotary Hammer XR</h1>
<p>Was $499.00, now $429.00</p>
<span>4.7 out of 5 stars</span><a>2,841 ratings</a>
<a href="/related/drill">Related drill</a></body></html>"""

PAGE_B = """<html><head><title>Rotary Hammer XR | BuildMart</title></head>
<body><p>Price: $449.50</p><span>4.5 out of 5</span></body></html>"""


def _fetcher(pages: dict, failures: dict = None):
    failures = failures or {}
    def _fetch(url, timeout):
        if url in failures:
            return failures[url]
        for key, html in pages.items():
            if key in url:
                return {"ok": True, "status": 200, "html": html, "url": url}
        return {"ok": False, "state": wr.FAILED, "detail": "not in fixture"}
    return _fetch


def _search(monkeypatch, urls):
    monkeypatch.setattr(wr, "search", lambda q, **k: {
        "ok": True, "state": wr.OK, "query": q,
        "sources": [{"url": u, "title": u, "snippet": "", "host": "x",
                     "found_at": "now", "read": False} for u in urls]})


@pytest.fixture(autouse=True)
def _no_memory(monkeypatch, tmp_path):
    from actions import operating_memory
    monkeypatch.setattr(operating_memory, "DB_PATH", tmp_path / "om.db")
    monkeypatch.setattr(wr, "MIN_SECONDS_BETWEEN_REQUESTS_PER_HOST", 0)


# 1. THE ENGINE EXECUTES A RESEARCH REQUEST ──────────────────────────────

def test_a_research_request_reads_sources_and_returns_findings(monkeypatch):
    _search(monkeypatch, ["https://toolshop.example/hammer", "https://buildmart.example/hammer"])
    out = wr.research("Rotary Hammer XR price", max_sources=2,
                      fetcher=_fetcher({"toolshop": PAGE_A, "buildmart": PAGE_B}))

    assert out["ok"] is True
    assert len(out["sources_read"]) == 2
    assert out["results"][0]["fields"]["price"]["value"] == 429.0
    assert out["results"][1]["fields"]["price"]["value"] == 449.50


# 2. STRUCTURED EVIDENCE AND SOURCE INFORMATION ──────────────────────────

def test_every_finding_carries_its_source_and_evidence_class(monkeypatch):
    _search(monkeypatch, ["https://toolshop.example/hammer"])
    out = wr.research("current price of the Rotary Hammer XR", max_sources=1, fetcher=_fetcher({"toolshop": PAGE_A}))
    price = out["results"][0]["fields"]["price"]

    assert price["evidence"] == wr.OBSERVED
    assert price["source_url"] == "https://toolshop.example/hammer"
    assert price["observed_at"], "an observed value with no timestamp"
    assert out["results"][0]["fields"]["rating"]["value"] == 4.7
    assert out["results"][0]["fields"]["review_count"]["value"] == 2841


def test_a_calculated_value_is_labelled_as_calculated(monkeypatch):
    _search(monkeypatch, ["https://toolshop.example/hammer"])
    out = wr.research("current price of the Rotary Hammer XR", max_sources=1, fetcher=_fetcher({"toolshop": PAGE_A}))
    fields = out["results"][0]["fields"]
    # $429 against a $499 also on the page.
    assert fields["discount_pct"]["evidence"] == wr.CALCULATED
    assert fields["original_price"]["evidence"] == wr.REPORTED


def test_comparison_exposes_conflict_rather_than_averaging(monkeypatch):
    _search(monkeypatch, ["https://toolshop.example/hammer", "https://buildmart.example/hammer"])
    out = wr.research("current price of the Rotary Hammer XR", max_sources=2,
                      fetcher=_fetcher({"toolshop": PAGE_A, "buildmart": PAGE_B}))
    comparison = wr.compare_field(out["results"], "price")

    assert comparison["source_count"] == 2
    assert comparison["conflict"] is True
    assert comparison["lowest"]["value"] == 429.0
    # An average is a number payable nowhere; both observations survive.
    assert {o["value"] for o in comparison["observations"]} == {429.0, 449.50}
    assert all(o["source_url"] and o["observed_at"] for o in comparison["observations"])


# 3. UNAVAILABLE IS NOT FABRICATED ───────────────────────────────────────

def test_a_field_that_is_not_on_the_page_stays_unknown(monkeypatch):
    _search(monkeypatch, ["https://buildmart.example/hammer"])
    out = wr.research("current price of the Rotary Hammer XR", max_sources=1, fetcher=_fetcher({"buildmart": PAGE_B}))
    review_count = out["results"][0]["fields"]["review_count"]

    assert review_count["value"] is None
    assert review_count["evidence"] == wr.UNKNOWN


def test_a_value_of_none_can_never_carry_a_confident_evidence_class():
    # The two cannot disagree, so `value` can never be mistaken for a reading.
    assert wr.finding("price", None, wr.OBSERVED)["evidence"] == wr.UNKNOWN


def test_a_blocked_source_is_reported_not_silently_dropped(monkeypatch):
    _search(monkeypatch, ["https://toolshop.example/hammer", "https://walled.example/x"])
    out = wr.research("current price of the Rotary Hammer XR", max_sources=2, fetcher=_fetcher(
        {"toolshop": PAGE_A},
        failures={"https://walled.example/x": {"ok": False, "state": wr.BLOCKED,
                                               "detail": "the site refused the request (HTTP 403)"}}))

    assert len(out["sources_read"]) == 1
    assert len(out["sources_failed"]) == 1
    assert out["sources_failed"][0]["state"] == wr.BLOCKED
    assert out["confidence"] == 0.5, "confidence must reflect what was actually read"
    assert "1 of 2" in out["detail"]


def test_no_reachable_source_is_never_reported_as_research(monkeypatch):
    _search(monkeypatch, ["https://down.example/a"])
    out = wr.research("current price of the Rotary Hammer XR", max_sources=1, fetcher=_fetcher(
        {}, failures={"https://down.example/a": {"ok": False, "state": wr.TIMEOUT,
                                                 "detail": "timed out"}}))
    assert out["ok"] is False
    assert out["results"] == []
    assert out["confidence"] == 0.0
    assert "couldn't research that" in wr.summarize(out)


def test_the_summary_never_claims_a_source_it_could_not_read(monkeypatch):
    _search(monkeypatch, ["https://toolshop.example/hammer", "https://walled.example/x"])
    out = wr.research("current price of the Rotary Hammer XR", max_sources=2, fetcher=_fetcher(
        {"toolshop": PAGE_A},
        failures={"https://walled.example/x": {"ok": False, "state": wr.BLOCKED, "detail": "403"}}))
    summary = wr.summarize(out)

    assert "Read 1 source(s)" in summary
    assert "1 source(s) could not be read" in summary
    assert "50%" in summary


def test_a_non_http_url_is_refused():
    result = wr.fetch_page("file:///etc/passwd")
    assert result["ok"] is False
    assert "http(s)" in result["detail"]


def test_research_is_distilled_into_memory_not_dumped(monkeypatch):
    from actions import operating_memory
    recorded = []
    monkeypatch.setattr(operating_memory, "record",
                        lambda *a, **k: recorded.append(k) or 1)
    _search(monkeypatch, ["https://toolshop.example/hammer"])
    wr.research("current price of the Rotary Hammer XR", max_sources=1, fetcher=_fetcher({"toolshop": PAGE_A}))

    assert recorded, "the run was never remembered"
    stored = str(recorded[0])
    assert "toolshop.example" in stored          # the source reference is kept
    assert "Rotary Hammer" not in stored or len(stored) < 2000, "page content was dumped into memory"


# 4. THE JARVIS TOOL PATH REACHES IT ─────────────────────────────────────

def test_the_conversation_tool_path_invokes_the_research_engine(monkeypatch):
    import asyncio
    from core.headless.tool_executor import ToolExecutor, ToolContext

    _search(monkeypatch, ["https://toolshop.example/hammer", "https://buildmart.example/hammer"])
    real = wr.research
    monkeypatch.setattr(wr, "research", lambda q, **k: real(
        q, max_sources=k.get("max_sources", 2),
        fetcher=_fetcher({"toolshop": PAGE_A, "buildmart": PAGE_B})))

    result = asyncio.run(ToolExecutor(ToolContext()).execute(
        "web_research", {"question": "Rotary Hammer XR price", "max_sources": 2}))

    assert "Read 2 source(s)" in result
    assert "429.0" in result and "449.5" in result
    assert "Lowest observed" in result
    assert "disagree" in result
    assert "toolshop.example" in result, "the answer must carry its sources"


# 5. LIVE SOURCE ACQUISITION RESILIENCE — the reported production bug,
# and Gemini-primary/DDG-fallback ────────────────────────────────────────
#
# Production symptom: a price/product question got "The model does not
# appear to be released or listed on major retailers..." — a fabricated
# non-existence claim. Root cause: search() had exactly one source-
# discovery path. When it was unreachable (a real, observed failure in
# this environment), search() failed outright with no fallback, and
# nothing told the model that a failed search is not evidence the
# product doesn't exist.
#
# Gemini's own grounded search is now the PRIMARY discovery path (Lee's
# explicit instruction: use Gemini first when it is available and has
# quota); DDG is the FALLBACK, tried whenever Gemini cannot complete the
# request — quota exhausted, rate-limited, temporarily unavailable, or
# any other failure. This does not depend on Google's paid Search
# grounding being available at all: DDG is attempted completely
# independently of Gemini and works even when Gemini is entirely
# unreachable.

def _fake_grounded_response(chunks):
    web_chunks = [type("Chunk", (), {"web": type("Web", (), {"uri": u, "title": t})()})()
                  for u, t in chunks]
    candidate = type("Candidate", (), {
        "content": type("Content", (), {"parts": []})(),
        "grounding_metadata": type("Meta", (), {"grounding_chunks": web_chunks})(),
    })()
    return type("Response", (), {"candidates": [candidate]})()


def test_gemini_is_attempted_first_and_ddg_is_never_called_on_success(monkeypatch):
    # Requirement 1 & 2: Gemini is tried first, and a Gemini success is
    # returned as-is — the fallback path is not even touched.
    from actions import web_search as ws

    monkeypatch.setattr(ws, "_gemini_grounded_response", lambda q: _fake_grounded_response([
        ("https://www.apple.com/iphone-16/", "iPhone 16 - Apple"),
        ("https://www.bestbuy.com/site/iphone-16", "iPhone 16 at Best Buy"),
    ]))

    def _ddg_must_not_be_called(query, max_results=6):
        raise AssertionError("DDG must not be called when Gemini succeeds")
    monkeypatch.setattr(ws, "_ddg_search", _ddg_must_not_be_called)

    result = wr.search("iPhone 16 128GB price")

    assert result["ok"] is True
    assert result["state"] == wr.OK
    urls = {s["url"] for s in result["sources"]}
    assert "https://www.apple.com/iphone-16/" in urls
    assert "https://www.bestbuy.com/site/iphone-16" in urls


@pytest.mark.parametrize("gemini_exc", [
    RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded for gemini-2.5-flash"),
    RuntimeError("503 UNAVAILABLE: the model is temporarily overloaded"),
    TimeoutError("Gemini grounded search timed out"),
])
def test_search_falls_back_to_ddg_when_gemini_cannot_complete(monkeypatch, gemini_exc):
    # Requirements 3 & 4: a quota/rate-limit error and a plain
    # unavailability both fall back to DDG, which works independently of
    # Gemini (no paid Search grounding dependency).
    from actions import web_search as ws

    def _gemini_boom(query):
        raise gemini_exc
    monkeypatch.setattr(ws, "_gemini_grounded_response", _gemini_boom)
    monkeypatch.setattr(ws, "_ddg_search", lambda query, max_results=6: [
        {"title": "iPhone 16 - Apple", "href": "https://www.apple.com/iphone-16/",
         "body": "The latest iPhone, starting at $799."},
    ])

    result = wr.search("iPhone 16 128GB price")

    assert result["ok"] is True
    assert result["state"] == wr.OK
    urls = {s["url"] for s in result["sources"]}
    assert "https://www.apple.com/iphone-16/" in urls


def test_search_reports_failure_honestly_when_both_paths_are_unreachable(monkeypatch):
    # Requirement 5: both providers failing is an honest "unavailable",
    # never a claim about the subject.
    from actions import web_search as ws

    monkeypatch.setattr(ws, "_gemini_grounded_response",
                        lambda query: (_ for _ in ()).throw(RuntimeError("429 RESOURCE_EXHAUSTED")))
    monkeypatch.setattr(ws, "_ddg_search",
                        lambda query, max_results=6: (_ for _ in ()).throw(RuntimeError("proxy 403")))

    result = wr.search("iPhone 16 128GB price")

    assert result["ok"] is False
    assert result["state"] == wr.UNAVAILABLE
    assert result["sources"] == []
    # The failure is about web access, not the subject — no claim of any
    # kind about the product itself anywhere in the detail text.
    assert "iphone" not in result["detail"].lower()
    assert "release" not in result["detail"].lower()
    assert "exist" not in result["detail"].lower()


def test_full_research_run_never_claims_non_existence_when_search_is_unreachable(monkeypatch):
    # Requirement 6, end-to-end reproduction of the exact production
    # report: both source-discovery paths fail -> research() ->
    # summarize() must say the research could not be completed, and must
    # never say or imply the product doesn't exist / isn't released /
    # isn't sold.
    from actions import web_search as ws

    monkeypatch.setattr(ws, "_gemini_grounded_response",
                        lambda query: (_ for _ in ()).throw(RuntimeError("429 RESOURCE_EXHAUSTED")))
    monkeypatch.setattr(ws, "_ddg_search",
                        lambda query, max_results=6: (_ for _ in ()).throw(RuntimeError("proxy 403")))

    outcome = wr.research("current price of iPhone 16 128GB, compare at least three sources")
    spoken = wr.summarize(outcome)

    assert outcome["ok"] is False
    assert outcome["confidence"] == 0.0
    forbidden = ("does not appear", "not released", "not available", "doesn't exist",
                "does not exist", "not listed", "discontinued")
    lowered = spoken.lower()
    for phrase in forbidden:
        assert phrase not in lowered, f"fabricated non-existence claim: {phrase!r} in {spoken!r}"
    assert "could not be completed" in spoken


def test_search_still_reports_no_results_honestly_when_search_actually_works(monkeypatch):
    # A working search that genuinely finds nothing is a different, real
    # outcome from "the web was unreachable" — the two must not collapse
    # into the same message.
    from actions import web_search as ws

    monkeypatch.setattr(ws, "_gemini_grounded_response",
                        lambda query: _fake_grounded_response([]))
    monkeypatch.setattr(ws, "_ddg_search", lambda query, max_results=6: [])

    result = wr.search("a query with genuinely no results anywhere")

    assert result["ok"] is True
    assert result["state"] == wr.NO_RESULTS
