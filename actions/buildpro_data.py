"""BuildPro Recruiting data foundation.

Persistent data model for actual recruiting operations: candidates,
clients/employers, jobs/openings, and candidate-to-job matches. Reuses the
project's existing SQLite database (data/jarvis2.db) and the same
_connect()/row-factory pattern as actions/twilio_integration.py and
actions/business_intelligence.py, rather than introducing new storage
infrastructure.

This is intentionally separate from actions/business_intelligence.py's
generic bi_entries table — that table is unstructured notes/outcomes
(research, decisions, lessons learned); this module is the structured CRM-
style record set (a candidate has an email and a stage; a match has a score
and links two specific rows) that the 3D Command Center's BuildPro ->
Candidates/Clients/Jobs nodes need real fields to display.

Every add_/update_ function returns the row id / affected-row confirmation;
nothing here calls out to HubSpot directly (see actions/hubspot_integration.py
for that) — hubspot_contact_id / hubspot_company_id columns, the
*_by_hubspot_id() lookups, and the sync-run tracking below exist so
actions/buildpro_sync.py can link/dedupe local records against HubSpot ones
without this module needing to know anything about HubSpot's API.

Matching fields (title/specialty/years_experience/skills/location/
compensation/availability on candidates; specialty/min_years_experience/
required_skills/compensation/employment_type on jobs) back the scoring logic
in actions/buildpro_matching.py — they're nullable and never guessed at by
this module; callers leave them unset when the information isn't known.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from core.headless.config import DATA_DIR

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = DATA_DIR / "jarvis2.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

CANDIDATE_STATUSES = {"new", "screening", "submitted", "interviewing", "placed", "rejected", "withdrawn"}
CLIENT_STATUSES = {"prospect", "active", "inactive"}
JOB_STATUSES = {"open", "on_hold", "filled", "closed"}
MATCH_STATUSES = {"proposed", "submitted", "interviewing", "rejected", "placed"}
AVAILABILITY_VALUES = {"available", "employed", "not_looking", "unknown"}

# A match at/above this score counts as "qualified" for summary() and the
# Command Center's qualified-match count — picked as a reasonable default
# threshold, not derived from any external spec.
QUALIFIED_MATCH_SCORE = 70.0

# A candidate/client in an "open" stage that hasn't been touched in this
# many days is surfaced as needing follow-up — a transparent, documented
# proxy for "needs attention" since there's no separate last-contacted
# field; see list_candidates_needing_followup / list_clients_needing_followup.
FOLLOWUP_STALE_DAYS = 7.0

# Statuses that still represent open/active work-in-progress — used to scope
# the follow-up queries above (a placed/rejected/withdrawn candidate going
# stale isn't something that needs follow-up).
_CANDIDATE_OPEN_STATUSES = {"new", "screening", "submitted", "interviewing"}
_CLIENT_OPEN_STATUSES = {"prospect", "active"}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS buildpro_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT,
            phone TEXT,
            source TEXT,
            status TEXT NOT NULL DEFAULT 'new',
            hubspot_contact_id TEXT,
            notes TEXT,
            created_ts REAL NOT NULL,
            updated_ts REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS buildpro_clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            contact_name TEXT,
            email TEXT,
            phone TEXT,
            source TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            hubspot_company_id TEXT,
            notes TEXT,
            created_ts REAL NOT NULL,
            updated_ts REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS buildpro_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER,
            title TEXT NOT NULL,
            description TEXT,
            location TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            source TEXT,
            created_ts REAL NOT NULL,
            updated_ts REAL NOT NULL,
            FOREIGN KEY (client_id) REFERENCES buildpro_clients(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS buildpro_matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL,
            job_id INTEGER NOT NULL,
            match_score REAL,
            match_rationale TEXT,
            status TEXT NOT NULL DEFAULT 'proposed',
            created_ts REAL NOT NULL,
            updated_ts REAL NOT NULL,
            FOREIGN KEY (candidate_id) REFERENCES buildpro_candidates(id),
            FOREIGN KEY (job_id) REFERENCES buildpro_jobs(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS buildpro_sync_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            source TEXT NOT NULL,
            created_count INTEGER NOT NULL DEFAULT 0,
            updated_count INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            errors TEXT,
            ts REAL NOT NULL
        )
    """)
    # 2026-09-02 reliability audit finding: the candidate/client intake
    # handlers used to gate their Gmail search on `is:unread` as their ONLY
    # "don't reprocess this message" mechanism. That silently and
    # permanently excluded any candidate email ever opened by anyone with
    # mailbox access (Lee included) — real resumes sat unseen forever. The
    # fix widens the Gmail query, so this table becomes the real dedup
    # mechanism: one row per (message_id, kind) the intake chain has
    # actually run against, so widening the search can't cause the same
    # message to create duplicate HubSpot notes/welcome drafts on every
    # scheduled re-scan.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS buildpro_processed_messages (
            message_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            processed_ts REAL NOT NULL,
            PRIMARY KEY (message_id, kind)
        )
    """)
    # Additive-only migration for installs that created these tables before
    # the matching/sync columns existed — ALTER TABLE ADD COLUMN is cheap
    # and each call is guarded individually so one already-present column
    # doesn't stop the rest from being added.
    _ensure_columns(conn, "buildpro_candidates", {
        "title": "TEXT", "specialty": "TEXT", "years_experience": "INTEGER",
        "skills": "TEXT", "location": "TEXT", "desired_compensation": "TEXT",
        "availability": "TEXT", "last_synced_ts": "REAL",
    })
    _ensure_columns(conn, "buildpro_clients", {
        "industry": "TEXT", "last_synced_ts": "REAL",
    })
    # 2026-09-03 provenance columns (Lee's autonomous-CEO spec, Section 11:
    # "NO ORPHAN RECORDS"). Additive-only, same _ensure_columns pattern as
    # every migration above — existing installs pick these up with no
    # manual step. company_id is the domain-isolation tag (Section 10);
    # the rest trace exactly which email this record came from, so every
    # candidate/client can be opened back to its real source message.
    # A record with no real source (e.g. the HubSpot bulk sync) leaves
    # these columns NULL rather than a fabricated value — display code
    # must show "SOURCE UNAVAILABLE" for a NULL/blank source_url, never
    # invent one.
    _PROVENANCE_COLUMNS = {
        "company_id": "TEXT", "source_system": "TEXT", "source_record_id": "TEXT",
        "source_url": "TEXT", "gmail_message_id": "TEXT", "gmail_thread_id": "TEXT",
        "captured_ts": "REAL",
    }
    _ensure_columns(conn, "buildpro_candidates", _PROVENANCE_COLUMNS)
    _ensure_columns(conn, "buildpro_clients", _PROVENANCE_COLUMNS)
    _ensure_columns(conn, "buildpro_jobs", {
        "specialty": "TEXT", "min_years_experience": "INTEGER",
        "required_skills": "TEXT", "compensation": "TEXT", "employment_type": "TEXT",
    })
    conn.commit()
    return conn


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, col_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


def _rows(rs: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rs]


def _update(table: str, row_id: int, fields: dict[str, Any]) -> bool:
    """Shared dynamic-UPDATE helper — used by all update_* functions below
    so each one only has to say which columns are legal for it."""
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        return False
    fields["updated_ts"] = time.time()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    conn = _connect()
    cur = conn.execute(f"UPDATE {table} SET {set_clause} WHERE id = ?", (*fields.values(), row_id))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def split_keywords(text: str | None) -> list[str]:
    """'welding, osha-30,  crane operation' -> ['welding','osha-30','crane operation']
    — shared by skills matching (buildpro_matching.py) and search filters
    below. Never invents keywords; just normalizes what's stored."""
    if not text:
        return []
    return [t.strip().lower() for t in text.split(",") if t.strip()]


