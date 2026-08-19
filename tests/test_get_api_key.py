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


@pytest.fixture(autouse=True)
def _no_real_gemini_env(monkeypatch):
    # _get_api_key() checks core.headless.config.GEMINI_API_KEY (sourced
    # from the real GEMINI_API_KEY env var) BEFORE ever reading
    # API_CONFIG_PATH — on a dev machine with a real .env this made every
    # test in this file silently exercise the env-var branch instead of
    # the config-file branch under test, regardless of what config_dict
    # said. Force the env-var branch off so these tests are hermetic.
    from core.headless import config as _hc
    monkeypatch.setattr(_hc, "GEMINI_API_KEY", None)


def _main_with_config(tmp_path, config_dict):
    main = load_module("jarvis_main_apikey", "main.py")
    cfg_path = tmp_path / "api_keys.json"
    cfg_path.write_text(json.dumps(config_dict), encoding="utf-8")
    main.API_CONFIG_PATH = cfg_path
    return main


def test_get_api_key_returns_the_configured_key(tmp_path):
    main = _main_with_config(tmp_path, {"gemini_api_key": "real-key-value"})
    assert main._get_api_key() == "real-key-value"


def test_get_api_key_raises_a_recognizable_error_when_field_missing(tmp_path):
    main = _main_with_config(tmp_path, {"os_system": "windows"})   # no gemini_api_key at all
    try:
        main._get_api_key()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        # Must match run()'s existing "API key not valid" recognition string,
        # so a missing key routes into the graceful reconfig-prompt path
        # instead of an unhandled-error infinite reconnect loop.
        assert "API key not valid" in str(e)


def test_get_api_key_raises_when_field_is_empty_string(tmp_path):
    main = _main_with_config(tmp_path, {"gemini_api_key": ""})
    try:
        main._get_api_key()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "API key not valid" in str(e)


def test_get_api_key_raises_when_config_file_is_missing_entirely(tmp_path):
    main = load_module("jarvis_main_apikey_missing_file", "main.py")
    main.API_CONFIG_PATH = tmp_path / "does_not_exist.json"
    try:
        main._get_api_key()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "API key not valid" in str(e)


def test_get_api_key_raises_when_config_file_is_corrupt_json(tmp_path):
    cfg_path = tmp_path / "api_keys.json"
    cfg_path.write_text("{not valid json", encoding="utf-8")
    main = load_module("jarvis_main_apikey_corrupt", "main.py")
    main.API_CONFIG_PATH = cfg_path
    try:
        main._get_api_key()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "API key not valid" in str(e)
