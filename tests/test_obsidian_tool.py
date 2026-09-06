"""Phase 4 Part 18 — Obsidian wired as a real, safe LLM tool. Reads are
always safe; writes require approved=true, and write_note additionally
requires overwrite=true to replace an existing note, mirroring every
other consequential-action tool in this codebase.
"""
import asyncio
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_live(name="jarvis_main_obsidian_tool"):
    main = load_module(name, "main.py")
    live = object.__new__(main.JarvisLive)
    return main, live


def _ui_stub():
    return type("UIStub", (), {
        "muted": False,
        "set_state": lambda self, s: None,
        "write_log": lambda self, m: None,
    })()


def _live(main):
    live = object.__new__(main.JarvisLive)
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    return live


def _make_fc(**args):
    return type("FC", (), {"id": "call-1", "name": "obsidian", "args": args})()


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    from actions import audit_log
    monkeypatch.setattr(audit_log, "DB_PATH", tmp_path / "test_audit.db")


def test_tool_declared_for_gemini():
    main, _ = _new_live()
    names = [t["name"] for t in main.TOOL_DECLARATIONS]
    assert "obsidian" in names


def test_status_reports_not_configured_honestly(monkeypatch):
    from core.headless import obsidian as obsidian_module
    monkeypatch.setattr(obsidian_module.config, "OBSIDIAN_VAULT_PATH", None)
    main, live = _new_live()
    l = _live(main)
    response = _run(l._execute_tool(_make_fc(action="status")))
    assert "no obsidian vault configured" in response.response["result"].lower()


def test_read_only_actions_work_against_a_real_temp_vault(tmp_path, monkeypatch):
    from core.headless import obsidian as obsidian_module
    (tmp_path / "Notes").mkdir()
    (tmp_path / "Notes" / "goals.md").write_text("# Goals\nHit the revenue target.", encoding="utf-8")
    monkeypatch.setattr(obsidian_module.config, "OBSIDIAN_VAULT_PATH", str(tmp_path))

    main, live = _new_live()
    l = _live(main)

    status_resp = _run(l._execute_tool(_make_fc(action="status")))
    assert "vault configured" in status_resp.response["result"].lower()

    list_resp = _run(l._execute_tool(_make_fc(action="list_notes")))
    assert "goals.md" in list_resp.response["result"]

    read_resp = _run(l._execute_tool(_make_fc(action="read_note", path="Notes/goals.md")))
    assert "revenue target" in read_resp.response["result"]

    search_resp = _run(l._execute_tool(_make_fc(action="search_notes", query="revenue")))
    assert "goals.md" in search_resp.response["result"]


def test_write_note_requires_approval(tmp_path, monkeypatch):
    from core.headless import obsidian as obsidian_module
    monkeypatch.setattr(obsidian_module.config, "OBSIDIAN_VAULT_PATH", str(tmp_path))
    main, live = _new_live()
    l = _live(main)

    response = _run(l._execute_tool(_make_fc(action="write_note", path="test.md", content="hello")))
    result = response.response["result"].lower()
    assert "couldn't save" in result or "not_approved" in result or "requires" in result
    assert not (tmp_path / "test.md").exists()


def test_write_note_with_approval_creates_the_file(tmp_path, monkeypatch):
    from core.headless import obsidian as obsidian_module
    monkeypatch.setattr(obsidian_module.config, "OBSIDIAN_VAULT_PATH", str(tmp_path))
    main, live = _new_live()
    l = _live(main)

    response = _run(l._execute_tool(_make_fc(action="write_note", path="test.md", content="hello", approved=True)))
    assert "saved" in response.response["result"].lower()
    assert (tmp_path / "test.md").read_text(encoding="utf-8") == "hello"


def test_write_note_never_silently_overwrites(tmp_path, monkeypatch):
    from core.headless import obsidian as obsidian_module
    monkeypatch.setattr(obsidian_module.config, "OBSIDIAN_VAULT_PATH", str(tmp_path))
    (tmp_path / "existing.md").write_text("original", encoding="utf-8")
    main, live = _new_live()
    l = _live(main)

    response = _run(l._execute_tool(_make_fc(action="write_note", path="existing.md", content="replaced", approved=True)))
    assert "couldn't save" in response.response["result"].lower()
    assert (tmp_path / "existing.md").read_text(encoding="utf-8") == "original"