# ── Candidates ───────────────────────────────────────────────────────────

def add_candidate(
    name: str, email: str = "", phone: str = "", source: str = "",
    status: str = "new", hubspot_contact_id: str = "", notes: str = "",
    title: str = "", specialty: str = "", years_experience: int | None = None,
    skills: str = "", location: str = "", desired_compensation: str = "",
    availability: str = "", last_synced_ts: float | None = None,
    company_id: str = "", source_system: str = "", source_record_id: str = "",
    source_url: str = "", gmail_message_id: str = "", gmail_thread_id: str = "",
    captured_ts: float | None = None,
) -> int:
    if status not in CANDIDATE_STATUSES:
        raise ValueError(f"Unknown candidate status: {status!r}. Valid: {sorted(CANDIDATE_STATUSES)}")
    if availability and availability not in AVAILABILITY_VALUES:
        raise ValueError(f"Unknown availability: {availability!r}. Valid: {sorted(AVAILABILITY_VALUES)}")
    now = time.time()
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO buildpro_candidates "
        "(name, email, phone, source, status, hubspot_contact_id, notes, "
        " title, specialty, years_experience, skills, location, desired_compensation, "
        " availability, last_synced_ts, "
        " company_id, source_system, source_record_id, source_url, gmail_message_id, gmail_thread_id, captured_ts, "
        " created_ts, updated_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, email or None, phone or None, source or None, status,
         hubspot_contact_id or None, notes or None,
         title or None, specialty or None, years_experience, skills or None,
         location or None, desired_compensation or None, availability or None,
         last_synced_ts,
         company_id or None, source_system or None, source_record_id or None,
         source_url or None, gmail_message_id or None, gmail_thread_id or None,
         captured_ts or (now if (gmail_message_id or source_system) else None),
         now, now),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def get_candidate(candidate_id: int) -> dict[str, Any] | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM buildpro_candidates WHERE id = ?", (candidate_id,)).fetchone()
    conn.close()
    return _row(row)


