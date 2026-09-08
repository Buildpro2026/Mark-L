"""Retire business records the old email classifier created in error.

gmail_integration.classify_message() used to return the first keyword rule
that matched anywhere in subject+sender+snippet+body, and its
client_inquiry rule is ("project", "quote", "bid", "estimate",
"proposal"). Any notification whose body mentioned a project therefore
became a client_inquiry, and buildpro_email_monitor logged a business
intelligence entry for it — titled "[Email:client_inquiry] ..." with the
machine's address recorded as the sender.

Those entries are still sitting in the store, still representing GitHub
and other platforms as business leads. The precedence gate stops new ones;
this retires the ones already written.

TWO RULES, BOTH LOAD-BEARING

  * Nothing is deleted. Ever. An entry is superseded by a correction
    entry that records the original id, the original sender, and why the
    original was wrong. The audit trail is the point — a record that
    quietly vanishes is indistinguishable from one that never existed,
    and Lee cannot check work he cannot see.

  * Only entries whose sender is CONCLUSIVELY a machine are touched.
    email_evidence.sender_identity() has to say MACHINE or BULK on the
    stored sender address. An entry whose sender cannot be re-evaluated —
    no address recorded, or a human address — is left exactly as it is.
    A borderline case stays; a false cleanup destroys a real lead, which
    is worse than leaving a wrong one for a human to spot.

dry_run defaults to True. Nothing writes until a caller asks it to.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from actions import business_intelligence as biz
from actions import email_evidence

logger = logging.getLogger("jarvis.email_action_cleanup")

# The two labels the old rules could wrongly produce for a machine sender.
_SUSPECT_TITLE_MARKERS = ("[Email:client_inquiry]", "[Email:candidate_reply]")

CORRECTION_TITLE = "[Correction] Misclassified automated email"


def _stored_sender(entry: dict[str, Any]) -> str:
    """The sender recorded on a BI entry, from either the structured data
    blob or the 'From: ...' line the monitor writes into content."""
    data = entry.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = {}
    if isinstance(data, dict):
        sender = data.get("sender") or data.get("from")
        if sender:
            return str(sender)
    content = entry.get("content") or ""
    for line in content.splitlines():
        if line.lower().startswith("from:"):
            return line.split(":", 1)[1].strip()
    return ""


def find_false_business_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Entries that represent a machine sender as a client or candidate.

    Pure function over rows so it can be tested without a database, and so
    the decision is auditable independently of the write path."""
    suspects: list[dict[str, Any]] = []
    for entry in entries or []:
        title = entry.get("title") or ""
        if not any(marker in title for marker in _SUSPECT_TITLE_MARKERS):
            continue
        sender = _stored_sender(entry)
        if not sender:
            continue  # cannot re-evaluate it, so leave it alone
        identity = email_evidence.sender_identity({"sender": sender})
        if identity["kind"] not in (email_evidence.SENDER_MACHINE,
                                    email_evidence.SENDER_BULK):
            continue
        suspects.append({
            "id": entry.get("id"),
            "title": title,
            "sender": sender,
            "sender_kind": identity["kind"],
            "reason": ("classified from body keywords while the sender is an "
                       "automated system: " + "; ".join(identity["evidence"][:3])),
        })
    return suspects


def clean_up(dry_run: bool = True, business: str = "buildpro") -> dict[str, Any]:
    """Find, and optionally supersede, falsely-derived business entries.

    Returns the full list either way, so a dry run shows exactly what a
    real run would do."""
    try:
        entries = biz.list_entries(business=business, limit=1000)
    except TypeError:
        entries = biz.list_entries(business=business)
    except Exception as exc:
        return {"ok": False, "detail": str(exc)[:300], "found": [], "corrected": 0}

    suspects = find_false_business_entries(entries or [])
    if dry_run or not suspects:
        return {"ok": True, "dry_run": dry_run, "found": suspects,
                "corrected": 0,
                "summary": (f"{len(suspects)} entr(ies) misrepresent an automated sender "
                            f"as a client or candidate." if suspects else
                            "No falsely-derived business entries found.")}

    corrected = 0
    for suspect in suspects:
        try:
            biz.add_entry(
                category="research", business=business,
                title=CORRECTION_TITLE,
                content=(f"Superseded entry #{suspect['id']}: {suspect['title']}\n"
                         f"Sender: {suspect['sender']} ({suspect['sender_kind']})\n"
                         f"Reason: {suspect['reason']}\n"
                         f"The original entry is retained for audit; it must no longer be "
                         f"treated as a client or candidate."),
                data={"supersedes": suspect["id"], "sender": suspect["sender"],
                      "sender_kind": suspect["sender_kind"],
                      "correction": "misclassified_automated_sender"},
                related_id=suspect["id"],
            )
            corrected += 1
        except Exception:
            logger.debug("could not record a correction for entry %s",
                         suspect.get("id"), exc_info=True)

    return {"ok": True, "dry_run": False, "found": suspects, "corrected": corrected,
            "summary": (f"{corrected} correction entr(ies) recorded. No original record "
                        f"was deleted — each is superseded and kept for audit.")}
