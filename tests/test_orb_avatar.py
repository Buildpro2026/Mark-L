"""JARVIS's presence: the orb.

2026-09-09, Lee's explicit direction, reversing the 2026-08-29/31 avatar-
video phase this suite used to cover: JARVIS is drawn as an abstract AI
orb again (OrbRenderer, canvas 2D, in the orb script block of
core/headless/ui_static/index.html) — never a rendering of a human face.
Covers the served markup/script (no video element, no photo, no SVG-drawn
face, no Three.js/GLTF) and confirms the dormant /ui/api/avatar/asset/*
endpoint (unused by the page now, left in place rather than torn out) still
behaves correctly on its own terms.
"""
from fastapi.testclient import TestClient

from core.headless import config, ui


def _client(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "test-ui-token-not-a-real-secret")
    from core.headless.app import create_app
    app = create_app(start_background_worker=False)
    return TestClient(app, base_url="https://testserver")


def _logged_in_client(monkeypatch):
    client = _client(monkeypatch)
    r = client.post("/ui/login", json={"token": "test-ui-token-not-a-real-secret"})
    assert r.status_code == 200
    return client


def test_served_page_uses_a_canvas_not_a_video_svg_or_photo(monkeypatch):
    client = _client(monkeypatch)
    html = client.get("/ui").text
    assert '<canvas id="orb-face-canvas">' in html
    # The rejected approaches must actually be gone, not just unused.
    assert '<video id="orb-face-video"' not in html
    assert "data:image/jpeg;base64" not in html
    assert "orb-face-svg" not in html
    assert "GLTFLoader" not in html
    assert '"three":' not in html


def test_orb_renderer_draws_a_layered_presence_not_a_flat_circle(monkeypatch):
    client = _client(monkeypatch)
    html = client.get("/ui").text
    # Real layered rendering, not a single filled circle: an outer bloom, a
    # lit core with a highlight-offset gradient (depth), rotating rings,
    # and an orbiting particle field — checked as code, not prose.
    assert "const OrbRenderer = (() => {" in html
    assert 'const canvasEl = document.getElementById("orb-face-canvas");' in html
    assert "ctx.createRadialGradient(" in html
    assert "ctx.shadowBlur" in html
    assert "for (let i = 0; i < energy.rings; i++) {" in html
    assert "ensureParticles(energy.particles);" in html


def test_every_required_state_has_its_own_energy_recipe(monkeypatch):
    client = _client(monkeypatch)
    html = client.get("/ui").text
    for state in ("idle", "listening", "thinking", "tool", "speaking", "interrupted",
                  "approval_required", "success", "warning", "error", "degraded", "offline"):
        assert f"{state}:" in html, f"OrbRenderer has no ENERGY entry for {state!r}"


def test_avatar_asset_requires_a_session(monkeypatch):
    client = _client(monkeypatch)
    r = client.get("/ui/api/avatar/asset/idle_loop.mp4")
    assert r.status_code == 401


def test_avatar_asset_served_after_login(monkeypatch):
    # The endpoint itself is unused by the served page now (see
    # test_served_page_uses_a_canvas_not_a_video_svg_or_photo), but it is
    # untouched, dormant infrastructure, not deleted — still expected to
    # behave correctly on its own terms.
    client = _logged_in_client(monkeypatch)
    for name in ("idle_loop.mp4", "speaking_sample.mp4"):
        r = client.get(f"/ui/api/avatar/asset/{name}")
        assert r.status_code == 200, name
        assert r.headers["content-type"] == "video/mp4"
        assert len(r.content) > 0


def test_avatar_asset_rejects_names_outside_the_allowlist(monkeypatch):
    # Guards against path traversal / serving arbitrary files from
    # ui_static/avatar even though FastAPI path params can't contain "/".
    client = _logged_in_client(monkeypatch)
    r = client.get("/ui/api/avatar/asset/does_not_exist.mp4")
    assert r.status_code == 404