def get_candidate_by_hubspot_id(hubspot_contact_id: str) -> dict[str, Any] | None:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM buildpro_candidates WHERE hubspot_contact_id = ?", (hubspot_contact_id,)
    ).fetchone()
    conn.close()
    return _row(row)


def find_candidate_by_email(email: str) -> dict[str, Any] | None:
    """Case-insensitive exact match — the dedup key for upsert_candidate()."""
    if not email:
        return None
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM buildpro_candidates WHERE LOWER(email) = LOWER(?)", (email.strip(),)
    ).fetchone()
    conn.close()
    return _row(row)


def upsert_candidate(name: str, email: str = "", **fields: Any) -> tuple[int, str]:
    """Idempotent create-or-update by email: adding the same candidate
    twice (e.g. a manual re-entry, or a voice command run twice) updates
    the existing row instead of creating a duplicate. Candidates without
    an email can't be deduped this way — add_candidate() always inserts
    for those, same as before; this wrapper only changes behavior when an
    email is actually given. Returns (candidate_id, "created"|"updated")."""
    if email:
        existing = find_candidate_by_email(email)
        if existing:
            update_fields = {k: v for k, v in fields.items() if v not in (None, "")}
            if name and name != existing.get("name"):
                update_fields["name"] = name
            if update_fields:
                update_candidate(existing["id"], **update_fields)
            return existing["id"], "updated"
    return add_candidate(name, email=email, **fields), "created"


