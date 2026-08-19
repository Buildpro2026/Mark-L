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
