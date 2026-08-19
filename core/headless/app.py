"""Headless JARVIS FastAPI app — importable without PyQt6, sounddevice, or
a live Gemini Live session. Assembles the health check, tool-execution API,
and Agent Orchestrator API behind one app object; core/headless_main.py is
the process entry point that actually serves it plus the background worker.

The bare public URL (see the dashboard mount at the bottom of create_app)
serves the ORIGINAL JARVIS interface — dashboard/server.py's login/pairing
flow, phone command-center (app.html), and 3D spatial command center
(/3d) — not a bespoke replacement UI. core/headless/ui.py's own generic
SPA stays mounted at /ui as a fallback surface (nothing currently links to
it) rather than being deleted outright.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from core.headless import agreement_routes
from core.headless import config
from core.headless import dashboard_bridge
from core.headless import orchestrator_api
from core.headless import tools_api
from core.headless import status_api
from core.headless import ui
from core.headless.background import BackgroundWorker

START_TS = time.time()
logger = logging.getLogger("jarvis.headless.app")


def _db_reachable() -> bool:
    try:
        conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=2)
        conn.execute("SELECT 1")
        conn.close()
        return True
    except Exception:
        return False


def create_app(start_background_worker: bool = True) -> FastAPI:
    config.ensure_data_dir()
    app = FastAPI(
        title="JARVIS Headless Core",
        docs_url=None, redoc_url=None,
    )
    app.state.background_worker = BackgroundWorker()

    # The original JARVIS dashboard/phone/3D-command-center UI, imported
    # lazily here (not at module scope) so a dashboard/server.py import
    # failure can't take the whole headless app down with it — see the
    # mount at the bottom of this function. The lazy import alone only
    # protects `import core.headless.app`; create_app() itself used to call
    # DashboardServer() with no try/except, so a real failure here (e.g. a
    # missing dependency or a bug in dashboard/server.py) still took down
    # /health, the tools API, and the orchestrator API along with it —
    # exactly the outcome this comment claimed couldn't happen. Now a
    # failure here is logged and the core API stays up in a degraded mode
    # instead of crash-looping the whole process.
    dashboard_server = None
    try:
        from dashboard.server import DashboardServer
        dashboard_server = DashboardServer()
    except Exception:
        logger.exception(
            "dashboard/server.py failed to load — /health, the tools API, and the "
            "orchestrator API stay up, but the phone/3D command-center UI at '/' "
            "will not be available until this is fixed."
        )
    app.state.dashboard_server = dashboard_server

    if start_background_worker:
        @app.on_event("startup")
        async def _start_background_worker():
            app.state.background_worker.start()
            if dashboard_server is not None:
                app.state.dashboard_bridge_task = asyncio.create_task(
                    dashboard_bridge.run(dashboard_server)
                )

        @app.on_event("shutdown")
        async def _stop_background_worker():
            await app.state.background_worker.stop()
            task = getattr(app.state, "dashboard_bridge_task", None)
            if task:
                task.cancel()

    @app.get("/health")
    def health():
        """Unauthenticated on purpose — this is what a cloud platform's
        health probe / uptime monitor hits, and it reveals no secrets, no
        conversation content, and no business data. Presence/reachability
        only, same discipline as scripts/health_check.py and
        core/startup.py's print_startup_banner()."""
        cfg = config.summarize()
        return {
            "status": "ok" if dashboard_server is not None else "degraded",
            "uptime_seconds": round(time.time() - START_TS, 1),
            "db_reachable": _db_reachable(),
            "dashboard_ui_available": dashboard_server is not None,
            **cfg,
        }

    app.include_router(tools_api.router, prefix="/api")
    app.include_router(orchestrator_api.router, prefix="/api/orchestrator")
    app.include_router(status_api.router, prefix="/api")
    app.include_router(ui.router)
    app.include_router(ui.api)
    # Public/unauthenticated on purpose — a candidate reaches this from a
    # plain link in an email, no JARVIS login of their own. See that
    # module's docstring.
    app.include_router(agreement_routes.router)

    # Everything else — "/", "/login", "/3d", "/ws", "/api/command", etc.
    # — falls through to dashboard/server.py's own FastAPI app, mounted
    # last so the explicit routes above (/health, /api/tools*, /api/status,
    # /api/orchestrator/*, /ui/*) are matched first and never shadowed by
    # it. This is what makes the bare public URL serve the ORIGINAL JARVIS
    # phone/3D command-center UI instead of core/headless/ui.py's generic
    # SPA (still reachable at /ui as a fallback — see module docstring).
    if dashboard_server is not None:
        app.mount("/", dashboard_server.app)
    else:
        @app.get("/{_path:path}")
        def _dashboard_unavailable(_path: str):
            return JSONResponse(
                {"error": "dashboard UI failed to load — check server logs", "status": "degraded"},
                status_code=503,
            )

    return app