def list_candidates(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    conn = _connect()
    q = "SELECT * FROM buildpro_candidates"
    params: list[Any] = []
    if status:
        q += " WHERE status = ?"
        params.append(status)
    q += " ORDER BY updated_ts DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return _rows(rows)


def find_candidates(
    skills: str | None = None, location: str | None = None, title: str | None = None,
    min_years_experience: int | None = None, status: str | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    """Filtered candidate search backing the BuildPro Candidate Agent's
    'identify candidates by skills, experience, location, role, and status'
    requirement. Every filter is optional and ANDed together; skills/title/
    location are case-insensitive substring matches against stored text —
    there's no fabrication here, just SQL LIKE over whatever was recorded."""
    conn = _connect()
    q = "SELECT * FROM buildpro_candidates WHERE 1=1"
    params: list[Any] = []
    if skills:
        q += " AND LOWER(IFNULL(skills, '')) LIKE ?"
        params.append(f"%{skills.strip().lower()}%")
    if location:
        q += " AND LOWER(IFNULL(location, '')) LIKE ?"
        params.append(f"%{location.strip().lower()}%")
    if title:
        q += " AND LOWER(IFNULL(title, '')) LIKE ?"
        params.append(f"%{title.strip().lower()}%")
    if min_years_experience is not None:
        q += " AND years_experience IS NOT NULL AND years_experience >= ?"
        params.append(min_years_experience)
    if status:
        q += " AND status = ?"
        params.append(status)
    q += " ORDER BY updated_ts DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return _rows(rows)


def list_candidates_needing_followup(days: float = FOLLOWUP_STALE_DAYS, limit: int = 20) -> list[dict[str, Any]]:
    """Candidates in an open stage (new/screening/submitted/interviewing)
    not updated in `days` — a transparent, documented proxy for 'needs
    attention' (no separate last-contacted field exists to track that more
    precisely)."""
    cutoff = time.time() - (days * 86400)
    conn = _connect()
    placeholders = ",".join("?" for _ in _CANDIDATE_OPEN_STATUSES)
    rows = conn.execute(
        f"SELECT * FROM buildpro_candidates WHERE status IN ({placeholders}) AND updated_ts < ? "
        f"ORDER BY updated_ts ASC LIMIT ?",
        (*_CANDIDATE_OPEN_STATUSES, cutoff, limit),
    ).fetchall()
    conn.close()
    return _rows(rows)


def update_candidate(candidate_id: int, **fields: Any) -> bool:
    if "status" in fields and fields["status"] not in CANDIDATE_STATUSES:
        raise ValueError(f"Unknown candidate status: {fields['status']!r}. Valid: {sorted(CANDIDATE_STATUSES)}")
    if fields.get("availability") and fields["availability"] not in AVAILABILITY_VALUES:
        raise ValueError(f"Unknown availability: {fields['availability']!r}. Valid: {sorted(AVAILABILITY_VALUES)}")
    allowed = {
        "name", "email", "phone", "source", "status", "hubspot_contact_id", "notes",
        "title", "specialty", "years_experience", "skills", "location",
        "desired_compensation", "availability", "last_synced_ts",
        "company_id", "source_system", "source_record_id", "source_url",
        "gmail_message_id", "gmail_thread_id", "captured_ts",
    }
    return _update("buildpro_candidates", candidate_id, {k: v for k, v in fields.items() if k in allowed})


# ── Clients / employers ──────────────────────────────────────────────────

def add_client(
    name: str, contact_name: str = "", email: str = "", phone: str = "",
    source: str = "", status: str = "active", hubspot_company_id: str = "", notes: str = "",
    industry: str = "", last_synced_ts: float | None = None,
    company_id: str = "", source_system: str = "", source_record_id: str = "",
    source_url: str = "", gmail_message_id: str = "", gmail_thread_id: str = "",
    captured_ts: float | None = None,
) -> int:
    if status not in CLIENT_STATUSES:
        raise ValueError(f"Unknown client status: {status!r}. Valid: {sorted(CLIENT_STATUSES)}")
    now = time.time()
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO buildpro_clients "
        "(name, contact_name, email, phone, source, status, hubspot_company_id, notes, "
        " industry, last_synced_ts, "
        " company_id, source_system, source_record_id, source_url, gmail_message_id, gmail_thread_id, captured_ts, "
        " created_ts, updated_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, contact_name or None, email or None, phone or None, source or None,
         status, hubspot_company_id or None, notes or None, industry or None,
         last_synced_ts,
         company_id or None, source_system or None, source_record_id or None,
         source_url or None, gmail_message_id or None, gmail_thread_id or None,
         captured_ts or (now if (gmail_message_id or source_system) else None),
         now, now),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def get_client(client_id: int) -> dict[str, Any] | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM buildpro_clients WHERE id = ?", (client_id,)).fetchone()
    conn.close()
    return _row(row)


def get_client_by_hubspot_id(hubspot_company_id: str) -> dict[str, Any] | None:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM buildpro_clients WHERE hubspot_company_id = ?", (hubspot_company_id,)
    ).fetchone()
    conn.close()
    return _row(row)


