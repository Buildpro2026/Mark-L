"""HTTP surface for the shared tool executor (core/headless/tool_executor.py)
— the same dispatch main.py's voice loop uses, reachable over the API for
non-voice callers (a future remote UI, a scheduled job, manual testing).
Behind require_auth: no unauthenticated tool execution, per the J1
dashboard finding this project is fixing across the board.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.headless.auth import require_auth
from core.headless.context import ToolContext
from core.headless.tool_executor import ToolExecutor, UnknownToolError
from core.headless.tool_registry import TOOL_DECLARATIONS

router = APIRouter(dependencies=[Depends(require_auth)])

# One executor, one NullPlayer-backed context, for the life of the process
# — every extracted tool is stateless aside from the proactive engine's
# snooze/rotation state, which is fine to share process-wide here (there's
# no per-caller session concept in the headless API, unlike main.py's one
# ToolContext per live voice session).
_executor = ToolExecutor(ToolContext())


class ExecuteToolRequest(BaseModel):
    name: str
    args: dict = {}


@router.get("/tools")
def list_tools():
    return {"tools": [t["name"] for t in TOOL_DECLARATIONS]}


@router.post("/tools/execute")
async def execute_tool(body: ExecuteToolRequest):
    try:
        result = await _executor.execute(body.name, body.args)
    except UnknownToolError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Tool '{body.name}' failed: {e}")
    return {"result": result}
