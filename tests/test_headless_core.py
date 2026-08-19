"""J2 verification — the headless core actually does what J2 claims, not
just "the file exists." Covers the 15 points from the J2 spec:

 1. Headless JARVIS imports without PyQt6.
 2. Headless JARVIS imports without sounddevice.
 3. Tool registry loads.
 4. Existing safe tool executes.
 5. Agent task can be created.
 6. Pending approval blocks execution.
 7. Explicit approval permits execution.
 8. Successful execution records success.
 9. Failed execution records failure.
10. Background worker can start independently (no Gemini session).
11. Health endpoint responds.
12. Authentication blocks unauthorized API access.
13. Authorized API access works.
14. Runtime data is not written into tracked source files.
15. Existing desktop app still starts through the shared execution layer.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]

_AUTH_HEADERS = {"Authorization": "Bearer test-dashboard-token-not-a-real-secret"}


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── 1 & 2: headless imports survive PyQt6/sounddevice being absent ──────
# A plain `import core.headless.app` in THIS process proves nothing about
# whether it secretly depends on PyQt6/sounddevice, since both are already
# installed in this venv and may already be imported by an earlier test in
# the same session. A subprocess with a meta-path finder that makes those
# two packages genuinely unimportable is the only way to prove the
# headless package doesn't need them — same isolation technique
# test_command_center_url.py already uses for the opposite reason (to
# force a real PyQt6 construction outside pytest's own process state).
_BLOCK_SCRIPT = textwrap.dedent(r"""
    import sys, importlib.abc

    class _Blocker(importlib.abc.MetaPathFinder):
        BLOCKED = {"PyQt6", "sounddevice"}
        def find_spec(self, name, path, target=None):
            if name.split(".")[0] in self.BLOCKED:
                raise ImportError(f"blocked for headless-import test: {name}")
            return None

    sys.meta_path.insert(0, _Blocker())
    sys.path.insert(0, __ROOT__)

    import core.headless.app
    import core.headless.tool_executor
    import core.headless.tool_registry
    import core.headless.context
    import core.headless.config
    import core.headless.auth
    import core.headless.background
    import core.headless.orchestrator_api
    import core.headless.tools_api
    import core.headless.obsidian
    import core.headless.ui
    import core.headless.status_api
    import core.headless.personalization
    import actions.voice_manager
    import actions.priorities_engine
    print("HEADLESS_IMPORT_OK")