def find_client_by_email(email: str) -> dict[str, Any] | None:
    """Case-insensitive exact match — the dedup key for upsert_client()."""
    if not email:
        return None
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM buildpro_clients WHERE LOWER(email) = LOWER(?)", (email.strip(),)
    ).fetchone()
    conn.close()
    return _row(row)


def upsert_client(name: str, email: str = "", **fields: Any) -> tuple[int, str]:
    """Idempotent create-or-update by email — same pattern as
    upsert_candidate(). Clients without an email can't be deduped this
    way; add_client() always inserts for those. Returns
    (client_id, "created"|"updated")."""
    if email:
        existing = find_client_by_email(email)
        if existing:
            update_fields = {k: v for k, v in fields.items() if v not in (None, "")}
            if name and name != existing.get("name"):
                update_fields["name"] = name
            if update_fields:
                update_client(existing["id"], **update_fields)
            return existing["id"], "updated"
    return add_client(name, email=email, **fields), "created"


def list_clients(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    conn = _connect()
    q = "SELECT * FROM buildpro_clients"
    params: list[Any] = []
    if status:
        q += " WHERE status = ?"
        params.append(status)
    q += " ORDER BY updated_ts DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return _rows(rows)


def list_clients_needing_followup(days: float = FOLLOWUP_STALE_DAYS, limit: int = 20) -> list[dict[str, Any]]:
    """Same proxy as list_candidates_needing_followup, scoped to
    prospect/active clients that have gone stale."""
    cutoff = time.time() - (days * 86400)
    conn = _connect()
    placeholders = ",".join("?" for _ in _CLIENT_OPEN_STATUSES)
    rows = conn.execute(
        f"SELECT * FROM buildpro_clients WHERE status IN ({placeholders}) AND updated_ts < ? "
        f"ORDER BY updated_ts ASC LIMIT ?",
        (*_CLIENT_OPEN_STATUSES, cutoff, limit),
    ).fetchall()
    conn.close()
    return _rows(rows)


def update_client(client_id: int, **fields: Any) -> bool:
    if "status" in fields and fields["status"] not in CLIENT_STATUSES:
        raise ValueError(f"Unknown client status: {fields['status']!r}. Valid: {sorted(CLIENT_STATUSES)}")
    allowed = {
        "name", "contact_name", "email", "phone", "source", "status",
        "hubspot_company_id", "notes", "industry", "last_synced_ts",
        "company_id", "source_system", "source_record_id", "source_url",
        "gmail_message_id", "gmail_thread_id", "captured_ts",
    }
    return _update("buildpro_clients", client_id, {k: v for k, v in fields.items() if k in allowed})


# ── Jobs / openings ──────────────────────────────────────────────────────

def add_job(
    title: str, client_id: int | None = None, description: str = "",
    location: str = "", status: str = "open", source: str = "",
    specialty: str = "", min_years_experience: int | None = None,
    required_skills: str = "", compensation: str = "", employment_type: str = "",
) -> int:
    if status not in JOB_STATUSES:
        raise ValueError(f"Unknown job status: {status!r}. Valid: {sorted(JOB_STATUSES)}")
    now = time.time()
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO buildpro_jobs "
        "(client_id, title, description, location, status, source, "
        " specialty, min_years_experience, required_skills, compensation, employment_type, "
        " created_ts, updated_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (client_id, title, description or None, location or None, status, source or None,
         specialty or None, min_years_experience, required_skills or None,
         compensation or None, employment_type or None, now, now),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def get_job(job_id: int) -> dict[str, Any] | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM buildpro_jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return _row(row)


