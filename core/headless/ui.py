"""Browser UI backend surface for jarvis-headless-core.

Serves a same-origin, session-cookie-authenticated JSON API for the static
single-page app in ui_static/index.html. Exists because the deployed
headless service had no browser-usable frontend at all — only /health and
a bearer-token JSON API meant for programmatic callers.

Security model: the browser NEVER sees JARVIS_API_TOKEN. The user pastes
it once into the login form, POST /ui/login checks it server-side against
config.API_TOKEN (the same constant-time check as auth.require_auth) and,
on success, issues a session cookie — not the token itself, and not an
id into any server-side store (see "Session persistence" below) — so the
token never touches page source, browser storage, or JS memory beyond
the single login POST body.

Session persistence: the cookie is a signed, stateless token
(base64(expiry) + "." + base64(HMAC-SHA256(expiry))), verified by
recomputing the HMAC with a key derived from config.API_TOKEN — never
stored anywhere server-side. This is deliberate: Render's Free plan has
no persistent disk (render.yaml's own J4 decision), so anything kept in
a dict or a file is wiped on every restart/redeploy/sleep-wake cycle —
which is exactly what broke the previous in-memory `_sessions` dict,
forcing re-login after every spin-down. A signature check needs no
storage to validate, only the (already-persistent, Render-env-var-backed)
API token, so it survives restarts the same way the token itself does.
Logout can't fully revoke a stateless token without server-side state,
so it does the best available thing: clears the browser's cookie AND
adds the token to a small in-memory revocation set for the remaining
life of THIS process (closes the "already-logged-out tab keeps working"
gap for as long as the process stays up; a restart clears the
revocation set exactly like it clears everything else here, but by then
the cookie is already gone from the browser that logged out, so this
only matters for a captured/replayed cookie value — an inherent limit
of stateless tokens on disk-less infrastructure, not a new one).

Every /ui/api/* handler below is a thin pass-through to the same
functions tools_api.py / orchestrator_api.py / status_api.py already
expose over the bearer-token API — no business logic is duplicated or
reimplemented, so the UI and the programmatic API can never drift apart.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from core.headless import config
from core.headless.auth import check_token
from core.headless import orchestrator_api
from core.headless import status_api
from core.headless import tools_api
from core.headless.orchestrator_api import CreateTaskRequest
from core.headless.tools_api import ExecuteToolRequest

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "core" / "prompt.txt"
CHAT_MODEL = "gemini-flash-latest"   # same model main.py's own text-only Gemini calls use (see _save_session_summary)
_MAX_TOOL_CALL_ROUNDS = 4            # caps a runaway tool-call chain, not normal conversation length

STATIC_DIR = Path(__file__).parent / "ui_static"
INDEX_FILE = STATIC_DIR / "index.html"
AVATAR_DIR = STATIC_DIR / "avatar"
# Pre-generated (SadTalker/MuseTalk, offline, $0, CPU) — see FaceRenderer in
# index.html. An allowlist rather than trusting the path param directly,
# even though FastAPI path params can't contain "/" on their own.
_AVATAR_ASSETS = {"idle_loop.mp4", "speaking_sample.mp4"}

COOKIE_NAME = "jarvis_ui_session"
SESSION_TTL_SECONDS = 365 * 24 * 3600   # Lee's call 2026-08-19: was 12h, too short for a tool left open across a workday

# Best-effort, process-lifetime-only revocation set for logout (see
# "Session persistence" above) — {token: expiry}. Not a session store:
# validity is decided by the HMAC signature, this only ever *removes*
# trust from an otherwise-valid token early. Fine for it to reset on
# restart; that's not a regression, the cookie that would need it is
# gone from the browser by then too.
_revoked: dict[str, float] = {}


def _prune_revoked() -> None:
    now = time.time()
    for tok in [t for t, exp in _revoked.items() if exp < now]:
        _revoked.pop(tok, None)


def _sign_key() -> bytes:
    # Derived, not the raw token itself — key separation, same pattern as
    # dashboard/server.py's _derive_key (SHA-256 of secret + purpose tag).
    return hashlib.sha256((config.API_TOKEN or "").encode("utf-8") + b"jarvis-ui-session-v1").digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s.encode("ascii"))


def _new_session() -> str:
    """Returns a signed, self-verifying session token — nothing is stored
    server-side for this call, on purpose (see module docstring)."""
    exp_bytes = str(int(time.time()) + SESSION_TTL_SECONDS).encode("ascii")
    sig = hmac.new(_sign_key(), exp_bytes, hashlib.sha256).digest()
    return f"{_b64(exp_bytes)}.{_b64(sig)}"


def _session_valid(token: str | None) -> bool:
    if not token or not config.API_TOKEN or "." not in token:
        return False
    exp_part, sig_part = token.split(".", 1)
    try:
        exp_bytes = _b64d(exp_part)
        sig = _b64d(sig_part)
        expiry = int(exp_bytes.decode("ascii"))
    except Exception:
        return False
    expected_sig = hmac.new(_sign_key(), exp_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected_sig):
        return False
    if expiry < time.time():
        return False
    _prune_revoked()
    return token not in _revoked


def require_ui_session(jarvis_ui_session: str | None = Cookie(default=None)) -> None:
    if not _session_valid(jarvis_ui_session):
        raise HTTPException(status_code=401, detail="Not logged in")


router = APIRouter(prefix="/ui")


@router.get("/session")
def get_session(jarvis_ui_session: str | None = Cookie(default=None)) -> dict:
    return {"authenticated": _session_valid(jarvis_ui_session)}


class LoginRequest(BaseModel):
    token: str


@router.post("/login")
def login(body: LoginRequest, response: Response):
    if not config.API_TOKEN:
        raise HTTPException(status_code=503, detail="JARVIS_API_TOKEN is not configured on this server.")
    if not check_token(body.token):
        raise HTTPException(status_code=401, detail="Incorrect token")
    sid = _new_session()
    response.set_cookie(
        COOKIE_NAME, sid,
        max_age=SESSION_TTL_SECONDS, httponly=True, secure=True, samesite="lax",
    )
    return {"ok": True}


@router.post("/logout")
def logout(response: Response, jarvis_ui_session: str | None = Cookie(default=None)):
    if jarvis_ui_session:
        # Best-effort revocation for the rest of this process's uptime —
        # see module docstring's "Session persistence" section for why a
        # stateless token can't be revoked more durably than that.
        _revoked[jarvis_ui_session] = time.time() + SESSION_TTL_SECONDS
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


# ── Authenticated proxy routes — every one below requires the session cookie ──
api = APIRouter(prefix="/ui/api", dependencies=[Depends(require_ui_session)])


@api.get("/tools")
def ui_list_tools():
    return tools_api.list_tools()


@api.post("/tools/execute")
async def ui_execute_tool(body: ExecuteToolRequest):
    return await tools_api.execute_tool(body)


@api.get("/status")
def ui_status():
    return status_api.status()


@api.get("/activity")
def ui_activity(limit: int = 50):
    return status_api.activity(limit=limit)


@api.get("/brief")
def ui_brief():
    return status_api.brief()


class SettingsUpdateRequest(BaseModel):
    updates: dict


@api.get("/priorities")
def ui_priorities(limit: int = 8):
    return status_api.priorities(limit=limit)


@api.get("/active-agents")
def ui_active_agents():
    return status_api.active_agents()


@api.get("/opportunities")
def ui_opportunities(limit: int = 8):
    return status_api.opportunities(limit=limit)


@api.get("/calendar-snapshot")
def ui_calendar_snapshot(max_results: int = 10):
    return status_api.calendar_snapshot(max_results=max_results)


@api.get("/buildpro-overview")
def ui_buildpro_overview():
    return status_api.buildpro_overview()


@api.get("/ddf-overview")
def ui_ddf_overview():
    return status_api.ddf_overview()


@api.get("/intelligence")
def ui_intelligence():
    return status_api.intelligence()


@api.get("/settings")
def ui_get_settings():
    from core.headless import personalization
    return personalization.get_all_settings()


@api.post("/settings")
def ui_update_settings(body: SettingsUpdateRequest):
    from core.headless import personalization
    try:
        return personalization.update_settings(body.updates)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class SpeakRequest(BaseModel):
    text: str


@api.post("/tts/speak")
def ui_tts_speak(body: SpeakRequest):
    """Real neural TTS for the web UI (2026-08-31, Lee's spec: JARVIS must
    sound human, not like OS-default browser speechSynthesis). Returns
    base64 audio for the frontend to play through a real <audio> element;
    never plays anything server-side (no speakers on this container).
    Honest {"configured": false} when no TTS provider is configured,
    rather than a fake 200 — the frontend falls back to speechSynthesis
    itself.

    Provider order is Cartesia first, ElevenLabs second, and that order is
    not arbitrary: the phone line is a Cartesia Line agent speaking with
    CARTESIA_VOICE_ID, so preferring Cartesia here is what makes the
    browser and the phone the same voice instead of two assistants wearing
    the same name. ElevenLabs stays as a real fallback rather than being
    ripped out — if the Cartesia key is missing or its API is down, the
    browser keeps a human-sounding voice."""
    from actions import cartesia_tts, elevenlabs_tts

    providers = [p for p in (cartesia_tts, elevenlabs_tts) if p.is_configured()]
    if not providers:
        return {"configured": False}

    last_detail = None
    for provider in providers:
        result = provider.synthesize_speech(body.text)
        if result.get("ok"):
            return {
                "configured": True, "ok": True,
                "audio_base64": result["audio_base64"],
                "mime_type": result["mime_type"],
                "provider": provider.__name__.rsplit(".", 1)[-1].replace("_tts", ""),
            }
        last_detail = result.get("detail")
    return {"configured": True, "ok": False, "detail": last_detail}


class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []   # [{"role": "user"|"model", "text": "..."}] — kept client-side, not persisted


def _chat_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JARVIS, a concise and direct assistant. "
            "Always use the provided tools to complete tasks — never simulate or guess results."
        )


def _chat_tool_declarations() -> list[dict]:
    from core.headless.tool_registry import TOOL_DECLARATIONS, SESSION_ONLY_TOOLS
    # SESSION_ONLY_TOOLS (screen_process, close_camera, shutdown_jarvis,
    # navigate_command_center) need a live desktop/voice session — excluded
    # from the declared set entirely so Gemini never tries to call one here,
    # rather than declaring them and only failing when invoked.
    return [t for t in TOOL_DECLARATIONS if t["name"] not in SESSION_ONLY_TOOLS]


def _status_label_for_tool(name: str, args: dict) -> str:
    """Human-readable, present-tense status line for a tool about to run —
    what a real UI shows instead of leaving the user staring at nothing
    while a tool call is in flight. Deliberately only describes what's
    ACTUALLY about to happen (the tool being called), never a fabricated
    or generic 'working on it' — see the Phase 3 UX requirement this
    exists for."""
    labels = {
        "web_search": "Researching...",
        "gmail": "Checking email...",
        "calendar": "Checking your calendar...",
        "hubspot": "Checking HubSpot...",
        "airtable": "Checking Airtable...",
        "social_post": "Checking Buffer...",
        "communications": "Checking Twilio...",
        "browser_control": "Working in the browser...",
        "buildpro_matching": "Checking BuildPro records...",
        "daily_deal_finders": "Checking Daily Deal Finders...",
        "agent_orchestrator": "Checking on agents...",
        "business_intelligence": "Checking business intelligence...",
        "opportunity_engine": "Checking opportunities...",
        "strategic_objective": "Checking the revenue objective...",
        "ceo_decision": "Reviewing the decision log...",
        "cloud_status": "Checking the cloud instance...",
        "file_processor": "Processing the file...",
        "code_helper": "Working on the code...",
        "dev_agent": "Building the project...",
        "system_status": "Checking system status...",
        "save_memory": "Saving that...",
    }
    return labels.get(name, "Waiting on external service...")


def _configured_providers() -> list[str]:
    """Ordered by preference — cheapest/most generous first. Groq's free
    tier (no credit card, roughly 1,000-14,400 requests/day depending on
    model) is far more generous than Gemini's free tier (20 requests/day,
    which 429s immediately once exhausted — see the P0 zero-cost-provider
    audit), so it's tried first when configured.

    Gemini is deliberately excluded from this list for now (Lee's
    explicit instruction: Groq-only, so JARVIS never calls Gemini and
    never consumes Gemini credits while that account is being managed
    separately) — GEMINI_API_KEY is still read into config.GEMINI_API_KEY
    and core/headless/gemini_client.py / _run_chat_turn_gemini are still
    fully intact, so re-enabling it later is a one-line change here, not
    a rebuild. This is the entire provider-priority mechanism: which of
    GROQ_API_KEY / ANTHROPIC_TOKEN are set, and in what order they're
    listed here — no other code changes to add, remove, or reorder a
    provider."""
    order = []
    if config.GROQ_API_KEY:
        order.append("groq")
    if config.ANTHROPIC_TOKEN:
        order.append("anthropic")
    return order


async def run_chat_turn(
    message: str, history: list[dict],
    on_status: Callable[[str], Awaitable[None]] | None = None,
    on_tool_event: Callable[[dict], Awaitable[None]] | None = None,
) -> tuple[str, list[dict]]:
    """One conversational turn through a configured AI provider + the
    shared ToolExecutor — the browser equivalent of what Gemini Live's
    function-calling already does for the desktop voice loop (main.py),
    using the SAME tool declarations and the SAME ToolExecutor, just over
    a plain (non-Live) chat-completion call since neither a browser chat
    tab nor a headless cloud process can hold a Live audio session the
    way main.py's Gemini Live client does. Nothing about tool dispatch or
    the underlying agents/business logic is reimplemented here. Shared by
    /ui/api/chat (the generic web UI) and core.headless.dashboard_bridge
    (the original JARVIS phone/3D command-center UI's typed-command
    relay) so both surfaces run the exact same conversational path.

    Which provider actually runs the turn is not hardcoded — see
    _configured_providers(). If the first configured provider fails (on
    any round, not just the first — a 429 on round 2 after round 1
    already ran a tool is common, not rare), the next configured provider
    picks up with the already-completed tool call(s) handed over as plain
    context rather than re-executed, so a HubSpot write or a sent email
    never runs twice across a provider switch mid-turn.

    Raises HTTPException if no provider is configured, the message is
    empty, or every configured provider's request failed — callers that
    don't want that (e.g. the dashboard bridge, which just wants a reply
    string) should catch it.

    on_tool_event, if given, is called with a structured
    {"type": "tool_start"|"tool_end", "name": ..., ["ok": bool]} dict
    right before/after each individual tool call — the per-tool signal
    on_status's human-readable labels don't carry (on_status only knows
    "about to call a tool" and "done with this round," not which tool or
    whether it succeeded). Purely additive: existing on_status callers
    (dashboard_bridge.py, the /3d/phone relay) are unaffected — they never
    pass this and nothing here changes on_status's own behavior."""
    if not message.strip():
        raise HTTPException(status_code=400, detail="Empty message.")
    providers = _configured_providers()
    if not providers:
        raise HTTPException(
            status_code=503,
            detail=(
                "No AI provider is configured on this server "
                "(set GROQ_API_KEY, GEMINI_API_KEY, or ANTHROPIC_TOKEN)."
            ),
        )

    from core.headless.context import ToolContext
    from core.headless.tool_executor import ToolExecutor

    executor = ToolExecutor(ToolContext())
    tool_calls_made: list[dict] = []
    return await _run_provider_chain(providers, message, history, on_status, executor, tool_calls_made, on_tool_event)


