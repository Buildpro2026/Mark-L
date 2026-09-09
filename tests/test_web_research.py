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
