import time

from actions import web_search as ws


# ── _race: the core reliability primitive ──────────────────────────────────

def test_race_returns_primary_result_when_it_succeeds():
    result = ws._race(lambda: "a valid gemini answer that is long enough", lambda: "ddg fallback result long enough")
    assert result == "a valid gemini answer that is long enough"


def test_race_falls_back_when_primary_raises():
    def boom():
        raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")

    result = ws._race(boom, lambda: "a working ddg fallback result that is long enough")
    assert result == "a working ddg fallback result that is long enough"


def test_race_does_not_wait_for_a_slow_primary_before_using_the_fallback():
    # This is the actual fix for the original bug: a hung/slow Gemini call
    # (e.g. an internal retry loop on a 429) must not delay DDG's result.
    def slow_gemini():
        time.sleep(3.0)
        return "gemini result that would have been long enough eventually"

    def fast_ddg():
        return "fast ddg result that is long enough to count as valid"

    started = time.monotonic()
    result = ws._race(slow_gemini, fast_ddg, timeout=10.0)
    elapsed = time.monotonic() - started

    assert result == "fast ddg result that is long enough to count as valid"
    assert elapsed < 2.0   # did not wait anywhere near the slow primary's 3s


def test_race_returns_none_when_both_fail():
    result = ws._race(lambda: (_ for _ in ()).throw(RuntimeError("gemini down")),
                       lambda: "")
    assert result is None


def test_race_rejects_too_short_results_as_invalid():
    result = ws._race(lambda: "short", lambda: "also short", min_len=40)
    assert result is None


def test_log_search_error_distinguishes_quota_from_other_failures(capsys):
    ws._log_search_error("Gemini", RuntimeError("429 RESOURCE_EXHAUSTED"))
    out = capsys.readouterr().out
    assert "quota exhausted" in out.lower()

    ws._log_search_error("Gemini", RuntimeError("connection reset"))
    out = capsys.readouterr().out
    assert "quota exhausted" not in out.lower()


# ── Full modes under simulated Gemini quota exhaustion (the original bug) ──