async def _run_provider_chain(
    providers: list[str], message: str, history: list[dict],
    on_status: Callable[[str], Awaitable[None]] | None,
    executor: "ToolExecutor",  # noqa: F821 — imported by the caller before this runs
    tool_calls_made: list[dict],
    on_tool_event: Callable[[dict], Awaitable[None]] | None = None,
) -> tuple[str, list[dict]]:
    """Tries providers[0]; on failure, recurses into providers[1:] (which
    raises the final, clean HTTPException itself once the list is empty —
    see each _run_chat_turn_* function's own except clause). One place
    for the "next provider, or give up" decision instead of three copies
    of it."""
    runner = {
        "groq": _run_chat_turn_groq,
        "gemini": _run_chat_turn_gemini,
        "anthropic": _run_chat_turn_anthropic,
    }[providers[0]]
    return await runner(message, history, on_status, executor, tool_calls_made, providers[1:], on_tool_event)


async def _emit_tool_event(on_tool_event: Callable[[dict], Awaitable[None]] | None, event: dict) -> None:
    if on_tool_event is None:
        return
    try:
        await on_tool_event(event)
    except Exception:
        pass  # a broken event sink must never break the actual turn


async def _run_chat_turn_gemini(
    message: str, history: list[dict],
    on_status: Callable[[str], Awaitable[None]] | None,
    executor: "ToolExecutor",  # noqa: F821
    tool_calls_made: list[dict],
    remaining_providers: list[str],
    on_tool_event: Callable[[dict], Awaitable[None]] | None = None,
) -> tuple[str, list[dict]]:
    from google.genai import types as gtypes
    from core.headless.tool_executor import UnknownToolError
    from core.headless.gemini_client import get_client

    try:
        client = get_client(config.GEMINI_API_KEY)
    except Exception as e:
        if remaining_providers:
            return await _run_provider_chain(remaining_providers, message, history, on_status, executor, tool_calls_made, on_tool_event)
        raise HTTPException(status_code=502, detail=f"All configured AI providers failed. Last attempted: Gemini. Error: {e}")
    tools = [gtypes.Tool(function_declarations=_chat_tool_declarations())]
    gen_config = gtypes.GenerateContentConfig(system_instruction=_chat_system_prompt(), tools=tools)

    contents: list = []
    for turn in history[-20:]:
        role = "model" if turn.get("role") == "model" else "user"
        text = str(turn.get("text") or "")
        if text:
            contents.append(gtypes.Content(role=role, parts=[gtypes.Part(text=text)]))
    contents.append(gtypes.Content(role="user", parts=[gtypes.Part(text=message)]))
    if tool_calls_made:
        already_done = "\n".join(f"- {c['name']}({c['args']}) -> {c['result']}" for c in tool_calls_made)
        contents.append(gtypes.Content(role="user", parts=[gtypes.Part(text=(
            "[System note: before switching to you, the following tool call(s) "
            "already ran successfully and must NOT be repeated — use these real "
            f"results to answer:]\n{already_done}"
        ))]))

    loop = asyncio.get_event_loop()

    for round_num in range(_MAX_TOOL_CALL_ROUNDS):
        try:
            resp = await loop.run_in_executor(
                None, lambda: client.models.generate_content(model=CHAT_MODEL, contents=contents, config=gen_config)
            )
        except Exception as e:
            if remaining_providers:
                return await _run_provider_chain(remaining_providers, message, history, on_status, executor, tool_calls_made, on_tool_event)
            # Gemini's own API returns a clean, user-safe error message
            # (e.g. "high demand" 503s) — safe to surface directly, this
            # is never a secret-bearing string.
            raise HTTPException(status_code=502, detail=f"All configured AI providers failed. Last attempted: Gemini. Error: {e}")

        if not resp.function_calls:
            return resp.text or "", tool_calls_made

        contents.append(resp.candidates[0].content)
        for fc in resp.function_calls:
            args = dict(fc.args or {})
            if on_status is not None:
                try:
                    await on_status(_status_label_for_tool(fc.name, args))
                except Exception:
                    pass   # a broken status sink must never break the actual turn
            await _emit_tool_event(on_tool_event, {"type": "tool_start", "name": fc.name})
            try:
                result = await executor.execute(fc.name, args)
                tool_ok = True
            except UnknownToolError as e:
                result = f"Error: {e}"
                tool_ok = False
            except Exception as e:
                result = f"Error: {fc.name} failed — {e}"
                tool_ok = False
            await _emit_tool_event(on_tool_event, {"type": "tool_end", "name": fc.name, "ok": tool_ok})
            tool_calls_made.append({"name": fc.name, "args": args, "result": result})
            contents.append(gtypes.Content(
                role="user",
                parts=[gtypes.Part(function_response=gtypes.FunctionResponse(name=fc.name, response={"result": result}))],
            ))

        if on_status is not None and resp.function_calls:
            try:
                await on_status("Analyzing results...")
            except Exception:
                pass

    return (
        "I ran several tool calls but didn't reach a final answer — try rephrasing or breaking the request into smaller steps.",
        tool_calls_made,
    )


