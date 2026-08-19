"""LLM provider layer (Gemini Live, main.py). This repo has exactly one
real, wired LLM provider — Gemini Live via google.genai, used directly in
main.py. core/llm_client.py (a dead Ollama/OpenAI-compatible local-LLM
client from a prior "MARK XL" architecture, confirmed unreferenced anywhere
in the codebase) has been removed entirely, rather than repaired, per
"support only providers the repository already intends to support."

These tests confirm the one real provider never leaks its API key into any
log/UI-visible message, and that timeouts/backoff stay bounded (the bounded
-backoff behavior itself is covered in test_connection_error_reporting.py;
this file focuses specifically on key-safety).
"""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _main():
    return load_module("jarvis_main_key_safety", "main.py")


@pytest.fixture(autouse=True)
def _no_real_gemini_env(monkeypatch):
    # _get_api_key() checks core.headless.config.GEMINI_API_KEY (the real
    # GEMINI_API_KEY env var) before ever reading API_CONFIG_PATH — on a
    # dev machine with a real .env this silently short-circuited the
    # config-file tests below regardless of what fake_path contained.
    from core.headless import config as _hc
    monkeypatch.setattr(_hc, "GEMINI_API_KEY", None)


def test_llm_client_module_no_longer_exists():
    # Confirms the dead module was actually removed, not just unreferenced.
    assert not (ROOT / "core" / "llm_client.py").exists()


def test_google_genai_sends_the_api_key_as_a_header_not_a_url_query_param():
    # The actual leak vector this matters for: if the SDK ever put the key
    # in the request URL instead of a header, connection-failure exceptions
    # (which often embed the URL) could leak it into logs. Verified against
    # the installed SDK version rather than assumed.
    from google.genai import _api_client
    import inspect
    source = inspect.getsource(_api_client.BaseApiClient.__init__)
    assert "headers['x-goog-api-key']" in source or 'headers["x-goog-api-key"]' in source


def test_classify_connection_error_generic_branch_never_echoes_raw_exception_text():
    # The generic/fallback branch must only ever include the exception
    # TYPE name, never the raw message body — this is what prevents a
    # secret hypothetically embedded in some future SDK exception message
    # from ever reaching the UI log or console.
    main = _main()
    secret = "AIzaSy-should-never-appear-in-logs-abc123"
    err = ValueError(f"connection failed, debug info: key={secret}")
    msg, _ = main._classify_connection_error(err, prev_backoff=3)
    assert secret not in msg
    assert "ValueError" in msg


def test_classify_connection_error_network_branch_never_echoes_raw_exception_text():
    main = _main()
    secret = "AIzaSy-should-never-appear-in-logs-xyz789"
    err = TimeoutError(f"timed out talking to server, auth=Bearer {secret}")
    msg, _ = main._classify_connection_error(err, prev_backoff=3)
    assert secret not in msg
    assert msg.startswith("NET:")


def test_get_api_key_raises_a_fixed_message_that_never_echoes_any_key_value(tmp_path, monkeypatch):
    main = _main()
    fake_path = tmp_path / "api_keys.json"
    fake_path.write_text(json.dumps({"gemini_api_key": ""}), encoding="utf-8")
    monkeypatch.setattr(main, "API_CONFIG_PATH", fake_path)

    with pytest.raises(RuntimeError, match="API key not valid"):
        main._get_api_key()


def test_get_api_key_returns_the_key_without_transformation(tmp_path, monkeypatch):
    main = _main()
    fake_path = tmp_path / "api_keys.json"
    fake_path.write_text(json.dumps({"gemini_api_key": "a-real-looking-key-value"}), encoding="utf-8")
    monkeypatch.setattr(main, "API_CONFIG_PATH", fake_path)

    assert main._get_api_key() == "a-real-looking-key-value"


def test_get_api_key_missing_config_file_raises_cleanly(tmp_path, monkeypatch):
    main = _main()
    monkeypatch.setattr(main, "API_CONFIG_PATH", tmp_path / "does_not_exist.json")

    with pytest.raises(RuntimeError, match="API key not valid"):
        main._get_api_key()
