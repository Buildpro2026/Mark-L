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
    # idle/listening/thinking/tool/error/interrupted all share the idle
    # loop today (ring/halo color+speed carries the per-state distinction);
    # only speaking swaps the video source.
    assert 'return state === "speaking" ? AVATAR_ASSETS.speaking : AVATAR_ASSETS.idle;' in html


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