def test_record_decision_requires_approval(tmp_path, monkeypatch):
    from core.headless import obsidian as obsidian_module
    monkeypatch.setattr(obsidian_module.config, "OBSIDIAN_VAULT_PATH", str(tmp_path))
    main, live = _new_live()
    l = _live(main)

    response = _run(l._execute_tool(_make_fc(action="record_decision", title="Test Decision", content="details")))
    assert "approval" in response.response["result"].lower()
    assert not list((tmp_path / "Jarvis" / "Decisions").glob("*.md")) if (tmp_path / "Jarvis" / "Decisions").exists() else True


def test_record_decision_with_approval_creates_a_timestamped_entry(tmp_path, monkeypatch):
    from core.headless import obsidian as obsidian_module
    monkeypatch.setattr(obsidian_module.config, "OBSIDIAN_VAULT_PATH", str(tmp_path))
    main, live = _new_live()
    l = _live(main)

    response = _run(l._execute_tool(_make_fc(action="record_decision", title="Test Decision", content="details", approved=True)))
    assert "recorded" in response.response["result"].lower()
    entries = list((tmp_path / "Jarvis" / "Decisions").glob("*.md"))
    assert len(entries) == 1
    assert "Test Decision" in entries[0].read_text(encoding="utf-8")


def test_unknown_action_reports_clearly(tmp_path, monkeypatch):
    from core.headless import obsidian as obsidian_module
    monkeypatch.setattr(obsidian_module.config, "OBSIDIAN_VAULT_PATH", str(tmp_path))
    main, live = _new_live()
    l = _live(main)
    response = _run(l._execute_tool(_make_fc(action="not_a_real_action")))
    assert "unknown obsidian action" in response.response["result"].lower()


def test_format_for_prompt_concatenates_notes_with_headers(tmp_path, monkeypatch):
    from core.headless.obsidian import ObsidianVault
    from core.headless import obsidian as obsidian_module
    (tmp_path / "Notes").mkdir()
    (tmp_path / "Notes" / "goals.md").write_text("Hit the revenue target.", encoding="utf-8")
    (tmp_path / "Notes" / "identity.md").write_text("JARVIS is the operating assistant.", encoding="utf-8")
    monkeypatch.setattr(obsidian_module.config, "OBSIDIAN_VAULT_PATH", str(tmp_path))

    result = ObsidianVault().format_for_prompt()
    assert "revenue target" in result
    assert "operating assistant" in result
    assert "Notes/goals.md" in result


def test_format_for_prompt_respects_max_chars(tmp_path, monkeypatch):
    from core.headless.obsidian import ObsidianVault
    from core.headless import obsidian as obsidian_module
    (tmp_path / "big.md").write_text("x" * 10_000, encoding="utf-8")
    monkeypatch.setattr(obsidian_module.config, "OBSIDIAN_VAULT_PATH", str(tmp_path))

    result = ObsidianVault().format_for_prompt(max_chars=500)
    assert len(result) <= 600  # header + truncated content, generous slack


def test_format_for_prompt_with_query_uses_search_ranking(tmp_path, monkeypatch):
    from core.headless.obsidian import ObsidianVault
    from core.headless import obsidian as obsidian_module
    (tmp_path / "goals.md").write_text("Hit the revenue target.", encoding="utf-8")
    (tmp_path / "unrelated.md").write_text("Nothing to do with the query.", encoding="utf-8")
    monkeypatch.setattr(obsidian_module.config, "OBSIDIAN_VAULT_PATH", str(tmp_path))

    result = ObsidianVault().format_for_prompt(query="revenue")
    assert "revenue target" in result
    assert "unrelated" not in result.lower()


def test_format_for_prompt_returns_empty_string_when_unconfigured(monkeypatch):
    from core.headless.obsidian import ObsidianVault
    from core.headless import obsidian as obsidian_module
    monkeypatch.setattr(obsidian_module.config, "OBSIDIAN_VAULT_PATH", "")

    assert ObsidianVault().format_for_prompt() == ""
