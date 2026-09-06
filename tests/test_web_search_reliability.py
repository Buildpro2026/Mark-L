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
