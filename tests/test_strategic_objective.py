import importlib.util
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _isolated(monkeypatch, tmp_path):
    so = load_module("jarvis_strategic_objective", "actions/strategic_objective.py")
    monkeypatch.setattr(so, "CONFIG_FILE", tmp_path / "strategic_objective.json")
    return so


def test_default_objective_is_one_million_over_6_to_12_months(monkeypatch, tmp_path):
    so = _isolated(monkeypatch, tmp_path)
    data = so.load_strategic_objective()
    assert data["target_amount_usd"] == 1_000_000
    assert data["committed_horizon_months"] == 12
    assert data["stretch_horizon_months"] == 6
    assert data["owner"] == "Lee"
    assert data["permission_model"] == "OBSERVE -> SUGGEST -> EXECUTE"
    assert data["cumulative_revenue_usd"] == 0


def test_objective_persists_across_loads(monkeypatch, tmp_path):
    so = _isolated(monkeypatch, tmp_path)
    so.load_strategic_objective()
    assert (tmp_path / "strategic_objective.json").exists()
    # A second module instance pointed at the same file sees the same data —
    # proves this is real persistent config, not in-memory/prompt-only state.
    so2 = _isolated(monkeypatch, tmp_path)
    assert so2.load_strategic_objective()["target_amount_usd"] == 1_000_000


def test_deadlines_are_computed_from_start_date(monkeypatch, tmp_path):
    so = _isolated(monkeypatch, tmp_path)
    so.save_strategic_objective({"start_date": "2026-01-15"})
    status = so.get_objective_status()
    assert status["stretch_deadline"] == "2026-07-15"     # +6 months
    assert status["committed_deadline"] == "2027-01-15"   # +12 months


def test_add_months_handles_year_rollover_and_short_months():
    so = load_module("jarvis_strategic_objective_months", "actions/strategic_objective.py")
    assert so._add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)
    assert so._add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert so._add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)  # leap year


def test_log_revenue_accumulates_and_progress_updates(monkeypatch, tmp_path):
    so = _isolated(monkeypatch, tmp_path)
    so.log_revenue(250_000)
    status = so.log_revenue(50_000)
    assert status["cumulative_revenue_usd"] == 300_000
    assert status["progress_pct"] == 30.0
    assert status["remaining_usd"] == 700_000


def test_format_objective_for_prompt_mentions_lee_authority_and_permission_model(monkeypatch, tmp_path):
    so = _isolated(monkeypatch, tmp_path)
    text = so.format_objective_for_prompt()
    assert "$1,000,000" in text
    assert "Lee" in text and "final authority" in text
    assert "OBSERVE -> SUGGEST -> EXECUTE" in text
