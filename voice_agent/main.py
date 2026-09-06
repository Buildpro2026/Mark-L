"""JARVIS on the phone — a Cartesia Line voice agent.

This is the ONLY part of JARVIS that runs on Cartesia's infrastructure.
It owns ears, mouth, and turn-taking. It owns no business logic at all.

    caller  <->  THIS AGENT  <->  jarvis-headless-core on Render
    (phone)      (Cartesia)       (the real brain + every tool)

Why the split is drawn here: JARVIS's tools, memory, prompt, provider
fallback, and audit trail already exist in one place and are already
reachable over HTTP. Re-declaring any of that inside a voice agent would
create a second JARVIS that drifts from the first one the day either is
edited. So this file's job is narrow and permanent:

  * hold the conversation (fast model, sub-second acknowledgements)
  * hand anything real to JARVIS via the ask_jarvis tool
  * speak the answer in the same voice the browser /ui uses

The fast model here is a mouth, not a mind. The system prompt fetched from
/api/voice/context forbids it from answering anything about Lee's business,
calendar, mail, contacts, deals, or agents out of its own head — that all
goes through ask_jarvis, which runs the real run_chat_turn() with the real
tools.

Env (set with `cartesia env set KEY=value`):
    JARVIS_BASE_URL    https://jarvis-headless-core.onrender.com
    JARVIS_API_TOKEN   the same secret as the Render service
    GROQ_API_KEY       conversational model for the voice layer
    VOICE_MODEL        optional LiteLLM id (default below)
    CARTESIA_VOICE_ID  optional; also settable in the dashboard
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Annotated

import httpx

from line.llm_agent import LlmAgent, LlmConfig, end_call
from line.voice_agent_app import VoiceAgentApp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jarvis.voice")

JARVIS_BASE_URL = os.getenv("JARVIS_BASE_URL", "https://jarvis-headless-core.onrender.com").rstrip("/")
JARVIS_API_TOKEN = os.getenv("JARVIS_API_TOKEN", "")

# The conversational layer only has to be quick and hold a thread — the
# thinking happens on the other end of ask_jarvis. gpt-oss-20b on Groq is
# the fastest thing that still calls tools reliably, which is exactly the
# trade this layer wants. Overridable without a code change.
VOICE_MODEL = os.getenv("VOICE_MODEL", "groq/openai/gpt-oss-20b")
VOICE_MODEL_API_KEY = os.getenv("GROQ_API_KEY") or os.getenv("VOICE_MODEL_API_KEY", "")

# A JARVIS turn can legitimately take a while: it may run a web search, a
# HubSpot lookup, and a Gmail read inside one turn. The agent says "one
# moment" before calling, so a long wait is survivable — a wait with no
# ceiling is not.
ASK_TIMEOUT_S = float(os.getenv("JARVIS_ASK_TIMEOUT_S", "75"))

# Render's free plan sleeps after inactivity and takes ~50s to wake. Line
# only rings for about five seconds before connecting, so fetching the
# prompt at call time would mean dead air on the first call after a quiet
# spell. The context is therefore cached in the process and refreshed in
# the background, never on the caller's clock.
CONTEXT_TTL_S = 300.0
_context_cache: dict = {}
_context_fetched_at = 0.0
_context_lock = asyncio.Lock()

FALLBACK_PROMPT = """You are JARVIS, chief of staff to Lee, on a live phone call.

You cannot reach Lee's systems right now, so you have no access to his
calendar, mail, deals, or agents this call. Say that plainly if he asks for
any of it — never guess, and never describe an action you did not take.

Keep replies to a sentence or two. Speak naturally. Ask one thing at a time.
"""


async def _fetch_context(timeout_s: float = 20.0) -> dict:
    """Pull JARVIS's live persona + situational brief from Render."""
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.get(
            f"{JARVIS_BASE_URL}/api/voice/context",
            headers={"Authorization": f"Bearer {JARVIS_API_TOKEN}"},
        )
        resp.raise_for_status()
        return resp.json()


async def get_context() -> dict:
    """Cached context, refreshed off the critical path.

    A stale prompt is vastly better than silence on an answered call, so a
    failed refresh keeps serving the last good copy and only falls back to
    the static prompt when nothing has ever been fetched.
    """
    global _context_cache, _context_fetched_at
    fresh = (time.time() - _context_fetched_at) < CONTEXT_TTL_S
    if _context_cache and fresh:
        return _context_cache
    async with _context_lock:
        if _context_cache and (time.time() - _context_fetched_at) < CONTEXT_TTL_S:
            return _context_cache
        try:
            _context_cache = await _fetch_context()
            _context_fetched_at = time.time()
            logger.info("voice context refreshed from %s", JARVIS_BASE_URL)
        except Exception as e:
            if _context_cache:
                logger.warning("context refresh failed (%s) — serving cached copy", e)
            else:
                logger.error("context fetch failed with no cache (%s) — degraded prompt", e)
                _context_cache = {"system_prompt": FALLBACK_PROMPT, "greeting": "JARVIS here, sir."}
        return _context_cache


async def warm_backend() -> None:
    """Wake a sleeping Render instance and prime the context cache.

    Called on startup and after each call. This is what stops the first
    call of the morning from opening with fifty seconds of nothing.
    """
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            await client.get(f"{JARVIS_BASE_URL}/health")
    except Exception as e:
        logger.warning("backend warm-up ping failed: %s", e)
    await get_context()


