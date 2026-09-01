"""The phone line's brain — what the deployed Cartesia Line agent calls.

Architecture, and why it's this shape:

    caller  <->  Cartesia Line agent  <->  THIS SERVICE  <->  every JARVIS
    (phone)      (ears, mouth, turn-      (the real brain:    tool, agent,
                  taking, telephony)       run_chat_turn +     and memory
                                           ToolExecutor)

The Line agent deliberately owns no business logic. It holds a fast model
purely for conversational glue — acknowledgements, "one moment", knowing
when a turn has actually ended — and delegates anything that touches real
data or takes a real action to /api/voice/ask, which runs the SAME
run_chat_turn() the browser /ui and the 3D command center already run.

That is the entire reason this file exists instead of a second tool
registry living inside the voice agent: JARVIS's tools, prompt, provider
fallback, and audit logging stay defined in exactly one place. Adding a
tool to core/headless/tool_registry.py makes it reachable by phone the
same day, with no redeploy of the voice agent at all.

Auth: bearer JARVIS_API_TOKEN, same as every other /api route. The voice
agent holds that token as a Cartesia secret.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.headless import config
from core.headless.auth import require_auth

logger = logging.getLogger("jarvis.headless.voice")

router = APIRouter(dependencies=[Depends(require_auth)])

PROMPT_PATH = config.BASE_DIR / "core" / "prompt.txt"

# Voice is not text. These rules are appended to JARVIS's normal persona
# rather than replacing it, because the personality should be identical on
# the phone and in the browser — only the delivery changes. Everything here
# is a genuine constraint of the medium: a caller cannot see a bulleted
# list, cannot re-read a long sentence, and will talk over a monologue.
_VOICE_RULES = """

# Speaking on the phone

You are on a live phone call right now. Everything you say is spoken aloud.

- One or two sentences per turn. This is a conversation, not a briefing.
- Never read out lists, bullet points, markdown, URLs, or raw ids. Say
  "three things came in" and then name them in a sentence.
- Numbers and dates spoken naturally: "about twelve hundred", "Thursday
  the fourth", not "1,247" or "2026-09-04".
- If something will take a moment, say so first, then do it.
- If you don't know, say so plainly. Never invent a fact, a number, a
  name, or the result of an action you didn't actually take.
- Ask one question at a time, then stop and let him answer.
"""


def _base_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        logger.warning("core/prompt.txt unreadable — falling back to the minimal voice persona.")
        return (
            "You are JARVIS, chief of staff to Lee. You are concise, direct, "
            "and you never simulate or guess the result of a tool — you use "
            "the real one."
        )


def _live_context() -> str:
    """A short, spoken-sized snapshot of what is actually live right now.

    This exists so the first thing out of JARVIS's mouth on an inbound call
    can be grounded rather than generic, without the voice agent making a
    round trip before it is allowed to speak. Deliberately capped and
    deliberately failure-tolerant: a broken priorities engine must degrade
    the greeting, never drop the call.
    """
    lines: list[str] = []
    try:
        from actions.priorities_engine import get_todays_priorities
        for p in (get_todays_priorities(limit=3) or []):
            title = (p.get("title") or p.get("summary") or "").strip()
            if title:
                lines.append(f"- {title}")
    except Exception:
        logger.debug("priorities unavailable for voice context", exc_info=True)

    try:
        from core.headless import status_api
        agents = status_api.active_agents() or {}
        running = agents.get("agents") or agents.get("active_agents") or []
        if running:
            lines.append(f"- {len(running)} agent(s) currently running")
    except Exception:
        logger.debug("active agents unavailable for voice context", exc_info=True)

    if not lines:
        return ""
    return "\n\n# Live right now\n\n" + "\n".join(lines) + (
        "\n\nMention these only if he asks what's going on, or if one of them "
        "is the reason you called."
    )


@router.get("/context")
def voice_context():
    """Fetched once by the voice agent at the start of every call.

    Returning the prompt from here rather than baking it into the voice
    agent's own code is what keeps a prompt edit a one-file change: update
    core/prompt.txt, and the next phone call already uses it. No redeploy
    of the Cartesia agent.
    """
    return {
        "system_prompt": _base_prompt() + _VOICE_RULES + _live_context(),
        "greeting": "JARVIS here, sir.",
        "voice_id": config.CARTESIA_VOICE_ID,
        "tts_model": config.CARTESIA_TTS_MODEL,
        "owner_phone": config.JARVIS_OWNER_PHONE,
    }


class AskRequest(BaseModel):
    message: str
    history: list[dict] = []   # [{"role": "user"|"model", "text": "..."}]


@router.post("/ask")
async def voice_ask(body: AskRequest):
    """One full JARVIS turn, for the voice agent to speak.

    This is run_chat_turn() — the same provider chain, the same
    ToolExecutor, the same audit logging as the browser. A phone call is
    just another surface onto the one brain; it is not a parallel
    implementation of it.
    """
    from core.headless.ui import run_chat_turn

    try:
        reply, tool_calls = await run_chat_turn(body.message, body.history)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("voice turn failed")
        raise HTTPException(status_code=500, detail=f"Voice turn failed: {e}")

    return {
        "reply": reply,
        # Names only. The voice agent has no use for tool payloads, and a
        # transcript that quietly carries HubSpot record contents through
        # a third party's logs is not something to do by accident.
        "tools_used": [t.get("name") for t in (tool_calls or []) if t.get("name")],
    }


class CallEndedRequest(BaseModel):
    agent_call_id: str = ""
    from_number: str = ""
    direction: str = "inbound"
    duration_seconds: float = 0.0
    summary: str = ""


@router.post("/call-ended")
def voice_call_ended(body: CallEndedRequest):
    """Logged when a call wraps, so a phone conversation leaves the same
    trail as any other consequential action rather than vanishing.

    Best-effort by design: a logging failure must never propagate back
    into the voice agent's hang-up path.
    """
    try:
        from actions import audit_log
        audit_log.record(
            "voice_call",
            execution_status="succeeded",
            result={
                "direction": body.direction,
                "from_number": body.from_number,
                "duration_seconds": body.duration_seconds,
                "summary": body.summary[:2000],
            },
            external_system="cartesia",
            reference_id=body.agent_call_id or None,
        )
        return {"logged": True}
    except Exception as e:
        logger.warning("call-ended log failed: %s", e)
        return {"logged": False, "detail": str(e)}