async def _run_chat_turn_groq(
    message: str, history: list[dict],
    on_status: Callable[[str], Awaitable[None]] | None,
    executor: "ToolExecutor",  # noqa: F821
    tool_calls_made: list[dict],
    remaining_providers: list[str],
    on_tool_event: Callable[[dict], Awaitable[None]] | None = None,
) -> tuple[str, list[dict]]:
    """Structurally parallel to _run_chat_turn_gemini/_run_chat_turn_anthropic
    but in OpenAI's chat-completions shape (tool_calls / role="tool" messages
    instead of function_calls/FunctionResponse or tool_use/tool_result)."""
    import json as _json
    from core.headless.tool_executor import UnknownToolError
    from core.headless.groq_client import get_client, gemini_tools_to_openai, CHAT_MODEL as GROQ_CHAT_MODEL

    try:
        client = get_client(config.GROQ_API_KEY)
    except Exception as e:
        if remaining_providers:
            return await _run_provider_chain(remaining_providers, message, history, on_status, executor, tool_calls_made, on_tool_event)
        raise HTTPException(status_code=502, detail=f"All configured AI providers failed. Last attempted: Groq. Error: {e}")
    tools = gemini_tools_to_openai(_chat_tool_declarations())

    messages: list[dict] = [{"role": "system", "content": _chat_system_prompt()}]
    # 10, not the 20 the other providers use — Groq's free tier caps at
    # 8,000 tokens/minute, so history gets a tighter budget here too (on
    # top of the tool-description trim above) to keep real headroom under
    # that limit rather than relying on the tool trim alone.
    for turn in history[-10:]:
        role = "assistant" if turn.get("role") == "model" else "user"
        text = str(turn.get("text") or "")
        if text:
            messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": message})
    if tool_calls_made:
        already_done = "\n".join(f"- {c['name']}({c['args']}) -> {c['result']}" for c in tool_calls_made)
        messages.append({
            "role": "user",
            "content": (
                "[System note: before switching to you, the following tool call(s) "
                "already ran successfully and must NOT be repeated — use these real "
                f"results to answer:]\n{already_done}"
            ),
        })

    loop = asyncio.get_event_loop()

    for _ in range(_MAX_TOOL_CALL_ROUNDS):
        try:
            resp = await loop.run_in_executor(
                None, lambda: client.chat.completions.create(
                    model=GROQ_CHAT_MODEL, messages=messages, tools=tools,
                )
            )
        except Exception as e:
            if remaining_providers:
                return await _run_provider_chain(remaining_providers, message, history, on_status, executor, tool_calls_made, on_tool_event)
            raise HTTPException(status_code=502, detail=f"All configured AI providers failed. Last attempted: Groq. Error: {e}")

        choice = resp.choices[0].message
        tool_calls = choice.tool_calls or []
        if not tool_calls:
            return choice.content or "", tool_calls_made

        messages.append({
            "role": "assistant", "content": choice.content,
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tool_calls
            ],
        })
        for tc in tool_calls:
            try:
                args = _json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            if on_status is not None:
                try:
                    await on_status(_status_label_for_tool(tc.function.name, args))
                except Exception:
                    pass
            await _emit_tool_event(on_tool_event, {"type": "tool_start", "name": tc.function.name})
            try:
                result = await executor.execute(tc.function.name, args)
                tool_ok = True
            except UnknownToolError as e:
                result = f"Error: {e}"
                tool_ok = False
            except Exception as e:
                result = f"Error: {tc.function.name} failed — {e}"
                tool_ok = False
            await _emit_tool_event(on_tool_event, {"type": "tool_end", "name": tc.function.name, "ok": tool_ok})
            tool_calls_made.append({"name": tc.function.name, "args": args, "result": result})
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})

        if on_status is not None and tool_calls:
            try:
                await on_status("Analyzing results...")
            except Exception:
                pass

    return (
        "I ran several tool calls but didn't reach a final answer — try rephrasing or breaking the request into smaller steps.",
        tool_calls_made,
    )


