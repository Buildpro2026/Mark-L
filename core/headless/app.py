"""Headless JARVIS FastAPI app — importable without PyQt6, sounddevice, or
a live Gemini Live session. Assembles the health check, tool-execution API,
and Agent Orchestrator API behind one app object; core/headless_main.py is
the process entry point that actually serves it plus the background worker.

The bare public URL opens the orb-first executive surface at /ui. Its
conversation, approvals, agents, business data, and settings are backed by
the same headless APIs as the rest of the service. dashboard/server.py's
phone command center and 3D spatial command center remain available at their
explicit routes, including /3d.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse

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

    # The original dashboard is optional in the cloud. It must never prevent
    # the headless API or the primary orb interface from starting.
    dashboard_server = None
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
        """Unauthenticated platform health probe."""
        cfg = config.summarize()
        return {
            "status": "ok" if _db_reachable() else "degraded",
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
    app.include_router(agreement_routes.router)

    # The orb-first executive surface is the primary browser entry point.
    # It is independent of the optional legacy dashboard.
    @app.get("/", include_in_schema=False)
    def primary_jarvis_ui():
        return RedirectResponse(url="/ui", status_code=307)

    # The legacy dashboard is optional. Keep a clean response for its routes
    # when the desktop dashboard cannot load in the cloud.
    if dashboard_server is not None:
        app.mount("/", dashboard_server.app)
    else:
        @app.get("/{_path:path}")
        def _dashboard_unavailable(_path: str):
            return JSONResponse(
                {"error": "dashboard UI unavailable in headless runtime", "status": "degraded"},
                status_code=503,
            )

    return app
