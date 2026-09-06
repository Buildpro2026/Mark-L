"""Twilio Communications integration.

Config lives in config/api_keys.json under a "twilio" key:
    {"account_sid": "", "auth_token": "", "from_number": ""}
Never hardcoded — api_keys.json is gitignored and read at call time.

States (get_status/check_connection):
    NOT_CONFIGURED — no credentials in config
    CONFIGURED     — credentials present, live connectivity not verified
    CONNECTED      — credentials verified against Twilio's API just now
    ERROR          — a live check or operation failed

Permission model (OBSERVE -> SUGGEST -> EXECUTE), enforced by the caller
(main.py's communications tool — see its description):
    OBSERVE: get_status/check_connection/get_history/get_missed_calls/
             lookup_contact — always allowed, no side effects.
    EXECUTE: send_sms/place_call — real, consequential, cost real money.
             Only called after an explicit user instruction naming a
             recipient and content; never inferred.

This module NEVER fabricates a successful call/SMS. If Twilio isn't
configured, every action returns a NOT_CONFIGURED result instead of
pretending to succeed. Every attempt (successful or not) is logged to the
communications history table for a real audit trail.
"""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from core.headless.config import DATA_DIR

BASE_DIR = Path(__file__).resolve().parent.parent
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
DB_PATH = DATA_DIR / "jarvis2.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"

# Statuses that count as a genuinely missed inbound call.
_MISSED_CALL_STATUSES = {"no-answer", "busy", "failed", "missed"}


def _load_config() -> dict[str, Any]:
    import json
    try:
        return json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_twilio_config() -> dict[str, str]:
    from core.headless import config as _hc
    if _hc.TWILIO_ACCOUNT_SID and _hc.TWILIO_AUTH_TOKEN and _hc.TWILIO_FROM_NUMBER:
        return {
            "account_sid": _hc.TWILIO_ACCOUNT_SID,
            "auth_token": _hc.TWILIO_AUTH_TOKEN,
            "from_number": _hc.TWILIO_FROM_NUMBER,
        }
    cfg = (_load_config().get("twilio") or {})
    return {
        "account_sid": (cfg.get("account_sid") or "").strip(),
        "auth_token": (cfg.get("auth_token") or "").strip(),
        "from_number": (cfg.get("from_number") or "").strip(),
    }


def is_configured() -> bool:
    cfg = _load_twilio_config()
    return bool(cfg["account_sid"] and cfg["auth_token"] and cfg["from_number"])


def get_status() -> dict[str, Any]:
    """Cheap, no-network status check — NOT_CONFIGURED or CONFIGURED.
    Use check_connection() for a live CONNECTED/ERROR verification."""
    cfg = _load_twilio_config()
    if not is_configured():
        return {"state": "NOT_CONFIGURED", "detail": "No Twilio credentials configured.", "from_number": ""}
    return {
        "state": "CONFIGURED",
        "detail": "Twilio credentials are present (connectivity not yet verified).",
        "from_number": cfg["from_number"],
    }


