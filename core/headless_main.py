"""Headless JARVIS process entry point — no PyQt6, no sounddevice, no
Gemini Live session required to start. Runs the FastAPI app (health check,
tool-execution API, Agent Orchestrator API) and the background worker
(agent scheduler, topic monitor, proactive observer) as one process.

Usage:
    python -m core.headless_main

Configuration is entirely via environment variables — see
core/headless/config.py and .env.example at the repo root. At minimum,
set JARVIS_API_TOKEN before exposing this beyond localhost; without it
every authenticated route returns 503, not open access.
"""
from __future__ import annotations

import logging

import uvicorn

from actions.google_auth import ensure_credentials_from_env
from core.headless import config
from core.headless.app import create_app
from core.startup import print_startup_banner


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    print_startup_banner()
    if not config.API_TOKEN:
        logging.getLogger("jarvis.headless").warning(
            "JARVIS_API_TOKEN is not set — every authenticated route will return 503 "
            "until it's configured. The /health endpoint still works."
        )
    ensure_credentials_from_env()   # no-op unless GOOGLE_TOKEN_JSON/GOOGLE_CLIENT_SECRET_JSON are set

    app = create_app()
    uvicorn.run(app, host=config.HEADLESS_HOST, port=config.HEADLESS_PORT)


if __name__ == "__main__":
    main()
