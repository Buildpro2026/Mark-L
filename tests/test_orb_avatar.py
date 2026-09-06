"""JARVIS orb avatar — real, locally-generated video (SadTalker for
idle/listening/thinking/tool/error motion, MuseTalk for audio-driven
speaking) swapped by FaceRenderer based on currentState. Covers the served
markup/script (no photo, no SVG-drawn face, no Three.js/GLTF) and the
/ui/api/avatar/asset/* endpoint that serves the pre-generated clips.

Generation itself (SadTalker/MuseTalk, run locally and offline) is out of
scope for this suite — these assets are static files by the time the app
serves them, exactly like any other file in ui_static/.
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


def test_served_page_uses_a_video_element_not_svg_or_photo(monkeypatch):
    client = _client(monkeypatch)
    html = client.get("/ui").text
    assert '<video id="orb-face-video"' in html
    # The rejected approaches must actually be gone, not just unused.
    assert "data:image/jpeg;base64" not in html
    assert "orb-face-svg" not in html
    assert "GLTFLoader" not in html
    assert '"three":' not in html


def test_face_renderer_maps_states_to_the_two_generated_assets(monkeypatch):
    client = _client(monkeypatch)
    html = client.get("/ui").text
    assert "/ui/api/avatar/asset/idle_loop.mp4" in html
    assert "/ui/api/avatar/asset/speaking_sample.mp4" in html
    # idle/listening/thinking/tool/error/success/offline/interrupted all
    # share the idle loop today (ring/halo color+speed carries the
    # per-state distinction); only speaking swaps the video source. Any
    # state without its own entry in AVATAR_CONFIG.assets — which is every
    # state except speaking, today — falls back to idle rather than erroring,
    # which is what makes a future full-body asset a config change instead
    # of a per-state rewrite.
    assert "function assetForState(state) {" in html
    assert "return AVATAR_CONFIG.assets[state] || AVATAR_CONFIG.assets.idle;" in html


def test_avatar_asset_requires_a_session(monkeypatch):
    client = _client(monkeypatch)
    r = client.get("/ui/api/avatar/asset/idle_loop.mp4")
    assert r.status_code == 401


def test_avatar_asset_served_after_login(monkeypatch):
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


def test_avatar_is_not_framed_in_a_circular_orb(monkeypatch):
    # 2026-08-29: Lee's explicit direction — the avatar itself must never
    # read as a face floating inside an orb. The circular ring/halo/canvas-
    # sphere framing is gone (not just visually hidden by chance); the
    # avatar's default shape is a rounded rectangle, not a circle; and the
    # panel is a docked layout element, not a free-floating widget.
    client = _client(monkeypatch)
    html = client.get("/ui").text
    assert 'id="orb-face-ring"' not in html
    assert 'id="orb-face-halo"' not in html
    assert '--avatar-aspect: 3 / 4; --avatar-radius: 10px;' in html
    assert "hud-corner" in html  # the instrument-panel framing that replaced the ring


def test_avatar_container_shape_is_config_driven_not_hard_coded(monkeypatch):
    # The future full-body asset needs an even taller container than
    # today's placeholder. Swapping AVATAR_CONFIG.shape must be enough —
    # no markup/CSS rewrite — so the container's aspect ratio and corner
    # radius must come from CSS custom properties JS sets from that one
    # config object, not literal values baked into the stylesheet. Default
    # shape is a rounded rectangle, not a circle — see
    # test_avatar_is_not_framed_in_a_circular_orb for why that matters.
    client = _client(monkeypatch)
    html = client.get("/ui").text
    assert "aspect-ratio: var(--avatar-aspect)" in html
    assert "border-radius: var(--avatar-radius)" in html
    assert 'shape: { aspect: "3 / 4", radius: "10px" }' in html
    assert 'wrap.style.setProperty("--avatar-aspect", AVATAR_CONFIG.shape.aspect);' in html
    assert 'wrap.style.setProperty("--avatar-radius", AVATAR_CONFIG.shape.radius);' in html


def test_offline_state_is_driven_by_the_real_health_check_not_a_fake_signal(monkeypatch):
    # Reuses the exact /health call the Command page's connection dot
    # already made — not a second, independently-invented offline probe.
    client = _client(monkeypatch)
    html = client.get("/ui").text
    assert "if (window.setOrbReason) window.setOrbReason('offline', false);" in html
    assert "if (window.setOrbReason) window.setOrbReason('offline', true);" in html


def test_offline_outranks_every_other_state(monkeypatch):
    # Nothing else the orb could show means anything while the backend
    # itself is actually unreachable.
    client = _client(monkeypatch)
    html = client.get("/ui").text
    priority_line = 'const PRIORITY = ["offline", "error", "speaking", "tool", "success", "thinking", "listening"];'
    assert priority_line in html


def test_success_is_driven_by_a_real_tool_end_ok_frame_and_self_clears(monkeypatch):
    # Only ever fires from resolveToolChip's real `ok` argument (itself
    # sourced from a genuine tool_end SSE frame — see
    # test_chat_tool_activity_stream.py) — never a fabricated timer, and
    # brief/self-clearing so it never gets stuck showing "success" forever.
    client = _client(monkeypatch)
    html = client.get("/ui").text
    assert "if (ok && window.setOrbReason) {" in html
    assert "window.setOrbReason('success', true);" in html
    assert "window._orbSuccessFallback = setTimeout(() => window.setOrbReason('success', false), 1200);" in html
