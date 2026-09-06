"""JARVIS Brain Nucleus (Phase 2 Step 2) — dashboard/server.py's
_module_knowledge() is a thin wrapper over the existing, already-tested
core.headless.obsidian.ObsidianVault (list_notes/search_notes/read_note).
No new retrieval system, no embeddings — these tests confirm the real
knowledge/JARVIS Brain/ vault is actually reachable through the existing
/3d/api/module/{module_id} route, distinct from the generic Files Nucleus.
"""
from pathlib import Path

from fastapi.testclient import TestClient

from dashboard.server import DashboardServer
from core.headless import config as headless_config

# Matches conftest.py's _dashboard_api_token autouse fixture.
_TEST_TOKEN = "test-dashboard-token-not-a-real-secret"
_AUTH_HEADERS = {"Authorization": f"Bearer {_TEST_TOKEN}"}

REAL_VAULT = Path(__file__).resolve().parents[1] / "knowledge" / "JARVIS Brain"


def test_knowledge_nucleus_appears_in_overview():
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    response = client.get("/3d/api/overview")

    assert response.status_code == 200
    payload = response.json()
    assert any(m["id"] == "knowledge" for m in payload["modules"])
    hierarchy_ids = [c["id"] for c in payload["hierarchy"]["children"]]
    assert "knowledge" in hierarchy_ids
    knowledge_node = next(c for c in payload["hierarchy"]["children"] if c["id"] == "knowledge")
    assert knowledge_node["name"] == "JARVIS Brain"


def test_knowledge_module_lists_the_real_brain_documents():
    # No JARVIS_OBSIDIAN_VAULT_PATH override here — this exercises the
    # actual default vault (knowledge/JARVIS Brain/ in this repo), not a
    # temp fixture, to prove the real 20+ documents are genuinely reachable.
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    response = client.get("/3d/api/module/knowledge")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["configured"] is True
    real_notes_on_disk = sorted(str(p.relative_to(REAL_VAULT).as_posix()) for p in REAL_VAULT.rglob("*.md"))
    assert data["notes"] == real_notes_on_disk
    assert len(data["notes"]) == 22
    assert any("COMMAND CENTER" in n for n in data["notes"])
    assert str(data["summary"]) == f"{len(data['notes'])} note(s) in the JARVIS Brain."


def test_knowledge_module_search_finds_a_real_note():
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    response = client.get("/3d/api/module/knowledge", params={"query": "COMMAND CENTER"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["configured"] is True
    assert data["query"] == "COMMAND CENTER"
    assert data["results"], "expected at least one real match"
    assert any("COMMAND CENTER" in r["path"] for r in data["results"])
    assert data["summary"].startswith(str(len(data["results"])))


def test_knowledge_module_search_with_no_matches_is_honest_not_fabricated():
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    response = client.get("/3d/api/module/knowledge", params={"query": "zzz_no_such_topic_zzz"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["results"] == []
    assert data["summary"] == '0 note(s) match "zzz_no_such_topic_zzz".'


def test_knowledge_module_read_note_returns_real_file_content():
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)
    rel_path = "07-JARVIS/JARVIS — COMMAND CENTER.md"
    real_content = (REAL_VAULT / rel_path).read_text(encoding="utf-8")

    response = client.get("/3d/api/module/knowledge", params={"note": rel_path})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["note"]["found"] is True
    assert data["note"]["path"] == rel_path
    assert data["note"]["content"] == real_content
    assert real_content.strip() != ""


def test_knowledge_module_nonexistent_document_is_reported_honestly():
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    response = client.get("/3d/api/module/knowledge", params={"note": "Does/Not/Exist.md"})

    # Not a 404 — same "still a real, navigable Nucleus" convention every
    # other module route uses; the honesty is in the payload, not the status.
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["note"]["found"] is False
    assert data["note"]["content"] is None
    assert "not found" in data["summary"].lower()


def test_knowledge_module_requires_authentication():
    server = DashboardServer()
    client = TestClient(server.app)  # no auth headers

    response = client.get("/3d/api/module/knowledge")

    assert response.status_code == 401


def test_knowledge_module_empty_vault_reports_honestly_not_fabricated(tmp_path, monkeypatch):
    monkeypatch.setattr(headless_config, "OBSIDIAN_VAULT_PATH", str(tmp_path))
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    response = client.get("/3d/api/module/knowledge")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["configured"] is True   # the directory exists, just has no notes
    assert data["notes"] == []
    assert data["summary"] == "The JARVIS Brain vault is empty."


def test_knowledge_module_unconfigured_vault_reports_honestly_not_fabricated(tmp_path, monkeypatch):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(headless_config, "OBSIDIAN_VAULT_PATH", str(missing))
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    response = client.get("/3d/api/module/knowledge")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["configured"] is False
    assert data["notes"] == []
    assert data["summary"] == "No JARVIS Brain vault configured."


def test_knowledge_module_distinct_from_files_module():
    # Files stays a general filesystem search; Knowledge is vault-specific —
    # confirms the two never collapse into the same data shape/source.
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    files_data = client.get("/3d/api/module/files").json()["data"]
    knowledge_data = client.get("/3d/api/module/knowledge").json()["data"]

    assert "notes" in knowledge_data and "notes" not in files_data
    assert "results" not in files_data or "top_products" not in files_data  # files' own shape, untouched
    assert "recent_files" in files_data and "recent_files" not in knowledge_data
