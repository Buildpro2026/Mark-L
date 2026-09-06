"""Focused tests for the 2026-09-03 Company Core Planet (Section 18) and
Personal Planet (Section 9): real, live-checked data — never fabricated —
reachable through the hierarchy dashboard/server.py actually serves."""
from fastapi.testclient import TestClient

from dashboard.server import DashboardServer
from actions import nucleus_hierarchy

_TEST_TOKEN = "test-dashboard-token-not-a-real-secret"
_AUTH_HEADERS = {"Authorization": f"Bearer {_TEST_TOKEN}"}


# ── hierarchy shape ─────────────────────────────────────────────────────

def test_company_core_is_a_real_top_level_nucleus():
    root = nucleus_hierarchy.get_hierarchy_root()
    ids = {c["id"] for c in root["children"]}
    assert "company_core" in ids


def test_personal_planet_children_have_unique_non_colliding_names():
    # Regression guard for the exact bug this session found and fixed:
    # a Personal Planet child sharing a display name with a top-level
    # domain ("Calendar", "Files", "Communications") breaks
    # find_node_by_name()'s global, first-match voice-command lookup.
    root = nucleus_hierarchy.get_hierarchy_root()
    all_names = []

    def walk(node):
        all_names.append(node["name"].strip().lower())
        for c in node.get("children", []) or []:
            walk(c)
    walk(root)
    assert len(all_names) == len(set(all_names))

    personal = next(c for c in root["children"] if c["id"] == "personal")
    child_ids = {c["id"] for c in personal["children"]}
    assert {
        "personal-email", "personal-contacts", "personal-calendar",
        "personal-documents", "personal-tasks", "personal-communications",
        "personal-files", "personal-alerts",
    } <= child_ids


def test_company_core_stars_are_the_real_infrastructure_platforms():
    root = nucleus_hierarchy.get_hierarchy_root()
    core = next(c for c in root["children"] if c["id"] == "company_core")
    star_ids = {c["id"] for c in core["children"]}
    assert "star-render" in star_ids
    assert "star-hubspot" in star_ids
    assert "star-twilio" in star_ids


# ── _module_company_core ────────────────────────────────────────────────

def test_module_company_core_reflects_real_integration_health(monkeypatch):
    server = DashboardServer()
    monkeypatch.setattr(server, "_integration_health", lambda: {
        "render": "OPERATIONAL", "hubspot": "NOT_CONFIGURED", "gmail": "AUTHENTICATED",
    })
    data = server._module_company_core()
    by_id = {s["id"]: s for s in data["stars"]}
    assert by_id["render"]["connected"] is True
    assert by_id["hubspot"]["connected"] is False
    assert by_id["hubspot"]["status"] == "NOT_CONFIGURED"
    assert by_id["gmail"]["connected"] is True


def test_module_data_dispatches_company_core(monkeypatch):
    server = DashboardServer()
    monkeypatch.setattr(server, "_integration_health", lambda: {})
    data = server._module_data("company_core")
    assert "stars" in data
    assert "summary" in data


# ── Personal Planet modules ─────────────────────────────────────────────

def test_module_personal_email_filters_to_personal_category(monkeypatch):
    from actions import google_auth, gmail_integration
    monkeypatch.setattr(google_auth, "get_credential_status", lambda: {"authorized": True})
    monkeypatch.setattr(gmail_integration, "list_messages", lambda query, max_results: {
        "ok": True,
        "messages": [
            {"id": "m1", "sender": "mom@example.com", "subject": "Dinner Friday?", "body": "Want to grab dinner?"},
            {"id": "m2", "sender": "jane@example.com", "subject": "Re: Application for Electrician role"},
        ],
    })
    server = DashboardServer()
    data = server._module_personal_email()
    assert data["configured"] is True
    ids = {m["id"] for m in data["messages"]}
    assert ids == {"m1"}  # only the personal one, not the BuildPro candidate email


def test_module_personal_contacts_honest_when_twilio_not_configured(monkeypatch):
    from actions import twilio_integration
    monkeypatch.setattr(twilio_integration, "is_configured", lambda: False)
    server = DashboardServer()
    data = server._module_personal_contacts()
    assert data["configured"] is False
    assert data["contacts"] == []


def test_module_personal_tasks_never_fabricates_a_task_list():
    server = DashboardServer()
    data = server._module_personal_tasks()
    assert data["tasks"] == []


def test_module_data_dispatches_every_personal_id(monkeypatch):
    from actions import google_auth
    monkeypatch.setattr(google_auth, "get_credential_status", lambda: {"authorized": False})
    server = DashboardServer()
    for module_id in (
        "personal-email", "personal-contacts", "personal-calendar",
        "personal-files", "personal-documents", "personal-communications",
        "personal-tasks", "personal-alerts",
    ):
        data = server._module_data(module_id)
        assert isinstance(data, dict)


# ── end-to-end through the real HTTP route ──────────────────────────────

def test_company_core_module_reachable_over_http():
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)
    response = client.get("/3d/api/module/company_core")
    assert response.status_code == 200
    assert "stars" in response.json().get("data", {})