""")


def test_headless_core_imports_without_pyqt6_or_sounddevice():
    script = _BLOCK_SCRIPT.replace("__ROOT__", repr(str(ROOT)))
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "HEADLESS_IMPORT_OK" in result.stdout


# ── 3: tool registry loads ───────────────────────────────────────────────

def test_tool_registry_loads():
    from core.headless.tool_registry import TOOL_DECLARATIONS, SESSION_ONLY_TOOLS
    names = {t["name"] for t in TOOL_DECLARATIONS}
    assert "gmail" in names and "agent_orchestrator" in names and "buildpro_matching" in names
    assert len(TOOL_DECLARATIONS) > 30
    # Every declared tool has a name + parameters schema — not just labels.
    for t in TOOL_DECLARATIONS:
        assert "name" in t and "parameters" in t
    assert SESSION_ONLY_TOOLS <= names   # the four excluded tools are real declared tools, not typos


# ── 4: an existing safe tool actually executes headlessly ───────────────

def test_safe_tool_executes_headlessly(monkeypatch, tmp_path):
    from actions import business_intelligence as bi
    monkeypatch.setattr(bi, "DB_PATH", tmp_path / "bi_test.db")

    from core.headless.tool_executor import ToolExecutor
    from core.headless.context import ToolContext

    executor = ToolExecutor(ToolContext())
    result = asyncio.run(executor.execute("business_intelligence", {
        "action": "log", "category": "research", "business": "general",
        "title": "headless smoke test", "content": "ran via ToolExecutor directly",
    }))
    assert "logged" in result.lower()

    listed = asyncio.run(executor.execute("business_intelligence", {"action": "list", "business": "general"}))
    assert "headless smoke test" in listed


def test_session_only_tool_refuses_clearly_instead_of_crashing():
    from core.headless.tool_executor import ToolExecutor, UnknownToolError
    from core.headless.context import ToolContext

    executor = ToolExecutor(ToolContext())
    with pytest.raises(UnknownToolError):
        asyncio.run(executor.execute("screen_process", {"text": "what do you see"}))


# ── 5-9: agent task lifecycle — create / pending-approval / approve /
#         success / failure — against a real isolated AgentOrchestrator,
#         not a mock of it.

@pytest.fixture
def isolated_orchestrator(monkeypatch, tmp_path):
    from actions import agent_orchestrator as ao
    monkeypatch.setattr(ao, "DB_PATH", tmp_path / "orch_test.db")
    monkeypatch.setattr(ao, "LOCK_PATH", tmp_path / "orch_test.lock")

    calls = {"observe": 0, "execute": 0, "failing": 0}

    def _observe_handler(task):
        calls["observe"] += 1
        return {"summary": "observed fine"}

    def _execute_handler(task):
        calls["execute"] += 1
        return {"summary": "executed for real"}

    def _failing_handler(task):
        calls["failing"] += 1
        raise RuntimeError("simulated failure")

    agents = {
        "test_observer": ao.AgentDefinition(
            id="test_observer", name="Test Observer", description="d", nucleus_id="system",
            permission_level=ao.PermissionLevel.OBSERVE, handler=_observe_handler,
        ),
        "test_executor": ao.AgentDefinition(
            id="test_executor", name="Test Executor", description="d", nucleus_id="system",
            permission_level=ao.PermissionLevel.EXECUTE, handler=_execute_handler,
        ),
        "test_failer": ao.AgentDefinition(
            id="test_failer", name="Test Failer", description="d", nucleus_id="system",
            permission_level=ao.PermissionLevel.OBSERVE, handler=_failing_handler,
        ),
    }
    orch = ao.AgentOrchestrator(agents=agents)
    return orch, calls


def test_agent_task_can_be_created(isolated_orchestrator):
    orch, calls = isolated_orchestrator
    task = orch.assign_task("test_observer", "do the thing")
    assert task.agent_id == "test_observer"
    assert task.id


def test_pending_approval_blocks_execution(isolated_orchestrator):
    orch, calls = isolated_orchestrator
    task = orch.assign_task("test_executor", "do the consequential thing")
    assert task.status.value == "pending_approval"
    assert orch.get_task(task.id).status.value == "pending_approval"
    assert calls["execute"] == 0   # never ran — this is the whole point of the gate
    # run_task() itself refuses a PENDING_APPROVAL task directly, not just assign_task
    with pytest.raises(PermissionError):
        orch.run_task(task.id)
    assert calls["execute"] == 0


def test_explicit_approval_permits_execution(isolated_orchestrator):
    orch, calls = isolated_orchestrator
    task = orch.assign_task("test_executor", "do the consequential thing")
    approved = orch.approve_task(task.id)
    assert approved.status.value == "done"
    assert calls["execute"] == 1
    assert approved.result == {"summary": "executed for real"}


def test_successful_execution_records_success(isolated_orchestrator):
    orch, calls = isolated_orchestrator
    task = orch.assign_task("test_observer", "observe something")   # OBSERVE runs immediately
    assert task.status.value == "done"
    assert task.error is None
    assert task.result == {"summary": "observed fine"}
    assert calls["observe"] == 1


def test_failed_execution_records_failure(isolated_orchestrator):
    orch, calls = isolated_orchestrator
    task = orch.assign_task("test_failer", "this will blow up")
    assert task.status.value == "failed"
    assert "simulated failure" in task.error
    assert calls["failing"] == 1


# ── 10: background worker starts independently of any Gemini session ────

def test_background_worker_starts_independently_of_gemini_session():
    from core.headless.background import BackgroundWorker

    async def _run():
        worker = BackgroundWorker()
        worker.start()
        try:
            assert len(worker._tasks) == 3
            assert all(isinstance(t, asyncio.Task) for t in worker._tasks)
            assert all(not t.done() for t in worker._tasks)
        finally:
            await worker.stop()
        assert worker._tasks == []

    asyncio.run(_run())
    # No Gemini/google.genai import anywhere in background.py or its
    # actions.* dependencies — this ran with none configured/mocked at all.


# ── 11-13: health endpoint, auth blocks/allows ───────────────────────────

@pytest.fixture
def headless_client(monkeypatch):
    from core.headless import config
    monkeypatch.setattr(config, "API_TOKEN", "test-dashboard-token-not-a-real-secret")
    from core.headless.app import create_app
    app = create_app(start_background_worker=False)
    return TestClient(app)


def test_health_endpoint_responds(headless_client):
    resp = headless_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "api_token_configured" in body
    # Presence flags like "gemini_api_key_env_set": true are fine — only the
    # literal secret-value key would be a leak, and this must never appear.
    assert '"gemini_api_key":' not in json.dumps(body)
    assert '"api_token":' not in json.dumps(body)


def test_dashboard_import_failure_degrades_instead_of_crashing_whole_app(monkeypatch):
    # Phase 2 fix: create_app() used to call DashboardServer() with no
    # try/except, so a broken dashboard/server.py took the entire headless
    # process down with it — /health, the tools API, and the orchestrator
    # API included. A dashboard bug should degrade the UI, not the API.
    import dashboard.server as dashboard_server_module

    def _boom(*a, **kw):
        raise RuntimeError("simulated dashboard/server.py failure")

    monkeypatch.setattr(dashboard_server_module, "DashboardServer", _boom)

    from core.headless import config
    monkeypatch.setattr(config, "API_TOKEN", "test-dashboard-token-not-a-real-secret")
    from core.headless.app import create_app
    app = create_app(start_background_worker=False)
    client = TestClient(app)

    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["dashboard_ui_available"] is False

    # Core API routes must still work even though the dashboard failed.
    resp = client.get("/api/tools", headers={"Authorization": "Bearer test-dashboard-token-not-a-real-secret"})
    assert resp.status_code == 200

    # The mounted-dashboard fallback route responds instead of 500ing.
    resp = client.get("/")
    assert resp.status_code == 503


def test_authentication_blocks_unauthorized_api_access(headless_client):
    resp = headless_client.get("/api/orchestrator/agents")
    assert resp.status_code == 401

    resp2 = headless_client.post("/api/tools/execute", json={"name": "system_status", "args": {}})
    assert resp2.status_code == 401


def test_authorized_api_access_works(headless_client):
    resp = headless_client.get("/api/orchestrator/agents", headers=_AUTH_HEADERS)
    assert resp.status_code == 200
    assert "agents" in resp.json()

    resp2 = headless_client.post(
        "/api/tools/execute", headers=_AUTH_HEADERS,
        json={"name": "system_status", "args": {}},
    )
    assert resp2.status_code == 200
    assert "cpu_percent" in resp2.json()["result"]


# ── 14: runtime data lives outside tracked source ────────────────────────

def test_runtime_data_not_written_into_tracked_source():
    from core.headless import config
    # DATA_DIR/DB_PATH must resolve under <repo>/data — the directory
    # .gitignore already excludes wholesale (see .gitignore: "data/") —
    # never under core/, tests/, actions/, or any other tracked package.
    assert config.DATA_DIR.name == "data"
    assert config.DB_PATH.parent.name == "data"
    for tracked_dir in ("core", "tests", "actions", "memory", "dashboard"):
        assert tracked_dir not in config.DATA_DIR.parts[-2:]

    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/" in gitignore   # the directory these paths resolve under is actually ignored


# ── 15: the desktop app runs through the SAME executor class, not a copy ─

# ── J3 Part 1: JARVIS_DATA_DIR actually relocates runtime state ─────────
# DATA_DIR is a module-level constant computed once at import time, so
# monkeypatching it after the fact would only prove the attribute *can* be
# reassigned, not that the environment variable itself works end to end.
# A subprocess with the env var set before any import is the real proof.
_DATA_DIR_SCRIPT = textwrap.dedent(r"""
    import json, sys
    sys.path.insert(0, __ROOT__)

    from core.headless import config
    from actions import agent_orchestrator as ao
    from actions import business_intelligence as bi
    from actions import buildpro_data as bd
    from actions import opportunity_engine as oe
    from actions import daily_deal_finders as ddf
    from actions import proactive as pr
    from actions import twilio_integration as tw
    from actions import buffer_integration as buf
    from core import startup

    modules = {
        "agent_orchestrator.DB_PATH": ao.DB_PATH,
        "agent_orchestrator.LOCK_PATH": ao.LOCK_PATH,
        "business_intelligence.DB_PATH": bi.DB_PATH,
        "buildpro_data.DB_PATH": bd.DB_PATH,
        "opportunity_engine.DB_PATH": oe.DB_PATH,
        "daily_deal_finders.DB_PATH": ddf.DB_PATH,
        "proactive.DB_PATH": pr.DB_PATH,
        "twilio_integration.DB_PATH": tw.DB_PATH,
        "buffer_integration.DB_PATH": buf.DB_PATH,
        "startup.APP_LOCK_PATH": startup.APP_LOCK_PATH,
    }
    print(json.dumps({k: str(v) for k, v in modules.items()}))