async def _run_chat_turn_anthropic(
    message: str, history: list[dict],
    on_status: Callable[[str], Awaitable[None]] | None,
    executor: "ToolExecutor",  # noqa: F821 — imported by the caller before this runs
    tool_calls_made: list[dict],
    remaining_providers: list[str],
    on_tool_event: Callable[[dict], Awaitable[None]] | None = None,
) -> tuple[str, list[dict]]:
    """Structurally parallel to _run_chat_turn_gemini's loop but in
    Anthropic's content-block shape (tool_use/tool_result instead of
    function_calls/FunctionResponse). Shares the caller's ToolExecutor and
    tool_calls_made list. Tool call(s) already completed by an earlier
    provider this turn are handed to Claude as plain completed-context (a
    "here's what already happened, don't repeat it" note) rather than
    re-executed — see run_chat_turn's docstring for why."""
    from core.headless.tool_executor import UnknownToolError
    from core.headless.anthropic_client import get_client, gemini_tools_to_anthropic, CHAT_MODEL as ANTHROPIC_CHAT_MODEL

    try:
        client = get_client(config.ANTHROPIC_TOKEN)
    except Exception as e:
        if remaining_providers:
            return await _run_provider_chain(remaining_providers, message, history, on_status, executor, tool_calls_made, on_tool_event)
        raise HTTPException(status_code=502, detail=f"All configured AI providers failed. Last attempted: Anthropic. Error: {e}")
    tools = gemini_tools_to_anthropic(_chat_tool_declarations())

    messages: list[dict] = []
    for turn in history[-20:]:
        role = "assistant" if turn.get("role") == "model" else "user"
        text = str(turn.get("text") or "")
        if text:
            messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": message})
    if tool_calls_made:
        already_done = "\n".join(f"- {c['name']}({c['args']}) -> {c['result']}" for c in tool_calls_made)
        messages.append({
            "role": "user",
            "content": (
                "[System note: before switching to you, the following tool call(s) "
                "already ran successfully and must NOT be repeated — use these real "
                f"results to answer:]\n{already_done}"
            ),
        })

    loop = asyncio.get_event_loop()

    for _ in range(_MAX_TOOL_CALL_ROUNDS):
        try:
            resp = await loop.run_in_executor(
                None, lambda: client.messages.create(
                    model=ANTHROPIC_CHAT_MODEL, max_tokens=2048,
                    system=_chat_system_prompt(), tools=tools, messages=messages,
                )
            )
        except Exception as e:
            if remaining_providers:
                return await _run_provider_chain(remaining_providers, message, history, on_status, executor, tool_calls_made, on_tool_event)
            raise HTTPException(status_code=502, detail=f"All configured AI providers failed. Last attempted: Anthropic. Error: {e}")

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            return "".join(b.text for b in resp.content if b.type == "text"), tool_calls_made

        messages.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for tu in tool_uses:
            args = dict(tu.input or {})
            if on_status is not None:
                try:
                    await on_status(_status_label_for_tool(tu.name, args))
                except Exception:
                    pass
            await _emit_tool_event(on_tool_event, {"type": "tool_start", "name": tu.name})
            try:
                result = await executor.execute(tu.name, args)
                tool_ok = True
            except UnknownToolError as e:
                result = f"Error: {e}"
                tool_ok = False
            except Exception as e:
                result = f"Error: {tu.name} failed — {e}"
                tool_ok = False
            await _emit_tool_event(on_tool_event, {"type": "tool_end", "name": tu.name, "ok": tool_ok})
            tool_calls_made.append({"name": tu.name, "args": args, "result": result})
            tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": str(result)})
        messages.append({"role": "user", "content": tool_results})

        if on_status is not None and tool_uses:
            try:
                await on_status("Analyzing results...")
            except Exception:
                pass

    return (
        "I ran several tool calls but didn't reach a final answer — try rephrasing or breaking the request into smaller steps.",
        tool_calls_made,
    )