def test_avatar_asset_files_actually_exist_on_disk():
    for name in ("idle_loop.mp4", "speaking_sample.mp4"):
        path = ui.AVATAR_DIR / name
        assert path.is_file(), f"missing {path}"
        assert path.stat().st_size > 0


def test_orb_is_not_a_human_avatar(monkeypatch):
    # The explicit product requirement this whole rebuild exists to satisfy:
    # never a rendering of a human face, never a photo. OrbRenderer draws
    # geometry (gradients/rings/particles) only — no asset path a human
    # face clip could still be flowing through.
    client = _client(monkeypatch)
    html = client.get("/ui").text
    assert '<video id="orb-face-video"' not in html
    assert "idle_loop.mp4" not in html
    assert "speaking_sample.mp4" not in html


def test_avatar_container_shape_is_still_config_driven_on_home(monkeypatch):
    # Home's circular presentation (the ring/halo/ticks/segments treatment
    # built for the earlier "Aegis Command Deck" pass) is preserved
    # unchanged by the orb rebuild — this file doesn't own that CSS, it
    # only needs the container's own default shape to still be a real CSS
    # custom property (not a hard-coded pixel size baked into the
    # stylesheet), same contract as before.
    client = _client(monkeypatch)
    html = client.get("/ui").text
    assert "aspect-ratio: var(--avatar-aspect)" in html
    assert "border-radius: var(--avatar-radius)" in html
    assert "--avatar-aspect: 3 / 4; --avatar-radius: 10px;" in html
    assert "--avatar-aspect: 1 / 1 !important; --avatar-radius: 50% !important;" in html


def test_offline_state_is_driven_by_the_real_health_check_not_a_fake_signal(monkeypatch):
    # Reuses the exact /health call the Command page's connection dot
    # already made — not a second, independently-invented offline probe.
    client = _client(monkeypatch)
    html = client.get("/ui").text
    assert "window.setOrbReason('offline', false);" in html
    assert "window.setOrbReason('offline', true);" in html


def test_degraded_state_is_driven_by_the_real_health_status_field(monkeypatch):
    # 2026-09-09 addition: the same /health payload already carried a real
    # status field ("degraded" when the database is unreachable) that
    # nothing on this page read before.
    client = _client(monkeypatch)
    html = client.get("/ui").text
    assert "window.setOrbReason('degraded', h.status === 'degraded');" in html


def test_offline_outranks_every_other_state(monkeypatch):
    # Nothing else the orb could show means anything while the backend
    # itself is actually unreachable.
    client = _client(monkeypatch)
    html = client.get("/ui").text
    priority_line = ('const PRIORITY = ["offline", "degraded", "error", "speaking", "tool", '
                     '"warning", "success", "thinking", "listening", "approval_required"];')
    assert priority_line in html


def test_success_is_driven_by_a_real_tool_end_ok_frame_and_self_clears(monkeypatch):
    # Only ever fires from resolveToolChip's real `ok` argument (itself
    # sourced from a genuine tool_end SSE frame — see
    # test_chat_tool_activity_stream.py) — never a fabricated timer, and
    # brief/self-clearing so it never gets stuck showing "success" forever.
    client = _client(monkeypatch)
    html = client.get("/ui").text
    assert "window.setOrbReason('success', true);" in html
    assert "window._orbSuccessFallback = setTimeout(() => window.setOrbReason('success', false), 1200);" in html


def test_a_failed_tool_call_gets_a_distinct_warning_not_silence(monkeypatch):
    # 2026-09-09: previously ok:false produced no cue at all — the orb just
    # fell back to whatever PRIORITY reason was next. "warning", not
    # "error": a tool reporting its own failure is diagnostic, not the same
    # severity as an unhandled chat-level exception.
    client = _client(monkeypatch)
    html = client.get("/ui").text
    assert "window.setOrbReason('warning', true);" in html
    assert "window._orbWarningFallback = setTimeout(() => window.setOrbReason('warning', false), 2200);" in html
