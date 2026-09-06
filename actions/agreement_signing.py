"""Representation-agreement e-signature — a minimal, self-hosted "click to
accept" flow instead of a paid e-signature vendor (DocuSign etc. need
their own account/credentials Lee doesn't have wired yet).

Legal basis: the US ESIGN Act / UETA don't require a specific signature
technology — they require clear intent to sign, association of that
intent with the specific document, and a retained record. A typed full
name + timestamp + IP address, tied to the specific agreement text shown
at signing time, satisfies that. This is not the same polish as a
commercial e-signature product, but it is a real, legally-usable
signature, not a placeholder gesture.

AGREEMENT_TEXT below is a DRAFT, not reviewed legal language — see the
warning on it. Nothing in this module is legal advice; Lee (or a lawyer)
must review and replace it before a real candidate ever sees this page.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from core.headless.config import DATA_DIR

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = DATA_DIR / "jarvis2.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# ⚠️ PLACEHOLDER TEXT — NOT REVIEWED LEGAL LANGUAGE. ⚠️
# This is a reasonable-sounding draft so the intake pipeline has something
# real to show while it's being built and tested. Lee (or BuildPro's
# lawyer) must review and replace this before any real candidate signs it.
AGREEMENT_TEXT = """BUILDPRO RECRUITERS — CANDIDATE REPRESENTATION AGREEMENT (DRAFT)

This agreement is between BuildPro Recruiters ("BuildPro") and the candidate named below.

1. NO COST TO CANDIDATE. BuildPro's placement fee is paid entirely by the
   hiring employer. The candidate will never be charged a fee for
   BuildPro's services under this agreement.

2. SCOPE OF REPRESENTATION. BuildPro will present the candidate's
   qualifications to prospective employers in BuildPro's network,
   advocate on the candidate's behalf during the hiring process, and
   assist with scheduling and offer negotiation when an opportunity
   advances.

3. CANDIDATE COOPERATION. The candidate agrees to provide accurate
   information about their qualifications, experience, and availability,
   and to communicate promptly about their interest (or lack of interest)
   in opportunities BuildPro presents.

4. NO OBLIGATION TO ACCEPT. The candidate is under no obligation to
   accept any opportunity BuildPro presents, and may end this
   relationship with BuildPro at any time by written notice.

5. CONFIDENTIALITY. BuildPro will not share the candidate's information
   with a specific employer without the candidate's consent.

By typing your full legal name below and clicking "Sign Agreement," you
confirm that you have read and agree to the terms above, and that your
typed name constitutes your electronic signature on this agreement.
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candidate_agreements (
            token TEXT PRIMARY KEY,
            candidate_id INTEGER,
            candidate_name TEXT,
            candidate_email TEXT,
            agreement_text TEXT,
            created_ts REAL,
            signed_ts REAL,
            signed_name TEXT,
            signer_ip TEXT
        )
    """)
    return conn


def create_pending_agreement(candidate_id: int | None, candidate_name: str, candidate_email: str) -> str:
    """Snapshots AGREEMENT_TEXT at creation time into the row itself — if
    the text is ever updated later, an already-sent link still shows and
    records exactly what that candidate was actually asked to sign."""
    token = uuid.uuid4().hex
    conn = _connect()
    conn.execute(
        "INSERT INTO candidate_agreements "
        "(token, candidate_id, candidate_name, candidate_email, agreement_text, created_ts) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (token, candidate_id, candidate_name, candidate_email, AGREEMENT_TEXT, time.time()),
    )
    conn.commit()
    conn.close()
    return token


def get_agreement(token: str) -> dict[str, Any] | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM candidate_agreements WHERE token = ?", (token,)).fetchone()
    conn.close()
    return dict(row) if row else None


def sign_agreement(token: str, signed_name: str, signer_ip: str) -> dict[str, Any]:
    """Idempotent: signing an already-signed link again just confirms the
    existing signature rather than overwriting it (the record of what was
    first agreed to, and when, must not change after the fact)."""
    agreement = get_agreement(token)
    if agreement is None:
        return {"ok": False, "detail": "Unknown or expired agreement link."}
    if agreement.get("signed_ts"):
        return {"ok": True, "already_signed": True, "signed_ts": agreement["signed_ts"], "signed_name": agreement["signed_name"]}
    signed_name = (signed_name or "").strip()
    if not signed_name:
        return {"ok": False, "detail": "A typed full name is required to sign."}
    conn = _connect()
    conn.execute(
        "UPDATE candidate_agreements SET signed_ts = ?, signed_name = ?, signer_ip = ? WHERE token = ?",
        (time.time(), signed_name, signer_ip, token),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "already_signed": False, "signed_name": signed_name}
