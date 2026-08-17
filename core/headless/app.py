"""Headless JARVIS FastAPI app — importable without PyQt6, sounddevice, or
a live Gemini Live session. Assembles the health check, tool-execution API,
and Agent Orchestrator API behind one app object; core/headless_main.py is
the process entry point that actually serves it plus the background worker.
"""
from __future__ import annotations

import sqlite3
import time

from fastapi import FastAPI

from core.headless import config
from core.headless import orchestrator_api
from core.headless import tools_api
from core.headless import status_api
from core.headless.background import BackgroundWorker

START_TS = time.time()


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

    if start_background_worker:
        @app.on_event("startup")
        async def _start_background_worker():
            app.state.background_worker.start()

        @app.on_event("shutdown")
        async def _stop_background_worker():
            await app.state.background_worker.stop()

    @app.get("/health")
    def health():
        """Unauthenticated on purpose — this is what a cloud platform's
        health probe / uptime monitor hits, and it reveals no secrets, no
        conversation content, and no business data. Presence/reachability
        only, same discipline as scripts/health_check.py and
        core/startup.py's print_startup_banner()."""
        cfg = config.summarize()
        return {
            "status": "ok",
            "uptime_seconds": round(time.time() - START_TS, 1),
            "db_reachable": _db_reachable(),
            **cfg,
        }

    app.include_router(tools_api.router, prefix="/api")
    app.include_router(orchestrator_api.router, prefix="/api/orchestrator")
    app.include_router(status_api.router, prefix="/api")

    return app