def list_jobs(
    status: str | None = None, client_id: int | None = None,
    created_after: float | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    conn = _connect()
    q = "SELECT * FROM buildpro_jobs WHERE 1=1"
    params: list[Any] = []
    if status:
        q += " AND status = ?"
        params.append(status)
    if client_id is not None:
        q += " AND client_id = ?"
        params.append(client_id)
    if created_after is not None:
        q += " AND created_ts >= ?"
        params.append(created_after)
        q += " ORDER BY created_ts DESC LIMIT ?"
    else:
        q += " ORDER BY updated_ts DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return _rows(rows)


def list_unmatched_open_jobs(limit: int = 10) -> list[dict[str, Any]]:
    """Open jobs with zero recorded matches — a real gap signal (nobody's
    been scored against this job yet) used by the morning recruiting
    intelligence report's recommended actions."""
    conn = _connect()
    rows = conn.execute(
        "SELECT j.* FROM buildpro_jobs j "
        "LEFT JOIN buildpro_matches m ON m.job_id = j.id "
        "WHERE j.status = 'open' AND m.id IS NULL "
        "ORDER BY j.created_ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return _rows(rows)


def top_matches(limit: int = 5) -> list[dict[str, Any]]:
    """Highest-scoring candidate/job matches with candidate/job names
    attached (via a join) for readability — used by both summary() and
    actions/buildpro_intelligence.py so the join lives in one place."""
    conn = _connect()
    rows = conn.execute(
        "SELECT m.id, m.match_score, m.match_rationale, m.status, m.updated_ts, "
        "       c.id AS candidate_id, c.name AS candidate_name, "
        "       j.id AS job_id, j.title AS job_title "
        "FROM buildpro_matches m "
        "JOIN buildpro_candidates c ON c.id = m.candidate_id "
        "JOIN buildpro_jobs j ON j.id = m.job_id "
        "WHERE m.match_score IS NOT NULL "
        "ORDER BY m.match_score DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return _rows(rows)


def update_job(job_id: int, **fields: Any) -> bool:
    if "status" in fields and fields["status"] not in JOB_STATUSES:
        raise ValueError(f"Unknown job status: {fields['status']!r}. Valid: {sorted(JOB_STATUSES)}")
    allowed = {
        "client_id", "title", "description", "location", "status", "source",
        "specialty", "min_years_experience", "required_skills", "compensation", "employment_type",
    }
    return _update("buildpro_jobs", job_id, {k: v for k, v in fields.items() if k in allowed})


# ── Candidate-to-job matches ─────────────────────────────────────────────

def add_match(
    candidate_id: int, job_id: int, match_score: float | None = None,
    match_rationale: str = "", status: str = "proposed",
) -> int:
    if status not in MATCH_STATUSES:
        raise ValueError(f"Unknown match status: {status!r}. Valid: {sorted(MATCH_STATUSES)}")
    now = time.time()
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO buildpro_matches "
        "(candidate_id, job_id, match_score, match_rationale, status, created_ts, updated_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (candidate_id, job_id, match_score, match_rationale or None, status, now, now),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def get_match(match_id: int) -> dict[str, Any] | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM buildpro_matches WHERE id = ?", (match_id,)).fetchone()
    conn.close()
    return _row(row)


def find_match(candidate_id: int, job_id: int) -> dict[str, Any] | None:
    """Look up an existing match for this exact candidate/job pair — used
    by the matching engine to update rather than duplicate a match when
    re-scored."""
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM buildpro_matches WHERE candidate_id = ? AND job_id = ?", (candidate_id, job_id)
    ).fetchone()
    conn.close()
    return _row(row)


def list_matches(
    candidate_id: int | None = None, job_id: int | None = None,
    status: str | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    conn = _connect()
    q = "SELECT * FROM buildpro_matches WHERE 1=1"
    params: list[Any] = []
    if candidate_id is not None:
        q += " AND candidate_id = ?"
        params.append(candidate_id)
    if job_id is not None:
        q += " AND job_id = ?"
        params.append(job_id)
    if status:
        q += " AND status = ?"
        params.append(status)
    q += " ORDER BY match_score DESC, updated_ts DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return _rows(rows)


def update_match(match_id: int, **fields: Any) -> bool:
    if "status" in fields and fields["status"] not in MATCH_STATUSES:
        raise ValueError(f"Unknown match status: {fields['status']!r}. Valid: {sorted(MATCH_STATUSES)}")
    allowed = {"match_score", "match_rationale", "status"}
    return _update("buildpro_matches", match_id, {k: v for k, v in fields.items() if k in allowed})


# ── Sync run tracking (actions/buildpro_sync.py) ─────────────────────────

def record_sync_run(
    entity_type: str, source: str, created_count: int = 0,
    updated_count: int = 0, error_count: int = 0, errors: list[str] | None = None,
) -> int:
    import json as _json
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO buildpro_sync_runs "
        "(entity_type, source, created_count, updated_count, error_count, errors, ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (entity_type, source, created_count, updated_count, error_count,
         _json.dumps(errors or []), time.time()),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def get_last_sync(entity_type: str) -> dict[str, Any] | None:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM buildpro_sync_runs WHERE entity_type = ? ORDER BY ts DESC LIMIT 1",
        (entity_type,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    d = _row(row)
    import json as _json
    try:
        d["errors"] = _json.loads(d["errors"]) if d["errors"] else []
    except Exception:
        d["errors"] = []
    return d


def list_sync_runs(entity_type: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    conn = _connect()
    q = "SELECT * FROM buildpro_sync_runs"
    params: list[Any] = []
    if entity_type:
        q += " WHERE entity_type = ?"
        params.append(entity_type)
    q += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return _rows(rows)


# ── Email-intake idempotency ─────────────────────────────────────────────
# Real dedup for the candidate/client intake chain — see the
# buildpro_processed_messages table comment above for why this exists.
# `kind` keeps candidate and client intake independent (the same message
# could in principle be relevant to both scans without one blocking the
# other), and lets a future intake type reuse this table for its own dedup
# key rather than inventing a fourth storage mechanism.

def is_message_processed(message_id: str, kind: str) -> bool:
    if not message_id:
        return False
    conn = _connect()
    row = conn.execute(
        "SELECT 1 FROM buildpro_processed_messages WHERE message_id = ? AND kind = ?",
        (message_id, kind),
    ).fetchone()
    conn.close()
    return row is not None


def mark_message_processed(message_id: str, kind: str) -> None:
    if not message_id:
        return
    conn = _connect()
    conn.execute(
        "INSERT OR REPLACE INTO buildpro_processed_messages (message_id, kind, processed_ts) VALUES (?, ?, ?)",
        (message_id, kind, time.time()),
    )
    conn.commit()
    conn.close()


# ── Command Center summary ───────────────────────────────────────────────

def summary() -> dict[str, Any]:
    """Compact counts + highlights consumed by the 3D Command Center's
    BuildPro nucleus (candidate/client/job counts, prospects, qualified
    matches, highest match scores, follow-ups, recent activity) — same role
    as business_intelligence.summary() plays for the generic BI panels."""
    conn = _connect()
    candidate_count = conn.execute("SELECT COUNT(*) FROM buildpro_candidates").fetchone()[0]
    client_count = conn.execute(
        "SELECT COUNT(*) FROM buildpro_clients WHERE status != 'prospect'"
    ).fetchone()[0]
    prospect_count = conn.execute(
        "SELECT COUNT(*) FROM buildpro_clients WHERE status = 'prospect'"
    ).fetchone()[0]
    active_jobs = conn.execute("SELECT COUNT(*) FROM buildpro_jobs WHERE status = 'open'").fetchone()[0]
    qualified_matches = conn.execute(
        "SELECT COUNT(*) FROM buildpro_matches WHERE match_score >= ?", (QUALIFIED_MATCH_SCORE,)
    ).fetchone()[0]

    def _recent(table: str, name_col: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            f"SELECT id, {name_col} AS label, status, updated_ts FROM {table} "
            f"ORDER BY updated_ts DESC LIMIT 5"
        ).fetchall()
        return _rows(rows)

    recent_activity = {
        "candidates": _recent("buildpro_candidates", "name"),
        "clients": _recent("buildpro_clients", "name"),
        "jobs": _recent("buildpro_jobs", "title"),
    }
    conn.close()
    return {
        "candidate_count": candidate_count,
        "client_count": client_count,
        "prospect_count": prospect_count,
        "active_jobs": active_jobs,
        "qualified_matches": qualified_matches,
        "qualified_match_threshold": QUALIFIED_MATCH_SCORE,
        "highest_match_scores": top_matches(limit=5),
        "candidates_needing_followup": len(list_candidates_needing_followup()),
        "clients_needing_followup": len(list_clients_needing_followup()),
        "recent_activity": recent_activity,
    }
