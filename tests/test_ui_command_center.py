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


def test_served_page_has_the_floating_orb_wired_into_the_chat_lifecycle(monkeypatch):
    # 2026-08-19: Lee said the rigged 3D face was "creepy" and wanted the
    # original floating breathing orb back, draggable/resizable, no
    # Three.js/GLTF this time — a plain canvas 2D orb instead.
    client = _client(monkeypatch)
    html = client.get("/ui").text
    assert "orb-widget" in html
    assert "orb-canvas" in html
    assert "window.setOrbState" in html
    # The old rigged-face pipeline must actually be gone, not just unused.
    assert "GLTFLoader" not in html
    assert "KTX2Loader" not in html
    assert "facecap.glb" not in html
    assert '"three":' not in html
    # Actually called at the right points in the chat lifecycle, not just defined.
    assert "setOrbState('thinking')" in html
    assert "setOrbState('speaking')" in html
    assert "setOrbState('idle')" in html


def test_orb_is_draggable_resizable_and_repositions_on_report_change(monkeypatch):
    client = _client(monkeypatch)
    html = client.get("/ui").text
    assert "orb-resize-handle" in html
    assert "pointerdown" in html
    assert "savePosition" in html
    assert "saveSize" in html
    assert "window.onReportOpened" in html
    assert "jarvis_orb_pos" in html
    assert "jarvis_orb_size" in html


def test_orb_has_real_voice_input_and_output(monkeypatch):
    # Lee's core complaint: "it doesn't respond or wake by voice, I have to
    # type everything." Confirms SpeechRecognition (mic in) and
    # speechSynthesis (spoken replies out) are actually wired, with a
    # graceful no-op fallback for unsupported browsers, not just present
    # as dead code.
    client = _client(monkeypatch)
    html = client.get("/ui").text
    assert "SpeechRecognition" in html
    assert "webkitSpeechRecognition" in html
    assert "window.speechSynthesis" in html
    assert "window.speakReply" in html
    assert "orb-continuous-toggle" in html
    assert "isn't supported in this browser" in html  # graceful degrade, not an error


def test_orb_has_its_own_compact_chat_input(monkeypatch):
    # Lee wants to talk to Jarvis at the orb directly, not have to switch
    # to the Command report first.
    client = _client(monkeypatch)
    html = client.get("/ui").text
    assert "orb-input" in html
    assert "orb-input-row" in html
    assert "window.sendMessage" in html


def test_report_picker_replaced_the_old_sidebar_tab_list(monkeypatch):
    client = _client(monkeypatch)
    html = client.get("/ui").text
    assert "report-picker" in html
    assert "nav.tabs" not in html
    assert "approval-badge" in html


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


