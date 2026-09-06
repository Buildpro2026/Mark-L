"""Proves a single logged-in browser session can reach both the normal
/ui interface and /3d — the actual navigation path the "3D Command
Center" button in index.html uses. Without this, a person logged into
/ui who clicks through to /3d would hit a 401, because /3d's auth
historically only accepted a Bearer token a plain browser navigation
never sends. Uses the full combined app (create_app()), not
DashboardServer.app in isolation, since that's what a real browser
actually talks to — /ui and /3d are two routers mounted on one app,
sharing one session.
"""
from fastapi.testclient import TestClient

from core.headless import config


def _client(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "test-ui-token-not-a-real-secret")
    from core.headless.app import create_app
    app = create_app(start_background_worker=False)
    # See test_ui_command_center.py's _client for why base_url must be https.
    return TestClient(app, base_url="https://testserver")


def test_logged_in_ui_session_can_open_3d_without_a_separate_token(monkeypatch):
    client = _client(monkeypatch)
    login = client.post("/ui/login", json={"token": "test-ui-token-not-a-real-secret"})
    assert login.status_code == 200

    # No Authorization header at all — exactly what a plain <a href="/3d">
    # click (or window.location assignment) sends: just the cookie the
    # browser already holds from logging into /ui.
    response = client.get("/3d")
    assert response.status_code == 200
    assert "jarvis-orb" in response.text


def test_3d_still_refuses_an_unauthenticated_browser(monkeypatch):
    client = _client(monkeypatch)
    # No login at all this time.
    response = client.get("/3d")
    assert response.status_code == 401


def test_3d_api_overview_reachable_via_ui_session_cookie(monkeypatch):
    client = _client(monkeypatch)
    client.post("/ui/login", json={"token": "test-ui-token-not-a-real-secret"})

    response = client.get("/3d/api/overview")
    assert response.status_code == 200
    assert "modules" in response.json()


def test_ui_shell_has_an_obvious_3d_command_center_link():
    """P1's explicit requirement: an obvious, attractive navigation
    control, not an undocumented URL a person has to be told about."""
    html = (open("core/headless/ui_static/index.html", encoding="utf-8").read())
    assert 'id="command-center-link"' in html
    assert 'href="/3d"' in html
    assert "3D Command Center" in html


def test_3d_still_accepts_the_bearer_token_for_non_browser_clients(monkeypatch):
    # The existing token-based path (used by tests/tools/API clients that
    # aren't a logged-in browser) must keep working unchanged.
    client = _client(monkeypatch)
    response = client.get("/3d", headers={"Authorization": "Bearer test-ui-token-not-a-real-secret"})
    assert response.status_code == 200
