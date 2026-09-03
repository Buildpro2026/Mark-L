"""Structured verification records (Lee's autonomous-CEO/COS spec, Section
ELEVENTH: "'API returned success' is not enough"). Every important
autonomous action the CEO operating cycle takes is recorded through
record_verification() with: the intended action, the actual action
attempted, the provider's own response, any resulting external id, an
explicit success/failure verdict, and whether follow-up is required.

Layered on top of actions/audit_log.py rather than replacing it —
audit_log is already the durable append-only log every consequential
action in this codebase writes to (gmail sends, calendar events, HubSpot
writes, Buffer posts, DDF publishes). This module doesn't introduce a
second log; it standardizes the specific fields Section ELEVENTH asks for
as one structured payload on that same log, and returns a small dict the
caller (ceo_operating_cycle.py) uses to decide whether to create a
follow-up item."""
from __future__ import annotations

from typing import Any, Optional

from actions import audit_log


def record_verification(
    action: str,
    *,
    intended: str,
    actual: str,
    success: bool,
    provider_response: Any = None,
    reference_id: Optional[str] = None,
    external_system: Optional[str] = None,
    follow_up_required: bool = False,
    follow_up_reason: str = "",
    actor: str = "ceo_operating_cycle",
) -> dict[str, Any]:
    """Never raises — verification bookkeeping must not be able to break
    the operating cycle that's calling it. A logging failure degrades to
    the returned dict still being correct; only the durable audit_log row
    is best-effort (audit_log.record() already guarantees that itself)."""
    verification_status = "verified_success" if success else "verified_failure"
    payload = {
        "intended_action": intended,
        "actual_action": actual,
        "provider_response": provider_response,
        "reference_id": reference_id,
        "success": success,
        "verification_status": verification_status,
        "follow_up_required": follow_up_required,
        "follow_up_reason": follow_up_reason,
    }
    try:
        row_id = audit_log.record(
            action,
            actor=actor,
            execution_status="succeeded" if success else "failed",
            result=payload,
            error=None if success else (follow_up_reason or "verification failed"),
            external_system=external_system,
            reference_id=reference_id,
        )
    except Exception:
        row_id = -1
    payload["audit_log_id"] = row_id
    return payload
