"""core/headless/ui_static/index.html — the Phase 3 CEO operating system
layout served at /ui. Covers the real HTTP path: unauthenticated access
is refused, login with the real token issues a working session, and the
served page actually contains the CEO/Agents/Tasks/Approvals structure
this phase built (not just that *a* page loads).
"""
from fastapi.testclient import TestClient

from core.headless import config


def _client(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "test-ui-token-not-a-real-secret")
    from core.headless.app import create_app
    app = create_app(start_background_worker=False)
    # /ui/login's session cookie is Secure (correct for the real HTTPS
    # deployment) — TestClient's default http://testserver base_url means
    # a Secure cookie would never be sent back on later requests. Forcing
    # an https base_url here isn't a workaround for a test-only quirk, it's
    # matching how this cookie actually behaves in production.
    return TestClient(app, base_url="https://testserver")


def test_ui_root_serves_the_spa_shell_without_login(monkeypatch):
    client = _client(monkeypatch)
    r = client.get("/ui")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # Served to everyone (auth happens client-side via the session cookie
    # flow) — the login screen itself must be present in the markup.
    assert "login-screen" in r.text


def test_ui_session_reports_not_authenticated_without_a_cookie(monkeypatch):
    client = _client(monkeypatch)
    r = client.get("/ui/session")
    assert r.status_code == 200
    assert r.json()["authenticated"] is False


def test_ui_login_with_wrong_token_is_refused(monkeypatch):
    client = _client(monkeypatch)
    r = client.post("/ui/login", json={"token": "not-the-real-token"})
    assert r.status_code == 401


def test_ui_login_with_real_token_issues_a_working_session(monkeypatch):
    client = _client(monkeypatch)
    r = client.post("/ui/login", json={"token": "test-ui-token-not-a-real-secret"})
    assert r.status_code == 200
    r2 = client.get("/ui/session")
    assert r2.json()["authenticated"] is True


def test_ui_api_agents_requires_a_session(monkeypatch):
    client = _client(monkeypatch)
    r = client.get("/ui/api/agents")
    assert r.status_code == 401


def test_ui_api_agents_works_after_login(monkeypatch):
    client = _client(monkeypatch)
    client.post("/ui/login", json={"token": "test-ui-token-not-a-real-secret"})
    r = client.get("/ui/api/agents")
    assert r.status_code == 200
    assert "agents" in r.json()


def test_ui_api_brief_reachable_after_login(monkeypatch, gmail_not_authorized):
    client = _client(monkeypatch)
    client.post("/ui/login", json={"token": "test-ui-token-not-a-real-secret"})
    r = client.get("/ui/api/brief")
    assert r.status_code == 200
    body = r.json()
    assert "risks" in body
    assert "daily_deal_finders" in body


def test_served_page_contains_the_ceo_operating_system_navigation(monkeypatch):
    client = _client(monkeypatch)
    html = client.get("/ui").text
    for tab in ("Home", "Command", "CEO Brief", "BuildPro", "Daily Deal Finders", "Intelligence",
                "Agents", "Tasks &amp; Approvals", "History", "Settings"):
        assert tab in html, f"missing nav tab: {tab}"
    # Real approve/reject wiring, not a placeholder — must call the actual API paths.
    assert "/ui/api/tasks/${id}/approve" in html or "/approve" in html
    assert "/ui/api/tasks/${id}/reject" in html or "/reject" in html


def test_served_page_contains_the_four_home_areas(monkeypatch):
    client = _client(monkeypatch)
    html = client.get("/ui").text
    for area in ("Today's Priorities", "Calendar", "Active Agents", "Opportunities"):
        assert area in html, f"missing home area: {area}"


def test_served_page_contains_real_settings_controls(monkeypatch):
    client = _client(monkeypatch)
    html = client.get("/ui").text
    assert "set-voice-provider" in html
    assert "set-voice-speed" in html
    assert "set-voice-volume" in html
    assert "identity-picker" in html
    assert "set-alert-sensitivity" in html


def test_served_page_does_not_use_the_old_breathing_orb_language(monkeypatch):
    # Confirms this is an actual structural change, not a palette tweak —
    # the old page's signature visual language (a pulsing orb + generic
    # "AI assistant" framing) is gone, replaced with the CEO console layout.
    client = _client(monkeypatch)
    html = client.get("/ui").text
    assert "orb-stage" not in html
    assert "breathe" not in html


def test_served_page_has_the_face_avatar_widget_wired_into_the_chat_lifecycle(monkeypatch):
    # Lee reported /ui as "no orb no face no person just a blank board" —
    # the CEO console rebuild above dropped the old 2D canvas orb and
    # nothing visual replaced it. This restores a face (reusing the same
    # rigged-glTF avatar /3d added, see dashboard/static/3d/app.js) as a
    # persistent sidebar widget, state-driven off the actual Command-tab
    # chat lifecycle (thinking while a reply is in flight, speaking once
    # it lands) since this page has no live voice session to key off.
    client = _client(monkeypatch)
    html = client.get("/ui").text
    assert "avatar-canvas" in html
    assert "window.setAvatarState" in html
    assert '"three": "/3d/assets/vendor/three.module.js"' in html
    assert "/3d/assets/vendor/GLTFLoader.js" in html
    assert "/3d/assets/models/facecap.glb" in html
    # Actually called at the right points in the chat lifecycle, not just defined.
    assert "setAvatarState('thinking')" in html
    assert "setAvatarState('speaking')" in html
    assert "setAvatarState('idle')" in html


def test_served_page_has_org_chart_and_live_session_tracker(monkeypatch):
    # Lee asked for "an org chart that shows all the agents, what they are
    # working on, and a live session tracker" — the old Agents tab was a
    # flat list with no grouping and no notion of "right now." This checks
    # the actual structure (Lee -> business -> agent tree, ticking live
    # section), not just that some HTML exists.
    client = _client(monkeypatch)
    html = client.get("/ui").text
    assert "org-tree" in html
    assert "live-now-list" in html
    assert "BUSINESS_LABELS" in html
    # The tree is genuinely built from real agent data (business/status/
    # permission_level), not hardcoded placeholder org-chart boxes.
    assert "a.business" in html
    assert "agent.permission_level" in html
    assert "runningByAgent" in html
    # Elapsed-time ticker for the live tracker — confirms it's actually
    # live (updates independent of the network poll), not a static label.
    assert "live-elapsed" in html
    assert "setInterval" in html


def test_ui_tasks_endpoint_lists_all_tasks_for_the_org_chart(monkeypatch):
    client = _client(monkeypatch)
    r = client.post("/ui/login", json={"token": "test-ui-token-not-a-real-secret"})
    assert r.status_code == 200
    r = client.get("/ui/api/tasks")
    assert r.status_code == 200
    body = r.json()
    assert "tasks" in body
    assert isinstance(body["tasks"], list)


def test_face_avatar_assets_are_reachable_through_the_full_headless_app(monkeypatch):
    # /ui is served by core.headless.app; the avatar assets it references
    # live under dashboard/static/3d/ and are only reachable because
    # dashboard/server.py's app is mounted at "/" on the same app (see
    # core/headless/app.py). Guards against that mount ever changing in a
    # way that silently 404s these paths for /ui specifically.
    client = _client(monkeypatch)
    for path in (
        "/3d/assets/vendor/GLTFLoader.js",
        "/3d/assets/vendor/KTX2Loader.js",
        "/3d/assets/libs/meshopt_decoder.module.js",
        "/3d/assets/models/facecap.glb",
    ):
        assert client.get(path).status_code == 200, path
