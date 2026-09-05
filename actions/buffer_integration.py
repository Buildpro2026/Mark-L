from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from core.headless.config import DATA_DIR

BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
DB_PATH = DATA_DIR / "jarvis2.db"

# Per-platform character limits, checked when a 'service' name is given --
# refuses early with a clear reason rather than letting the platform
# silently truncate or reject the post. Only checked when the caller
# names a service directly (not resolved from a raw channel_id, to avoid
# an extra network round-trip on every publish).
_PLATFORM_LIMITS = {
    "twitter": 280, "x": 280,
    "linkedin": 3000,
    "instagram": 2200,
    "facebook": 63206,
    "tiktok": 2200,
    "pinterest": 500,
    "threads": 500,
    "mastodon": 500,
    "bluesky": 300,
}

# Refuse to re-publish identical text to the same channel within this
# window unless allow_duplicate=True -- catches an accidental duplicate
# (e.g. a retried tool call) without blocking genuinely repeated content
# posted days apart.
_DUPLICATE_WINDOW_SECONDS = 24 * 60 * 60


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS buffer_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            text_hash TEXT NOT NULL,
            text_preview TEXT,
            buffer_id TEXT,
            published_ts REAL NOT NULL
        )
    """)
    return conn


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()


def _find_recent_duplicate(channel_id: str, text: str) -> dict[str, Any] | None:
    """Read-only: the most recent local record of this exact text having
    been published to this channel within the duplicate window, or None.
    Never raises -- a lookup failure degrades to 'no known duplicate'
    rather than blocking a publish that's otherwise valid."""
    try:
        conn = _connect()
        try:
            cutoff = time.time() - _DUPLICATE_WINDOW_SECONDS
            row = conn.execute(
                "SELECT * FROM buffer_posts WHERE channel_id = ? AND text_hash = ? AND published_ts >= ? "
                "ORDER BY published_ts DESC LIMIT 1",
                (channel_id, _text_hash(text), cutoff),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception:
        return None


def _record_published_post(channel_id: str, text: str, buffer_id: str | None) -> None:
    """Best-effort local publish history for future duplicate checks.
    Never raises -- this must not fail a publish that has already
    succeeded against the real Buffer API."""
    try:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO buffer_posts (channel_id, text_hash, text_preview, buffer_id, published_ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (channel_id, _text_hash(text), text[:200], buffer_id, time.time()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass

# Buffer's legacy REST API (api.bufferapp.com/1/...) explicitly rejects the
# token type stored in config/api_keys.json: "Public API tokens are not
# accepted for REST API access" (confirmed live). That token authenticates
# fine against Buffer's current GraphQL API instead, so verification,
# channel lookup, AND publishing (see publish_to_buffer below) all go
# through GRAPHQL_URL -- nothing in this module calls the REST API anymore.
GRAPHQL_URL = "https://api.buffer.com/graphql"


def get_buffer_token() -> str | None:
    from core.headless import config as _hc
    if _hc.BUFFER_TOKEN:
        return _hc.BUFFER_TOKEN
    if not CONFIG_PATH.exists():
        return None
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    token = str(data.get("buffer_token") or "").strip()
    return token or None


def _http_error_status(exc: "requests.HTTPError") -> tuple[str, str | None]:
    """Classifies a Buffer GraphQL transport-level HTTPError into a status
    string + optional actionable detail, shared by verify_buffer(),
    get_channels(), and publish_to_buffer() so all three surfaces describe
    the same failure the same way instead of three slightly different
    generic 'UNAVAILABLE:<code>' strings that read identically whether the
    token is merely rate-limited or genuinely dead.

    401/403 specifically mean Buffer itself rejected this token/request --
    not JARVIS's own gateway auth (which never returns 403, see
    dashboard/server.py's _3d_auth()) and not a rate limit (429, handled
    separately below). This is the one case worth telling a human to go
    check Buffer's own token/channel-connection state rather than treating
    it as a transient failure to retry."""
    code = exc.response.status_code if exc.response is not None else None
    if code in (401, 403):
        detail = (
            f"Buffer rejected this token with HTTP {code} -- the BUFFER_TOKEN is likely invalid, "
            "expired, revoked, or missing the scope this operation needs. Verify/regenerate it at "
            "https://publish.buffer.com/settings/api, or reconnect the channel in Buffer, then update "
            "the BUFFER_TOKEN environment variable. This is not a JARVIS-side bug."
        )
        return f"AUTH_FAILED:{code}", detail
    if code == 429:
        retry_after = None
        try:
            retry_after = exc.response.headers.get("Retry-After") if exc.response is not None else None
        except Exception:
            retry_after = None
        detail = "Buffer's API is rate-limiting this token right now -- not an authentication problem."
        if retry_after:
            detail += f" Retry after {retry_after}s."
        return "RATE_LIMITED:429", detail
    return f"UNAVAILABLE:{code if code is not None else '?'}", None


def _graphql(query: str, variables: dict[str, Any] | None = None, timeout: int = 20) -> dict[str, Any]:
    """POST a GraphQL query/mutation with the configured token. Raises on
    transport failure or a non-200 response; callers handle GraphQL-level
    `errors` in the returned payload themselves (a 200 can still carry
    errors, per GraphQL convention)."""
    token = get_buffer_token()
    if not token:
        raise RuntimeError("Buffer token not configured")
    resp = requests.post(
        GRAPHQL_URL,
        json={"query": query, "variables": variables or {}},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def verify_buffer() -> dict[str, Any]:
    """2026-09-03 finding: a live /3d Buffer/Social module open was
    returning "UNAVAILABLE:429" -- lumped in with every other failure
    under the generic UNAVAILABLE bucket, which reads exactly like a bad
    token even though it isn't one. A 429 here is Buffer's own GraphQL
    API rate-limiting this token (see get_channels()/
    discover_scheduling_capabilities() below, which each also call
    _graphql() -- three live calls can fire from a single module open,
    with zero caching on the caller side; dashboard/server.py's
    _module_social() now caches this result briefly for exactly that
    reason). Labelled RATE_LIMITED so the UI/health surface can say so
    honestly instead of showing a red "auth failed"-looking status for a
    token that's actually fine."""
    token = get_buffer_token()
    if not token:
        return {"configured": False, "authenticated": False, "verified": False, "functional": False, "status": "NOT CONFIGURED"}
    try:
        payload = _graphql("{ account { id name email } }")
        if payload.get("errors"):
            detail = payload["errors"][0].get("message", "unknown GraphQL error")
            return {"configured": True, "authenticated": False, "verified": False, "functional": False, "status": f"UNAVAILABLE:{detail}"}
        account = (payload.get("data") or {}).get("account")
        if account:
            return {"configured": True, "authenticated": True, "verified": True, "functional": True, "status": "VERIFIED", "data": account}
        return {"configured": True, "authenticated": False, "verified": False, "functional": False, "status": "UNAVAILABLE:empty response"}
    except requests.HTTPError as exc:
        status, detail = _http_error_status(exc)
        result = {"configured": True, "authenticated": False, "verified": False, "functional": False, "status": status}
        if detail:
            result["detail"] = detail
        return result
    except Exception as exc:
        return {"configured": True, "authenticated": False, "verified": False, "functional": False, "status": f"UNAVAILABLE:{exc}"}


def get_channels() -> dict[str, Any]:
    """Retrieve the Buffer channels ("profiles" in the old REST API's
    terms) this token can post to -- needed to supply a channel/profile ID
    when publishing. Looks up the account's organization ID first since
    the channels query requires one."""
    token = get_buffer_token()
    if not token:
        return {"configured": False, "channels": [], "status": "NOT CONFIGURED"}
    try:
        acct_payload = _graphql("{ account { organizations { id name } } }")
        if acct_payload.get("errors"):
            detail = acct_payload["errors"][0].get("message", "unknown GraphQL error")
            return {"configured": True, "channels": [], "status": f"UNAVAILABLE:{detail}"}
        orgs = ((acct_payload.get("data") or {}).get("account") or {}).get("organizations") or []
        if not orgs:
            return {"configured": True, "channels": [], "status": "UNAVAILABLE:no organization found for this account"}
        org_id = orgs[0]["id"]

        channels_payload = _graphql(
            """
            query($orgId: OrganizationId!) {
              channels(input: {organizationId: $orgId}) {
                id
                name
                displayName
                service
                type
                isDisconnected
              }
            }
            """,
            {"orgId": org_id},
        )
        if channels_payload.get("errors"):
            detail = channels_payload["errors"][0].get("message", "unknown GraphQL error")
            return {"configured": True, "channels": [], "status": f"UNAVAILABLE:{detail}"}
        channels = (channels_payload.get("data") or {}).get("channels") or []
        return {"configured": True, "channels": channels, "status": "VERIFIED", "organization_id": org_id}
    except requests.HTTPError as exc:
        status, detail = _http_error_status(exc)
        result = {"configured": True, "channels": [], "status": status}
        if detail:
            result["detail"] = detail
        return result
    except Exception as exc:
        return {"configured": True, "channels": [], "status": f"UNAVAILABLE:{exc}"}


def discover_scheduling_capabilities() -> dict[str, Any]:
    """Introspects Buffer's LIVE GraphQL schema (via this account's own
    already-authenticated token) to honestly report which scheduled-post
    operations are actually available -- never guessed, never assumed from
    Buffer's old REST-era docs. 'create' is already wired and working (see
    _CREATE_POST_MUTATION below, whose shape was itself discovered the same
    way). This answers the other half: can this token's schema retrieve,
    update, delete, or check the status of a scheduled post too?

    Every capability in the returned dict is derived strictly from field
    names __schema introspection actually reports for this account's
    Query/Mutation types -- a capability reads False if the matching field
    genuinely isn't there, not as a guess."""
    token = get_buffer_token()
    if not token:
        return {"configured": False, "status": "NOT CONFIGURED", "capabilities": {}}
    query = """
    {
      queryType: __type(name: "Query") { fields { name } }
      mutationType: __type(name: "Mutation") { fields { name } }
    }
    """
    try:
        payload = _graphql(query)
        if payload.get("errors"):
            detail = payload["errors"][0].get("message", "unknown GraphQL error")
            return {"configured": True, "status": f"UNAVAILABLE:{detail}", "capabilities": {}}
        data = payload.get("data") or {}
        query_fields = sorted({f["name"] for f in (data.get("queryType") or {}).get("fields") or []})
        mutation_fields = sorted({f["name"] for f in (data.get("mutationType") or {}).get("fields") or []})
        qf, mf = set(query_fields), set(mutation_fields)
        capabilities = {
            "create_post": "createPost" in mf,
            "retrieve_posts": bool(qf & {"posts", "post", "queue", "queuedPosts", "channelPosts"}),
            "update_post": bool(mf & {"updatePost", "editPost", "reschedulePost", "updatePostText"}),
            "delete_post": bool(mf & {"deletePost", "removePost", "cancelPost"}),
            "post_status_check": bool(qf & {"post", "posts"}),
        }
        return {
            "configured": True, "status": "VERIFIED",
            "capabilities": capabilities,
            "available_query_fields": query_fields,
            "available_mutation_fields": mutation_fields,
        }
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "?"
        return {"configured": True, "status": f"UNAVAILABLE:{code}", "capabilities": {}}
    except Exception as exc:
        return {"configured": True, "status": f"UNAVAILABLE:{exc}", "capabilities": {}}


_VALID_MODES = {"addToQueue", "shareNow", "shareNext", "customScheduled"}

# Schema discovered live via GraphQL introspection against this account's
# token (see GRAPHQL_URL) -- createPost's input/return shapes aren't
# documented anywhere in this codebase otherwise, so this mutation mirrors
# exactly what __schema introspection reported for CreatePostInput and the
# PostActionPayload union, rather than guessing at Buffer's REST-era shape.
_CREATE_POST_MUTATION = """
    mutation($input: CreatePostInput!) {
      createPost(input: $input) {
        __typename
        ... on PostActionSuccess { post { id channelId dueAt externalLink } }
        ... on InvalidInputError { message }
        ... on UnauthorizedError { message }
        ... on UnexpectedError { message }
        ... on LimitReachedError { message }
        ... on NotFoundError { message }
        ... on RestProxyError { message code }
      }
    }
"""


def resolve_channel_id(service: str) -> dict[str, Any]:
    """Looks up a connected channel's Buffer ID by service name (e.g.
    'linkedin', 'instagram', 'facebook', 'tiktok') via get_channels() --
    never hardcodes or guesses an ID. Skips disconnected channels."""
    result = get_channels()
    if result["status"] != "VERIFIED":
        return {"ok": False, "channel_id": None, "detail": result["status"]}
    matches = [
        c for c in result["channels"]
        if c["service"].lower() == service.strip().lower() and not c.get("isDisconnected")
    ]
    if not matches:
        return {"ok": False, "channel_id": None, "detail": f"No connected channel found for service '{service}'."}
    return {"ok": True, "channel_id": matches[0]["id"], "channel_name": matches[0]["displayName"]}


def publish_to_buffer(post: dict[str, Any], approved: bool = False) -> dict[str, Any]:
    """Publishes via Buffer's current GraphQL createPost mutation -- the
    legacy REST endpoint this used to call rejects this account's token
    entirely (see GRAPHQL_URL comment above), so there's no REST fallback.

    `post` must supply:
      - "text" or "caption": the post content (required, never fabricated)
      - "channel_id" (a real Buffer channel id), OR "service" (one of the
        connected channel service names -- resolved via resolve_channel_id());
        "service" also enables the platform character-limit check below.
    Optional: "link_url", "image_url" (attached as Buffer assets),
    "mode" -- defaults to "addToQueue" (adds to that channel's existing
    posting queue) rather than "shareNow", so calling this never posts
    immediately unless the caller explicitly asks for that. "allow_duplicate"
    bypasses the recent-duplicate check (see below) once a human has
    confirmed a repeat post is actually wanted.

    Input is validated (text/channel/mode/length/duplicate) BEFORE the
    configured/approved checks, so a caller always finds out what's wrong
    with their post regardless of Buffer's config state -- previously the
    NOT-CONFIGURED check ran first and masked every validation error.

    Requires approved=True to actually publish -- the same gate every
    other integration wired into JARVIS this session uses (Gmail send,
    Calendar create, Airtable/HubSpot writes). Calling with approved=False
    (the default) validates everything and returns a PREVIEW of exactly
    what would be posted, without publishing anything -- the
    preview-then-confirm workflow this function is designed around.
    """
    text = (post.get("text") or post.get("caption") or "").strip()
    if not text:
        return {"status": "ERROR:missing text", "published": False, "detail": "No post text/caption provided."}

    channel_id = post.get("channel_id")
    service = (post.get("service") or "").strip().lower() or None
    if not channel_id:
        if not service:
            return {"status": "ERROR:missing channel", "published": False, "detail": "Provide either channel_id or service."}
        resolved = resolve_channel_id(service)
        if not resolved["ok"]:
            return {"status": f"ERROR:{resolved['detail']}", "published": False}
        channel_id = resolved["channel_id"]

    mode = post.get("mode", "addToQueue")
    if mode not in _VALID_MODES:
        return {"status": f"ERROR:invalid mode {mode!r}", "published": False, "detail": f"Valid modes: {sorted(_VALID_MODES)}"}

    if service:
        limit = _PLATFORM_LIMITS.get(service)
        if limit and len(text) > limit:
            return {
                "status": f"ERROR:text exceeds {service} limit ({len(text)}/{limit} chars)",
                "published": False,
                "detail": f"{service} posts are limited to {limit} characters.",
            }

    duplicate = _find_recent_duplicate(channel_id, text)
    if duplicate and not post.get("allow_duplicate"):
        posted_at = datetime.fromtimestamp(duplicate["published_ts"], tz=timezone.utc).isoformat()
        return {
            "status": "ERROR:duplicate post", "published": False,
            "detail": f"Identical text was already posted to this channel at {posted_at}. "
                      "Pass allow_duplicate=True to post it again anyway.",
            "duplicate_of": duplicate.get("buffer_id"),
        }

    token = get_buffer_token()
    if not token:
        return {"status": "NOT CONFIGURED", "published": False}

    if not approved:
        return {
            "status": "PREVIEW", "published": False,
            "detail": "Validated -- call again with approved=True to actually publish.",
            "preview": {"channel_id": channel_id, "service": service, "text": text, "mode": mode},
        }

    assets = []
    if post.get("link_url"):
        assets.append({"link": {"url": post["link_url"]}})
    if post.get("image_url"):
        assets.append({"image": {"url": post["image_url"]}})

    variables = {
        "input": {
            "channelId": channel_id,
            "text": text,
            "assets": assets,
            "mode": mode,
            "needsApproval": False,
            "schedulingType": "automatic",
        }
    }
    try:
        payload = _graphql(_CREATE_POST_MUTATION, variables)
        if payload.get("errors"):
            detail = payload["errors"][0].get("message", "unknown GraphQL error")
            return {"status": f"UNAVAILABLE:{detail}", "published": False}
        result = (payload.get("data") or {}).get("createPost") or {}
        typename = result.get("__typename")
        if typename == "PostActionSuccess":
            post_data = result["post"]
            _record_published_post(channel_id, text, post_data.get("id"))
            return {
                "status": "PUBLISHED", "published": True, "buffer_id": post_data["id"],
                "due_at": post_data.get("dueAt"), "external_link": post_data.get("externalLink"),
            }
        return {"status": f"ERROR:{typename}", "published": False, "detail": result.get("message")}
    except requests.HTTPError as exc:
        status, detail = _http_error_status(exc)
        result = {"status": status, "published": False}
        if detail:
            result["detail"] = detail
        return result
    except Exception as exc:
        return {"status": f"UNAVAILABLE:{exc}", "published": False}