""")


def test_jarvis_data_dir_env_var_relocates_all_runtime_state_paths(tmp_path, monkeypatch):
    import os
    custom_dir = tmp_path / "custom_jarvis_data"
    env = dict(os.environ)
    env["JARVIS_DATA_DIR"] = str(custom_dir)
    script = _DATA_DIR_SCRIPT.replace("__ROOT__", repr(str(ROOT)))
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, env=env,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    paths = json.loads(result.stdout.strip().splitlines()[-1])
    for label, raw_path in paths.items():
        assert str(custom_dir) in raw_path, f"{label} did not respect JARVIS_DATA_DIR: {raw_path}"
        assert "jarvis2.db" in raw_path or "lock" in raw_path


def test_data_dir_defaults_to_repo_data_folder_when_env_unset(tmp_path):
    import os
    env = dict(os.environ)
    env.pop("JARVIS_DATA_DIR", None)
    script = _DATA_DIR_SCRIPT.replace("__ROOT__", repr(str(ROOT)))
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, env=env,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    paths = json.loads(result.stdout.strip().splitlines()[-1])
    for label, raw_path in paths.items():
        assert str(ROOT / "data") in raw_path, f"{label} did not default to <repo>/data: {raw_path}"


# ── J3 Part 4: Obsidian read/write, against a real filesystem vault ─────
# Hermetic and machine-independent on purpose — a real end-to-end pass
# against Lee's actual "JARVIS — BuildPro Operating System" vault was
# run manually during J3 (see the J3 report) rather than committed here,
# since baking that personal path into a tracked test file is exactly
# what the J1/J2/J3 instructions repeatedly rule out. This proves the
# same read/write/safety mechanics against a throwaway tmp_path vault
# with the same shape (numbered folders, .md notes).

@pytest.fixture
def sample_vault(tmp_path):
    from core.headless.obsidian import ObsidianVault
    vault_dir = tmp_path / "TestVault"
    (vault_dir / "01-FOUNDER").mkdir(parents=True)
    (vault_dir / "01-FOUNDER" / "Founder.md").write_text("# Founder\nTest founder note.", encoding="utf-8")
    (vault_dir / "02-COMPANY").mkdir()
    (vault_dir / "02-COMPANY" / "Company.md").write_text("# Company\nTest company note.", encoding="utf-8")
    return ObsidianVault(str(vault_dir))


def test_obsidian_read_test(sample_vault):
    assert sample_vault.is_configured()
    notes = sample_vault.list_notes()
    assert "01-FOUNDER/Founder.md" in notes
    assert "02-COMPANY/Company.md" in notes
    content = sample_vault.read_note("01-FOUNDER/Founder.md")
    assert "Test founder note" in content
    results = sample_vault.search_notes("company")
    assert any(r["path"] == "02-COMPANY/Company.md" for r in results)


def test_obsidian_write_test_using_a_safe_test_note(sample_vault):
    result = sample_vault.record_decision(
        "Test decision", "This is a safe, clearly-labeled test note — not real founder/company data.",
    )
    assert result["ok"]
    assert result["path"].startswith("Jarvis/Decisions/")
    readback = sample_vault.read_note(result["path"])
    assert "Test decision" in readback
    assert "safe, clearly-labeled test note" in readback


def test_obsidian_write_requires_approved_flag(sample_vault):
    r = sample_vault.write_note("01-FOUNDER/Founder.md", "overwritten!", approved=False)
    assert r["ok"] is False
    assert r["state"] == "NOT_APPROVED"
    assert "Test founder note" in sample_vault.read_note("01-FOUNDER/Founder.md")   # untouched


def test_obsidian_never_blindly_overwrites_an_existing_note(sample_vault):
    r = sample_vault.write_note("01-FOUNDER/Founder.md", "overwritten!", approved=True)
    assert r["ok"] is False
    assert r["state"] == "EXISTS"
    assert "Test founder note" in sample_vault.read_note("01-FOUNDER/Founder.md")   # still untouched

    r2 = sample_vault.write_note("01-FOUNDER/Founder.md", "deliberately overwritten", approved=True, overwrite=True)
    assert r2["ok"] is True
    assert sample_vault.read_note("01-FOUNDER/Founder.md") == "deliberately overwritten"


def test_obsidian_refuses_path_traversal(sample_vault):
    with pytest.raises(ValueError):
        sample_vault._resolve_safe("../outside_the_vault.md")


def test_obsidian_reports_not_configured_honestly_when_unset():
    # Pass "" explicitly rather than None — None means "use
    # config.OBSIDIAN_VAULT_PATH", which on a machine with a real vault
    # configured via .env (as this one is — see the J3 report) would
    # legitimately resolve to something real. "" means "no vault, full
    # stop," which is what this test is actually checking.
    from core.headless.obsidian import ObsidianVault, VaultNotConfigured
    vault = ObsidianVault("")
    assert not vault.is_configured()
    assert vault.list_notes() == []
    with pytest.raises(VaultNotConfigured):
        vault.record_decision("x", "y")


# ── J3 Part 18: audit log ────────────────────────────────────────────────

def test_audit_log_records_and_lists_consequential_actions(monkeypatch, tmp_path):
    from actions import audit_log as al
    monkeypatch.setattr(al, "DB_PATH", tmp_path / "audit_test.db")

    row_id = al.record(
        "gmail_send", execution_status="succeeded", result={"to": "a@b.com"},
        external_system="gmail", reference_id="msg-1",
    )
    assert row_id > 0

    al.record("gmail_send", execution_status="failed", error="quota exceeded", external_system="gmail")

    rows = al.list_recent(limit=10)
    assert len(rows) == 2
    assert rows[0]["action"] == "gmail_send"
    assert rows[0]["execution_status"] == "failed"   # most recent first
    assert rows[1]["result"] == {"to": "a@b.com"}


def test_gmail_send_via_tool_executor_writes_an_audit_entry(monkeypatch, tmp_path):
    from actions import audit_log as al
    from actions import gmail_integration
    monkeypatch.setattr(al, "DB_PATH", tmp_path / "audit_test2.db")
    monkeypatch.setattr(gmail_integration, "send_email",
                         lambda to, subject, body, approved=False: {"ok": True, "message_id": "m1"})

    from core.headless.tool_executor import ToolExecutor
    from core.headless.context import ToolContext
    executor = ToolExecutor(ToolContext())
    asyncio.run(executor.execute("gmail", {
        "action": "send", "to": "john@example.com", "subject": "Hi", "body": "Test",
    }))

    rows = al.list_recent(limit=5)
    assert any(r["action"] == "gmail_send" and r["execution_status"] == "succeeded" for r in rows)


def test_desktop_app_uses_the_shared_tool_executor_not_a_duplicate():
    main = load_module("jarvis_j2_shared_executor_check", "main.py")
    from core.headless.tool_executor import ToolExecutor as SharedToolExecutor

    live = object.__new__(main.JarvisLive)
    live.ui = type("UIStub", (), {
        "muted": False, "set_state": lambda self, s: None, "write_log": lambda self, m: None,
    })()

    # main.py imports the exact same class object, not a reimplementation —
    # this is the load-bearing assertion that Step 1's "no duplicate tool
    # logic" requirement actually held.
    assert main.ToolExecutor is SharedToolExecutor
    assert isinstance(live._tool_executor, SharedToolExecutor)
