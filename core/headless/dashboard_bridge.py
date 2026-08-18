"""Headless consumer for dashboard/server.py's command queue — the cloud
equivalent of main.py's DashboardServer._process_dashboard_commands().

Locally, a typed (or transcribed) phone command is dropped onto
DashboardServer._command_queue and main.py's own loop drains it straight
into its live Gemini Live session. There is no such live session in the
headless/cloud process, so this drains the same queue and runs each
command through one Gemini + ToolExecutor turn instead — the exact path
core/headless/ui.py's /ui/api/chat already uses (see run_chat_turn) — then
broadcasts the reply back over the dashboard's own /ws channel exactly
the way main.py's voice loop pushes its spoken replies to the phone UI
(see main.py's "type": "log", "speaker": "jarvis" broadcasts).

This is what makes the original phone dashboard (app.html) and, via
apply_navigation/broadcast_nav, the 3D command center's voice-adjacent
state actually *do* something on Render, instead of just queuing commands
nobody ever reads.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException

from core.headless.ui import run_chat_turn

logger = logging.getLogger("jarvis.headless.dashboard_bridge")

_MAX_HISTORY_TURNS = 20


async def run(dashboard_server) -> None:
    history: list[dict] = []
    while True:
        text = await dashboard_server._command_queue.get()
        if not text:
            continue
        try:
            reply, _tool_calls = await run_chat_turn(text, history)
        except HTTPException as e:
            reply = f"Error: {e.detail}"
        except Exception as e:
            logger.warning("dashboard bridge turn failed: %s", e)
            reply = f"Error: {e}"
        history.append({"role": "user", "text": text})
        history.append({"role": "model", "text": reply})
        del history[:-_MAX_HISTORY_TURNS]
        await dashboard_server.broadcast({"type": "log", "speaker": "jarvis", "text": reply})
