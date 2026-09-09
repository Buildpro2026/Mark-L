"""Gemini Live voice model migration (Lee's instruction, 2026-09-09):
gemini-2.5-flash-native-audio-preview-12-2025 -> gemini-3.1-flash-live-preview.

Two call sites use this exact model string for a live audio session:
main.py's JarvisLive voice/tools loop, and screen_processor.py's vision
live session. Both are migrated together so the codebase is never left in
a half-migrated state with two different Live model versions in flight.
This does not touch Ollama, the research fallback chain, or any other
model reference (main.py's session-summary call uses a plain, non-live
gemini-2.5-flash generate_content call and is intentionally untouched —
it is text summarization, not the Live voice session)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_main_py_live_model_migrated_to_gemini_3_1_flash_live_preview():
    import main
    assert main.LIVE_MODEL == "models/gemini-3.1-flash-live-preview"


def test_screen_processor_live_model_migrated_too():
    from actions import screen_processor as sp
    assert sp._LIVE_MODEL == "models/gemini-3.1-flash-live-preview"


def test_main_py_session_summary_model_is_untouched():
    # Not part of the voice migration — a plain text summarization call,
    # not a Live session. Confirms the migration was scoped correctly.
    import inspect
    import main
    src = inspect.getsource(main.JarvisLive._save_session_summary)
    assert 'model="gemini-2.5-flash"' in src