@api.post("/chat")
async def chat(body: ChatRequest):
    """Streams the turn as Server-Sent Events instead of waiting for the
    whole (possibly multi-tool-call) turn to finish — the Command console
    sees "JARVIS -> Gmail" the moment that tool starts, not after the
    entire reply is ready. Smallest reliable change over the previous
    single-JSON-response shape: same route, same POST body, still one
    HTTP request/response, no new WebSocket or transport concept.
    run_chat_turn's own tool-dispatch/provider-fallback logic is
    unchanged — this only taps its existing, purely additive
    on_tool_event hook (see that function's docstring). dashboard_bridge.py
    (the /3d/phone relay) calls run_chat_turn directly and never touches
    this route, so /3d is untouched by this change.

    Frame shape (one JSON object per "data: " line, blank line between
    frames — standard SSE):
      {"type": "tool_start", "name": "gmail"}
      {"type": "tool_end", "name": "gmail", "ok": true}
      {"type": "done", "reply": "...", "tool_calls": [...]}   -- terminal
      {"type": "error", "detail": "..."}                       -- terminal
    """
    async def _events():
        queue: asyncio.Queue = asyncio.Queue()

        async def _on_tool_event(event: dict) -> None:
            await queue.put(event)

        async def _runner() -> None:
            try:
                reply, tool_calls = await run_chat_turn(body.message, body.history, on_tool_event=_on_tool_event)
                await queue.put({"type": "done", "reply": reply, "tool_calls": tool_calls})
            except HTTPException as e:
                await queue.put({"type": "error", "detail": e.detail})
            except Exception as e:
                await queue.put({"type": "error", "detail": str(e)})
            finally:
                await queue.put(None)  # sentinel: no more frames coming

        # Deliberately not cancelled if the client disconnects mid-turn
        # (tab closed, network drop): a HubSpot write or a sent email must
        # never be torn down mid-tool-call just because nobody's watching
        # the stream anymore. The task simply finishes on its own; nothing
        # after this generator exits is reading the queue, which is a
        # harmless no-op, not a leak (one bounded, small object).
        asyncio.create_task(_runner())
        while True:
            item = await queue.get()
            if item is None:
                break
            yield f"data: {json.dumps(item)}\n\n"

    return StreamingResponse(_events(), media_type="text/event-stream")