def check_connection() -> dict[str, Any]:
    """Live diagnostic — fetches the Twilio account resource. Call
    explicitly (not on every status query) to avoid unnecessary API calls."""
    cfg = _load_twilio_config()
    if not is_configured():
        return {"state": "NOT_CONFIGURED", "detail": "No Twilio credentials configured."}
    import requests
    try:
        resp = requests.get(
            f"{TWILIO_API_BASE}/Accounts/{cfg['account_sid']}.json",
            auth=(cfg["account_sid"], cfg["auth_token"]),
            timeout=10,
        )
        if resp.status_code == 200:
            payload = resp.json()
            return {"state": "CONNECTED", "detail": "Twilio account verified.", "account_status": payload.get("status")}
        return {"state": "ERROR", "detail": f"Twilio returned HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"state": "ERROR", "detail": f"Connection check failed: {e}"}


# ── persistent communication history ────────────────────────────────────

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS communications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            direction   TEXT NOT NULL,   -- 'outbound' | 'inbound'
            kind        TEXT NOT NULL,   -- 'call' | 'sms'
            to_number   TEXT,
            from_number TEXT,
            body        TEXT,
            status      TEXT,            -- queued/sent/delivered/failed/completed/no-answer/...
            sid         TEXT,
            transcription TEXT,
            error       TEXT,
            ts          REAL NOT NULL
        )
    """)
    conn.commit()
    return conn


def _log(**fields) -> int:
    conn = _connect()
    defaults = {
        "to_number": None, "from_number": None, "body": None,
        "status": None, "sid": None, "transcription": None, "error": None,
    }
    row = {**defaults, **fields, "ts": time.time()}
    cur = conn.execute(
        "INSERT INTO communications "
        "(direction, kind, to_number, from_number, body, status, sid, transcription, error, ts) "
        "VALUES (:direction, :kind, :to_number, :from_number, :body, :status, :sid, :transcription, :error, :ts)",
        row,
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def update_by_sid(sid: str, **fields) -> None:
    """Used by status/transcription webhooks to update a previously-logged row."""
    if not sid or not fields:
        return
    conn = _connect()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE communications SET {set_clause} WHERE sid = ?", (*fields.values(), sid))
    conn.commit()
    conn.close()


def get_history(limit: int = 20, kind: str | None = None) -> list[dict[str, Any]]:
    conn = _connect()
    q = "SELECT * FROM communications"
    params: list[Any] = []
    if kind:
        q += " WHERE kind = ?"
        params.append(kind)
    q += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_missed_calls(limit: int = 10) -> list[dict[str, Any]]:
    conn = _connect()
    placeholders = ",".join("?" for _ in _MISSED_CALL_STATUSES)
    rows = conn.execute(
        f"SELECT * FROM communications WHERE kind='call' AND direction='inbound' "
        f"AND status IN ({placeholders}) ORDER BY ts DESC LIMIT ?",
        (*_MISSED_CALL_STATUSES, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── contacts (minimal — no dedicated contacts backend exists yet) ───────

_PHONE_RE = re.compile(r"^\+?[0-9][0-9\-\s()]{6,}$")


def lookup_contact(name_or_number: str) -> dict[str, Any]:
    """No dedicated contacts database exists in this codebase yet (see
    actions/send_message.py — it has no contact resolution either). If
    given something that already looks like a phone number, use it
    directly; otherwise report honestly that name resolution isn't
    available rather than guessing a number."""
    text = (name_or_number or "").strip()
    if _PHONE_RE.match(text):
        return {"resolved": True, "number": text, "source": "literal"}
    return {
        "resolved": False,
        "number": None,
        "source": None,
        "detail": f"No contacts database is configured — I don't have a number for '{text}'. Give me the phone number directly.",
    }


# ── outbound actions (EXECUTE — real, consequential) ────────────────────

def _xml_escape(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&apos;")
    )


def send_sms(to_number: str, body: str) -> dict[str, Any]:
    cfg = _load_twilio_config()
    if not is_configured():
        _log(direction="outbound", kind="sms", to_number=to_number, body=body,
             status="not_configured", error="Twilio not configured")
        return {"ok": False, "state": "NOT_CONFIGURED", "detail": "Twilio isn't configured — no SMS was sent."}

    import requests
    try:
        resp = requests.post(
            f"{TWILIO_API_BASE}/Accounts/{cfg['account_sid']}/Messages.json",
            auth=(cfg["account_sid"], cfg["auth_token"]),
            data={"To": to_number, "From": cfg["from_number"], "Body": body},
            timeout=15,
        )
        payload = resp.json()
        if resp.status_code in (200, 201):
            _log(direction="outbound", kind="sms", to_number=to_number, from_number=cfg["from_number"],
                 body=body, status=payload.get("status", "queued"), sid=payload.get("sid"))
            return {"ok": True, "state": "SENT", "sid": payload.get("sid"), "status": payload.get("status")}
        error = payload.get("message") or resp.text[:200]
        _log(direction="outbound", kind="sms", to_number=to_number, from_number=cfg["from_number"],
             body=body, status="failed", error=error)
        return {"ok": False, "state": "ERROR", "detail": error}
    except Exception as e:
        _log(direction="outbound", kind="sms", to_number=to_number, from_number=cfg["from_number"],
             body=body, status="failed", error=str(e))
        return {"ok": False, "state": "ERROR", "detail": str(e)}


def place_call(to_number: str, message: str = "") -> dict[str, Any]:
    """Places a call that speaks `message` via inline TwiML <Say> — the
    simplest reliable path, no separately-hosted TwiML endpoint required."""
    cfg = _load_twilio_config()
    if not is_configured():
        _log(direction="outbound", kind="call", to_number=to_number,
             status="not_configured", error="Twilio not configured")
        return {"ok": False, "state": "NOT_CONFIGURED", "detail": "Twilio isn't configured — no call was placed."}

    import requests
    twiml = f"<Response><Say>{_xml_escape(message or 'This is a call from Jarvis.')}</Say></Response>"
    try:
        resp = requests.post(
            f"{TWILIO_API_BASE}/Accounts/{cfg['account_sid']}/Calls.json",
            auth=(cfg["account_sid"], cfg["auth_token"]),
            data={"To": to_number, "From": cfg["from_number"], "Twiml": twiml},
            timeout=15,
        )
        payload = resp.json()
        if resp.status_code in (200, 201):
            _log(direction="outbound", kind="call", to_number=to_number, from_number=cfg["from_number"],
                 status=payload.get("status", "queued"), sid=payload.get("sid"))
            return {"ok": True, "state": "PLACED", "sid": payload.get("sid"), "status": payload.get("status")}
        error = payload.get("message") or resp.text[:200]
        _log(direction="outbound", kind="call", to_number=to_number, from_number=cfg["from_number"],
             status="failed", error=error)
        return {"ok": False, "state": "ERROR", "detail": error}
    except Exception as e:
        _log(direction="outbound", kind="call", to_number=to_number, from_number=cfg["from_number"],
             status="failed", error=str(e))
        return {"ok": False, "state": "ERROR", "detail": str(e)}


# ── inbound webhook helpers (called from dashboard/server.py) ───────────

def record_inbound_sms(from_number: str, to_number: str, body: str, sid: str) -> int:
    return _log(direction="inbound", kind="sms", to_number=to_number, from_number=from_number,
                body=body, status="received", sid=sid)


def record_inbound_call(from_number: str, to_number: str, sid: str, status: str = "ringing") -> int:
    return _log(direction="inbound", kind="call", to_number=to_number, from_number=from_number,
                status=status, sid=sid)


def voicemail_twiml() -> str:
    """TwiML for an inbound call: greet, then record a voicemail with
    transcription — the practical "voicemail/call-result handling" and
    "transcription hooks" this phase asks for, without needing a real-time
    bidirectional audio bridge into the Gemini Live session (out of scope
    for this session — see media_stream_placeholder below)."""
    return (
        "<Response>"
        "<Say>You've reached Jarvis. Please leave a message after the tone.</Say>"
        '<Record maxLength="120" transcribe="true" '
        'transcribeCallback="/twilio/transcription" '
        'recordingStatusCallback="/twilio/recording-status" playBeep="true" />'
        "</Response>"
    )
