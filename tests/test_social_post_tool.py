"""main.py's "social_post" voice tool — preview/gated-publish wiring into
actions/buffer_integration.py. Never makes a live Buffer call: every
buffer_integration.py function is monkeypatched.

'preview' always calls publish_to_buffer(post, approved=False) — never
publishes, always safe. 'publish' calls with approved=True. Both route
through the same underlying validation (text/channel/mode/platform-limit/
duplicate), so a caller can trust that whatever 'preview' showed is
exactly what 'publish' will attempt.
"""
import asyncio
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_live(name="jarvis_main_social"):
    main = load_module(name, "main.py")
    live = object.__new__(main.JarvisLive)
    return main, live


def _ui_stub():
    return type("UIStub", (), {
        "muted": False,
        "set_state": lambda self, s: None,
        "write_log": lambda self, m: None,
    })()


def _make_fc(**args):
    return type("FC", (), {"id": "call-1", "name": "social_post", "args": args})()


def _run(coro):
    return asyncio.run(coro)


def _live(main):
    live = object.__new__(main.JarvisLive)
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    return live


def test_tool_declared_for_gemini():
    main, _ = _new_live()
    names = [t["name"] for t in main.TOOL_DECLARATIONS]
    assert "social_post" in names


# ── status ────────────────────────────────────────────────────────────

def test_status_reports_configured(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.buffer_integration, "verify_buffer",
                         lambda: {"configured": True, "status": "VERIFIED"})

    response = _run(live._execute_tool(_make_fc(action="status")))
    assert "buffer: verified" in response.response["result"].lower()


def test_status_reports_not_configured(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.buffer_integration, "verify_buffer",
                         lambda: {"configured": False, "status": "NOT CONFIGURED"})

    response = _run(live._execute_tool(_make_fc(action="status")))
    assert "isn't configured" in response.response["result"].lower()


# ── preview — never publishes ────────────────────────────────────────

def test_preview_without_text_is_refused(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    called = {"n": 0}
    monkeypatch.setattr(main.buffer_integration, "publish_to_buffer",
                         lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    response = _run(live._execute_tool(_make_fc(action="preview")))
    assert called["n"] == 0
    assert "content" in response.response["result"].lower()


def test_preview_calls_with_approved_false(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    captured = {}

    def _fake_publish(post, approved=False):
        captured.update(post=post, approved=approved)
        return {"published": False, "status": "PREVIEW", "preview": {
            "channel_id": "c1", "service": "linkedin", "text": "Hello world", "mode": "addToQueue",
        }}

    monkeypatch.setattr(main.buffer_integration, "publish_to_buffer", _fake_publish)

    response = _run(live._execute_tool(_make_fc(action="preview", text="Hello world", service="linkedin")))

    assert captured["approved"] is False
    assert captured["post"]["text"] == "Hello world"
    assert captured["post"]["service"] == "linkedin"
    result = response.response["result"].lower()
    assert "preview" in result
    assert "hello world" in result
    assert "linkedin" in result


def test_preview_surfaces_a_validation_error(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.buffer_integration, "publish_to_buffer", lambda post, approved=False: {
        "published": False, "status": "ERROR:duplicate post", "detail": "already posted",
    })

    response = _run(live._execute_tool(_make_fc(action="preview", text="hi", channel_id="c1")))
    result = response.response["result"].lower()
    assert "couldn't preview the post" in result
    assert "duplicate post" in result


# ── publish — the gated, irreversible action ─────────────────────────

def test_publish_without_text_is_refused_and_never_touches_buffer(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    called = {"n": 0}
    monkeypatch.setattr(main.buffer_integration, "publish_to_buffer",
                         lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    response = _run(live._execute_tool(_make_fc(action="publish")))
    assert called["n"] == 0


def test_publish_calls_with_approved_true(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    captured = {}

    def _fake_publish(post, approved=False):
        captured.update(post=post, approved=approved)
        return {"published": True, "status": "PUBLISHED", "buffer_id": "p1"}

    monkeypatch.setattr(main.buffer_integration, "publish_to_buffer", _fake_publish)

    response = _run(live._execute_tool(_make_fc(action="publish", text="Hello world", channel_id="c1")))

    assert captured["approved"] is True
    assert "posted" in response.response["result"].lower()
    assert "p1" in response.response["result"]


def test_publish_forwards_optional_fields(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    captured = {}

    def _fake_publish(post, approved=False):
        captured.update(post)
        return {"published": True, "status": "PUBLISHED", "buffer_id": "p1"}

    monkeypatch.setattr(main.buffer_integration, "publish_to_buffer", _fake_publish)

    _run(live._execute_tool(_make_fc(
        action="publish", text="Check this out", channel_id="c1",
        link_url="https://example.com", image_url="https://example.com/i.jpg",
        mode="shareNow", allow_duplicate=True,
    )))

    assert captured["link_url"] == "https://example.com"
    assert captured["image_url"] == "https://example.com/i.jpg"
    assert captured["mode"] == "shareNow"
    assert captured["allow_duplicate"] is True


def test_publish_failure_is_reported_not_fabricated_as_success(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.buffer_integration, "publish_to_buffer", lambda post, approved=False: {
        "published": False, "status": "ERROR:missing channel", "detail": "Provide either channel_id or service.",
    })

    response = _run(live._execute_tool(_make_fc(action="publish", text="Hello")))
    result = response.response["result"].lower()
    assert "couldn't publish the post" in result
    assert "missing channel" in result


def test_unknown_social_post_action_reports_clearly():
    main, _ = _new_live()
    live = _live(main)

    response = _run(live._execute_tool(_make_fc(action="delete_everything")))
    assert "unknown social_post action" in response.response["result"].lower()
