"""Phase 4 Part 14 — adaptive startup briefing. main.py's
_startup_situation_clause turns real priority/risk/opportunity counts
into what Gemini should actually say, never a fixed script and never
fabricated urgency. Pure function, tested without a live session.
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _main():
    return load_module("jarvis_main_startup_briefing", "main.py")


def test_a_real_risk_produces_a_direct_attention_request():
    main = _main()
    clause = main._startup_situation_clause({"risk_count": 1, "priority_count": 3, "top_risk": "Buffer is broken", "opportunity_count": 0})
    assert "need their attention" in clause
    assert "Buffer is broken" in clause


def test_multiple_risks_use_plural_language():
    main = _main()
    clause = main._startup_situation_clause({"risk_count": 3, "priority_count": 5, "top_risk": None, "opportunity_count": 0})
    assert "3 high-priority issues" in clause


def test_no_risks_but_priorities_is_a_quiet_but_worthwhile_day():
    main = _main()
    clause = main._startup_situation_clause({"risk_count": 0, "priority_count": 4, "top_risk": None, "opportunity_count": 2})
    assert "nothing is urgent" in clause
    assert "4 items worth reviewing" in clause
    assert "2 opportunities" in clause


def test_nothing_at_all_is_a_genuinely_quiet_day():
    main = _main()
    clause = main._startup_situation_clause({"risk_count": 0, "priority_count": 0, "top_risk": None, "opportunity_count": 0})
    assert "quiet day" in clause


def test_missing_data_never_fabricates_urgency():
    main = _main()
    clause = main._startup_situation_clause(None)
    assert "normal day" in clause
    assert "notable" in clause


def test_singular_phrasing_for_exactly_one_priority():
    main = _main()
    clause = main._startup_situation_clause({"risk_count": 0, "priority_count": 1, "top_risk": None, "opportunity_count": 1})
    assert "1 item worth reviewing" in clause
    assert "1 opportunity on the board" in clause
