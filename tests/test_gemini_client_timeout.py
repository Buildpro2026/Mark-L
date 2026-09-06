"""core/headless/gemini_client.py — the shared, timeout-bounded
google-genai client constructor.

Phase 3 finding: genai.Client(api_key=...) alone sets NO request timeout.
The SDK's own httpx client args default to timeout=None, which in httpx
means block forever, not "use a sane default." A stalled connection to
Gemini's REST API could hang a request indefinitely, and since chat
turns are processed one at a time (core/headless/dashboard_bridge.py's
queue), a single hung call stalled every later message behind it too.
This is confirmed to be the actual explanation for reported multi-minute
response times, not slow reasoning — see the Phase 3 report.

These tests confirm get_client() actually sets a bounded httpx timeout
(not just that the function exists), and that every real call site in
the codebase routes through it instead of calling genai.Client() itself.
"""
import ast
from pathlib import Path

from core.headless.gemini_client import get_client, DEFAULT_TIMEOUT_MS

ROOT = Path(__file__).resolve().parents[1]

# Every file that legitimately still constructs a raw genai.Client() —
# both are Gemini Live (websocket) sessions, a different protocol with
# its own reconnect/backoff handling, not the REST timeout this module
# fixes. Any other file gaining a raw genai.Client( call is a regression.
_ALLOWED_RAW_CLIENT_FILES = {
    ROOT / "main.py",
    ROOT / "actions" / "screen_processor.py",
}


def test_get_client_sets_a_bounded_httpx_timeout():
    client = get_client("fake-key-not-real")
    # google-genai stores the resolved HttpOptions on the client; the
    # important thing is it's not None (infinite) and matches what we asked for.
    timeout = client._api_client._http_options.timeout
    assert timeout is not None
    assert timeout == DEFAULT_TIMEOUT_MS


def test_get_client_respects_a_custom_timeout():
    client = get_client("fake-key-not-real", timeout_ms=5_000)
    assert client._api_client._http_options.timeout == 5_000


def test_no_new_unbounded_genai_client_call_sites():
    """Every genai.Client(/_genai.Client( call site in the repo (outside
    gemini_client.py itself) must be one of the two known, deliberately
    unwrapped Gemini Live sessions. A new plain call site would silently
    reintroduce the unbounded-hang bug this module exists to close."""
    offenders = []
    for py_file in ROOT.rglob("*.py"):
        if any(part in (".venv", "__pycache__", "tests") for part in py_file.parts):
            continue
        if py_file == ROOT / "core" / "headless" / "gemini_client.py":
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "Client" and isinstance(node.func.value, ast.Name) and node.func.value.id in ("genai", "_genai"):
                    offenders.append(py_file)
    unexpected = set(offenders) - _ALLOWED_RAW_CLIENT_FILES
    assert not unexpected, f"New unbounded genai.Client() call site(s) found, route through gemini_client.get_client() instead: {unexpected}"
