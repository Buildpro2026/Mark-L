"""Phase 4 Part 12 — voice-first approvals. The system prompt (shared by
both the voice loop and the chat path, since both read core/prompt.txt)
must instruct the model to resolve "approve it" to a specific task_id,
ask when ambiguous, and never claim to modify a pending task's details
(no such capability exists in agent_orchestrator).
"""
from pathlib import Path

PROMPT_PATH = Path(__file__).resolve().parents[1] / "core" / "prompt.txt"


def test_prompt_instructs_resolving_approval_to_a_specific_task():
    text = PROMPT_PATH.read_text(encoding="utf-8")
    assert "task_id" in text
    assert "approve" in text.lower()


def test_prompt_instructs_asking_when_ambiguous():
    text = PROMPT_PATH.read_text(encoding="utf-8").lower()
    assert "ask which one" in text or "ask" in text


def test_prompt_forbids_claiming_a_fake_modify_capability():
    text = PROMPT_PATH.read_text(encoding="utf-8").lower()
    assert "do not claim to have modified" in text or "not claim" in text


def test_prompt_is_shared_by_voice_and_chat_paths():
    from core.headless import ui as headless_ui
    assert headless_ui.PROMPT_PATH == PROMPT_PATH