def test_search_falls_back_to_ddg_when_gemini_is_quota_exhausted(monkeypatch):
    monkeypatch.setattr(ws, "_gemini_search", lambda q: (_ for _ in ()).throw(
        RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded for gemini-2.5-flash")))
    monkeypatch.setattr(ws, "_ddg_search", lambda q, max_results=6: [
        {"title": "Result A", "snippet": "A useful snippet about the query.", "url": "https://a.example"},
    ])
    result = ws._search("current bitcoin price")
    assert "Result A" in result
    assert "429" not in result


def test_news_falls_back_to_ddg_when_gemini_is_quota_exhausted(monkeypatch):
    monkeypatch.setattr(ws, "_gemini_search", lambda q: (_ for _ in ()).throw(
        RuntimeError("429 RESOURCE_EXHAUSTED")))
    monkeypatch.setattr(ws, "_ddg_news", lambda q, max_results=8: [
        {"title": "Breaking headline", "snippet": "Details about today's top story.", "url": "https://news.example", "source": "Example News"},
    ])
    result = ws._news("technology")
    assert "Breaking headline" in result


def test_research_falls_back_to_ddg_when_gemini_is_quota_exhausted(monkeypatch):
    monkeypatch.setattr(ws, "_gemini_search", lambda q: (_ for _ in ()).throw(
        RuntimeError("429 RESOURCE_EXHAUSTED")))
    monkeypatch.setattr(ws, "_ddg_search", lambda q, max_results=10: [
        {"title": "Deep dive result", "snippet": "A long comprehensive explanation of the topic at hand.", "url": "https://r.example"},
    ])
    result = ws._research("quantum computing")
    assert "Deep dive result" in result


def test_price_falls_back_to_ddg_when_gemini_is_quota_exhausted(monkeypatch):
    monkeypatch.setattr(ws, "_gemini_search", lambda q: (_ for _ in ()).throw(
        RuntimeError("429 RESOURCE_EXHAUSTED")))
    monkeypatch.setattr(ws, "_ddg_search", lambda q, max_results=6: [
        {"title": "Price listing", "snippet": "Currently priced at $499 at major retailers.", "url": "https://shop.example"},
    ])
    result = ws._price("playstation 5")
    assert "Price listing" in result


def test_web_search_entry_point_never_surfaces_a_raw_429_to_the_user(monkeypatch):
    monkeypatch.setattr(ws, "_gemini_search", lambda q: (_ for _ in ()).throw(
        RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")))
    monkeypatch.setattr(ws, "_ddg_search", lambda q, max_results=6: [
        {"title": "Fallback works", "snippet": "This came from DuckDuckGo instead.", "url": "https://ok.example"},
    ])
    result = ws.web_search(parameters={"query": "what happened today", "mode": "search"})
    assert "Fallback works" in result
    assert "429" not in result
    assert "RESOURCE_EXHAUSTED" not in result


# ── _grounding_sources / _gemini_search: real citations, never invented ───

def _fake_grounded_response(text="The current price is $999.", chunks=None):
    web_chunks = [type("Chunk", (), {"web": type("Web", (), {"uri": u, "title": t})()})()
                  for u, t in (chunks or [])]
    candidate = type("Candidate", (), {
        "content": type("Content", (), {"parts": [type("Part", (), {"text": text})()]})(),
        "grounding_metadata": type("Meta", (), {"grounding_chunks": web_chunks})(),
    })()
    return type("Response", (), {"candidates": [candidate]})()


def test_grounding_sources_extracts_real_citation_urls():
    response = _fake_grounded_response(chunks=[
        ("https://www.apple.com/iphone-17/", "iPhone 17 - Apple"),
        ("https://www.bestbuy.com/site/iphone-17", "iPhone 17 at Best Buy"),
    ])
    sources = ws._grounding_sources(response)
    assert sources == [
        {"url": "https://www.apple.com/iphone-17/", "title": "iPhone 17 - Apple"},
        {"url": "https://www.bestbuy.com/site/iphone-17", "title": "iPhone 17 at Best Buy"},
    ]


def test_grounding_sources_deduplicates_repeated_urls():
    response = _fake_grounded_response(chunks=[
        ("https://example.com/a", "A"), ("https://example.com/a", "A again"),
    ])
    assert len(ws._grounding_sources(response)) == 1


def test_grounding_sources_is_empty_with_no_chunks_never_invents_one():
    response = _fake_grounded_response(chunks=[])
    assert ws._grounding_sources(response) == []


def test_grounding_sources_survives_a_response_shape_with_no_metadata_at_all():
    response = type("Response", (), {"candidates": [type("C", (), {})()]})()
    assert ws._grounding_sources(response) == []


# ── Double failure: never fabricate non-existence from a search outage ────
# (production bug: JARVIS told the user "the iPhone 16 has not been
# released" when both search backends were simply unreachable. The gap was
# these modes' own unprotected retry of _ddg_search() after _race() already
# gave up — it could raise straight into the generic "Search failed: {e}"
# handler, which has no anti-fabrication caveat at all.)

def test_search_reports_could_not_verify_when_both_backends_fail(monkeypatch):
    monkeypatch.setattr(ws, "_gemini_search", lambda q: (_ for _ in ()).throw(
        RuntimeError("connection reset")))
    monkeypatch.setattr(ws, "_ddg_search", lambda q, max_results=6: (_ for _ in ()).throw(
        RuntimeError("connection reset")))
    result = ws._search("iphone 16 price")
    assert "could not verify" in result.lower()
    # The message states the non-existence caveat as an explicit negation —
    # "NOT evidence that X does not exist... was never released" — never as
    # a bare, unqualified claim.
    assert "not evidence" in result.lower()


def test_price_reports_could_not_verify_when_both_backends_fail(monkeypatch):
    monkeypatch.setattr(ws, "_gemini_search", lambda q: (_ for _ in ()).throw(
        RuntimeError("connection reset")))
    monkeypatch.setattr(ws, "_ddg_search", lambda q, max_results=6: (_ for _ in ()).throw(
        RuntimeError("connection reset")))
    result = ws._price("iphone 16")
    assert "could not verify" in result.lower()
    assert "not evidence" in result.lower()


def test_research_reports_could_not_verify_when_both_backends_fail(monkeypatch):
    monkeypatch.setattr(ws, "_gemini_search", lambda q: (_ for _ in ()).throw(
        RuntimeError("connection reset")))
    monkeypatch.setattr(ws, "_ddg_search", lambda q, max_results=10: (_ for _ in ()).throw(
        RuntimeError("connection reset")))
    result = ws._research("iphone 16")
    assert "could not verify" in result.lower()
    assert "not evidence" in result.lower()


def test_web_search_entry_point_never_fabricates_nonexistence_on_a_hard_failure(monkeypatch):
    def boom(query):
        raise RuntimeError("network unreachable")
    monkeypatch.setattr(ws, "_search", boom)
    result = ws.web_search(parameters={"query": "iphone 16 price", "mode": "search"})
    assert "could not verify" in result.lower()
    assert "not evidence" in result.lower()


def test_gemini_search_includes_real_sources_and_a_checked_timestamp(monkeypatch):
    response = _fake_grounded_response(
        text="The current price is $999.",
        chunks=[("https://www.apple.com/iphone-17/", "iPhone 17 - Apple")])

    class _FakeModels:
        def generate_content(self, **kw):
            return response

    import core.headless.gemini_client as gemini_client
    monkeypatch.setattr(gemini_client, "get_client",
                        lambda key: type("Client", (), {"models": _FakeModels()})())
    monkeypatch.setattr(ws, "_get_api_key", lambda: "fake-key")

    result = ws._gemini_search("current price of iphone 17")

    assert "The current price is $999." in result
    assert "https://www.apple.com/iphone-17/" in result
    assert "checked " in result and "UTC" in result