@api.get("/agents")
def ui_list_agents():
    return orchestrator_api.list_agents()


@api.post("/agents/{agent_id}/start")
def ui_start_agent(agent_id: str):
    return orchestrator_api.start_agent(agent_id)


@api.post("/agents/{agent_id}/stop")
def ui_stop_agent(agent_id: str):
    return orchestrator_api.stop_agent(agent_id)


@api.post("/tasks", status_code=201)
def ui_create_task(body: CreateTaskRequest):
    return orchestrator_api.create_task(body)


@api.get("/tasks")
def ui_list_tasks(agent_id: str | None = None, limit: int = 200):
    return orchestrator_api.list_tasks(agent_id=agent_id, limit=limit)


@api.get("/tasks/{task_id}")
def ui_get_task(task_id: str):
    return orchestrator_api.get_task(task_id)


@api.get("/tasks/{task_id}/result")
def ui_get_task_result(task_id: str):
    return orchestrator_api.get_task_result(task_id)


@api.post("/tasks/{task_id}/approve")
def ui_approve_task(task_id: str):
    return orchestrator_api.approve_task(task_id)


@api.post("/tasks/{task_id}/reject")
def ui_reject_task(task_id: str):
    return orchestrator_api.reject_task(task_id)


@api.post("/tasks/{task_id}/execute")
def ui_execute_task(task_id: str):
    return orchestrator_api.execute_task(task_id)