TOOL_RULES = """

# Using JARVIS

You are the voice of JARVIS, but you are not his memory and you are not his
hands. Anything about Lee's business, calendar, email, contacts, deals,
candidates, agents, documents, finances, or any current fact about the world
goes through the ask_jarvis tool. Never answer those from your own knowledge,
and never say a task is done unless ask_jarvis told you it was done.

Before calling ask_jarvis, say one short natural line so the caller isn't
sitting in silence — "let me check", "one second, pulling that up". Then call
it. Say the line first, every time; the lookup can take a few seconds.

Write each ask_jarvis request as a complete sentence that stands on its own,
including any detail from earlier in the call that it needs. JARVIS sees the
request you send, not the conversation around it.

When the caller is done, or says goodbye, use end_call.
"""


async def get_agent(env, call_request):
    """Built fresh for every call — the per-call history closure below is
    the reason this can't be a module-level singleton."""
    context = await get_context()

    # Each call keeps its own thread of what was asked and answered, so the
    # brain has continuity within the call ("what about the other one?")
    # without any cross-call state living on Cartesia's side.
    history: list[dict] = []

    metadata = getattr(call_request, "metadata", None) or {}
    reason = metadata.get("reason") if isinstance(metadata, dict) else None

    async def ask_jarvis(
        ctx,
        request: Annotated[
            str,
            "The full self-contained request to send to JARVIS, e.g. "
            "'What's on Lee's calendar tomorrow afternoon?' or 'Text Marcus "
            "that the walkthrough moved to Thursday at 9.'",
        ],
    ) -> str:
        """Ask the real JARVIS. Use for anything involving Lee's data,
        his business, taking an action, or any current fact."""
        nonlocal history
        try:
            async with httpx.AsyncClient(timeout=ASK_TIMEOUT_S) as client:
                resp = await client.post(
                    f"{JARVIS_BASE_URL}/api/voice/ask",
                    headers={"Authorization": f"Bearer {JARVIS_API_TOKEN}"},
                    json={"message": request, "history": history[-12:]},
                )
        except httpx.TimeoutException:
            return ("JARVIS did not answer in time. Tell the caller it's taking too "
                    "long and offer to follow up — do not invent a result.")
        except Exception as e:
            logger.exception("ask_jarvis transport failure")
            return (f"Could not reach JARVIS ({e}). Tell the caller you can't reach "
                    "his systems right now — do not invent a result.")

        if resp.status_code == 401:
            return ("JARVIS rejected the token. Tell the caller the phone line isn't "
                    "authorized right now — do not invent a result.")
        if resp.status_code >= 400:
            return (f"JARVIS returned an error ({resp.status_code}). Tell the caller "
                    "it failed — do not invent a result.")

        data = resp.json()
        reply = (data.get("reply") or "").strip()
        history.append({"role": "user", "text": request})
        history.append({"role": "model", "text": reply})
        return reply or "JARVIS returned nothing. Say you couldn't get an answer."

    # An outbound call has a reason — JARVIS placed it (see
    # actions/cartesia_calls.py). Opening with a generic greeting when you
    # are the one who rang is how a useful call becomes a confusing one.
    if reason:
        introduction = f"Sir, it's JARVIS. {reason}"
    else:
        introduction = context.get("greeting") or "JARVIS here, sir."

    return LlmAgent(
        model=VOICE_MODEL,
        api_key=VOICE_MODEL_API_KEY,
        tools=[ask_jarvis, end_call],
        config=LlmConfig(
            system_prompt=context.get("system_prompt", FALLBACK_PROMPT) + TOOL_RULES,
            introduction=introduction,
        ),
    )


# Voice selection. The dashboard's Voice & Language setting already applies
# to this agent, so this handler is an optional override for keeping the
# phone voice pinned to the same CARTESIA_VOICE_ID the browser /ui speaks
# with. The SDK's pre-call types have moved between versions, so it is
# wired defensively: if the import isn't there, the agent still deploys and
# simply uses the dashboard voice.
_VOICE_ID = os.getenv("CARTESIA_VOICE_ID", "").strip()
_pre_call_handler = None
if _VOICE_ID:
    try:
        from line.voice_agent_app import PreCallResult  # type: ignore
    except Exception:
        try:
            from line import PreCallResult  # type: ignore
        except Exception:
            PreCallResult = None  # type: ignore

    if PreCallResult is not None:
        async def _pre_call_handler(call_request):  # type: ignore[misc]
            return PreCallResult(
                config={
                    "tts": {
                        "voice_id": _VOICE_ID,
                        "model": os.getenv("CARTESIA_TTS_MODEL", "sonic-3"),
                        "language": "en",
                    }
                }
            )
    else:
        logger.warning("PreCallResult unavailable in this SDK build — using the dashboard voice.")

if _pre_call_handler is not None:
    app = VoiceAgentApp(get_agent=get_agent, pre_call_handler=_pre_call_handler)
else:
    app = VoiceAgentApp(get_agent=get_agent)


if __name__ == "__main__":
    try:
        asyncio.run(warm_backend())
    except Exception as e:
        logger.warning("startup warm-up failed: %s", e)
    app.run()