@api.get("/events")
def ui_list_events(agent_id: str | None = None, limit: int = 50):
    return orchestrator_api.list_events(agent_id=agent_id, limit=limit)


@api.get("/avatar/asset/{filename}")
def ui_avatar_asset(filename: str) -> FileResponse:
    """Serves a pre-generated avatar video (idle/speaking) — see
    FaceRenderer in index.html. Session-gated like the rest of /ui/api,
    matching the protection the raw reference photo had before it was
    replaced with these generated assets."""
    if filename not in _AVATAR_ASSETS:
        raise HTTPException(status_code=404, detail="not found")
    path = AVATAR_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type="video/mp4")


@router.get("")
@router.get("/")
def serve_index() -> FileResponse:
    """Serves the SPA shell at GET /ui (and /ui/ — browsers routinely hit
    both depending on how the link was typed/clicked).

    Phase 3 fix: this function existed, and its own docstring claimed
    "app.py wires GET / to this," but nothing actually decorated it as a
    route — /ui returned a bare 404. That's the literal reason this page
    was "a fallback surface nothing currently links to": the route to
    reach it didn't exist. GET / itself intentionally stays owned by
    dashboard/server.py's phone/3D command-center UI (see app.py's mount
    comment) — this page lives at /ui specifically, not at the bare
    public URL."""
    return FileResponse(INDEX_FILE, media_type="text/html")
