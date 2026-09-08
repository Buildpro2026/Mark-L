"""Who actually wrote this email, when it arrived by forwarding.

THE BUG
info@buildprorecruiters.com forwards into Gmail. Depending on how the
forward is performed, the From header Gmail reports is the FORWARDING
address, not the person who wrote the message. Every identity path in
this codebase read message["sender"] and took it at face value —
candidate_intake, buildpro_client_intake, email_evidence, business_intent
and the draft recipient all did. So a candidate's application arrived,
JARVIS read the sender as info@buildprorecruiters.com, decided Lee was
the candidate, and drafted a welcome email addressed back to Lee.

The envelope tells you how a message was DELIVERED. It does not tell you
who WROTE it. Those are two different facts and this module keeps them
apart: original_sender and forwarder are separate fields, always, and
callers that care about identity ask for the original.

WHAT COUNTS AS EVIDENCE
Only a real, parseable address from a real forwarding structure:

  * Resent-From / X-Original-From / X-Original-Sender headers — set by
    forwarders precisely to preserve the author.
  * Reply-To, when it is a different mailbox from the forwarder. A
    forwarding alias that sets Reply-To is naming the original author.
  * The From: line inside a forwarded block in the body — Gmail's
    "---------- Forwarded message ---------", Outlook's "-----Original
    Message-----", and the Apple/Thunderbird "Begin forwarded message:".

A "Fwd:" subject with nothing parseable behind it is NOT evidence of who
sent it. It is evidence that we do not know. That case returns
determined=False with a reason, and the caller must treat the message as
UNKNOWN_NEEDS_REVIEW rather than falling back to the envelope — falling
back to the envelope IS the bug.

Nothing here guesses. An address is either extracted from the message or
it is absent.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

# Addresses that belong to Lee/JARVIS. A draft is never addressed to one
# of these on the strength of a forwarding envelope: mail that arrives
# through info@ was written by somebody else, by definition.
_DEFAULT_OWN_ADDRESSES = (
    "info@buildprorecruiters.com",
    "lchandler@buildprorecruiters.com",
)


def own_addresses() -> set[str]:
    """Lee's own mailboxes, lowercased. Extendable through
    JARVIS_OWN_EMAILS (comma-separated) without a code change."""
    configured = os.environ.get("JARVIS_OWN_EMAILS", "")
    extra = [a.strip().lower() for a in configured.split(",") if a.strip()]
    return {a.lower() for a in _DEFAULT_OWN_ADDRESSES} | set(extra)


def is_own_address(address: str) -> bool:
    return _email_of(address) in own_addresses()


_ADDR_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

# Forwarding block openers, in the formats real clients emit.
_FORWARD_BLOCK_RE = re.compile(
    r"(-{2,}\s*Forwarded message\s*-{2,}"
    r"|-{2,}\s*Original Message\s*-{2,}"
    r"|Begin forwarded message:"
    r"|^\s*>?\s*-{3,}\s*$)",
    re.I | re.M)

# The From: line inside such a block. Handles "From: Jane <j@x.com>",
# "From: j@x.com" and the quoted "> From: ..." of a plain-text reply.
_BLOCK_FROM_RE = re.compile(
    r"^\s*>*\s*(?:From|De|Von|Da)\s*:\s*(?P<value>.+)$", re.I | re.M)
_BLOCK_TO_RE = re.compile(
    r"^\s*>*\s*(?:To|Cc|Para|An)\s*:\s*(?P<value>.+)$", re.I | re.M)

_FWD_SUBJECT_RE = re.compile(r"^\s*(fwd?|fw|tr|wg|rv)\s*:", re.I)

# Headers a forwarder sets to preserve the original author. Ordered by how
# much they mean: an explicit original-sender header beats an inference.
_ORIGINAL_SENDER_HEADERS = (
    "x-original-from", "x-original-sender", "resent-from", "x-forwarded-from",
)
_FORWARDING_HEADERS = (
    "x-forwarded-for", "x-forwarded-to", "resent-to", "resent-sender",
    "x-original-to", "delivered-to",
)


def _headers(message: dict[str, Any]) -> dict[str, str]:
    raw = message.get("headers") or {}
    if isinstance(raw, list):
        return {str(h.get("name", "")).lower(): str(h.get("value", ""))
                for h in raw if isinstance(h, dict)}
    return {str(k).lower(): str(v) for k, v in raw.items()}


def _email_of(value: str) -> str:
    match = _ADDR_RE.search(value or "")
    return match.group(0).lower() if match else ""


def _display_of(value: str) -> str:
    """'Jane Doe <jane@x.com>' -> 'Jane Doe'; '' when there is no name."""
    text = (value or "").strip()
    if "<" in text:
        return text.split("<", 1)[0].strip().strip('"').strip()
    return ""


def _all_emails(value: str) -> list[str]:
    seen, out = set(), []
    for match in _ADDR_RE.finditer(value or ""):
        address = match.group(0).lower()
        if address not in seen:
            seen.add(address)
            out.append(address)
    return out


def _forwarded_block(body: str) -> str:
    """The text after the first forwarding marker, or ''. Only the block is
    searched for the original From, so a signature or a quoted address
    elsewhere in the body cannot be mistaken for the author."""
    match = _FORWARD_BLOCK_RE.search(body or "")
    return (body or "")[match.end():] if match else ""


def analyse(message: dict[str, Any]) -> dict[str, Any]:
    """Separate who wrote this from who forwarded it.

    Always returns both identities and never conflates them. When the
    original author cannot be established from real evidence,
    determined=False and `reason` says why — the caller must not fall back
    to the envelope sender, which is exactly how Lee became the candidate.
    """
    headers = _headers(message)
    envelope_raw = str(message.get("sender") or headers.get("from") or "")
    envelope_email = _email_of(envelope_raw)
    subject = str(message.get("subject") or headers.get("subject") or "")
    body = str(message.get("body") or "") + "\n" + str(message.get("snippet") or "")

    evidence: list[str] = []
    forwarded_markers: list[str] = []

    for header in _FORWARDING_HEADERS:
        if header in headers:
            forwarded_markers.append(f"header {header}")
    if _FWD_SUBJECT_RE.search(subject):
        forwarded_markers.append("subject carries a forward prefix")
    block = _forwarded_block(body)
    if block:
        forwarded_markers.append("body contains a forwarded-message block")
    # Mail relayed by one of Lee's own aliases is forwarded by definition.
    if envelope_email and envelope_email in own_addresses():
        forwarded_markers.append(f"envelope sender is our own address {envelope_email}")

    original_raw = ""
    source = ""

    # 1. A header set specifically to preserve the author. These headers
    #    exist only because something forwarded the message, so finding
    #    one is itself proof of forwarding.
    for header in _ORIGINAL_SENDER_HEADERS:
        value = headers.get(header, "")
        if _email_of(value):
            original_raw, source = value, f"header {header}"
            forwarded_markers.append(f"header {header}")
            break

    is_forwarded = bool(forwarded_markers)

    # The remaining two signals only mean "original sender" INSIDE a
    # forward. Outside one they mean something else entirely, and reading
    # them as authorship invents a forward that never happened: plenty of
    # ordinary senders set a Reply-To on a different mailbox (GitHub
    # notifications route replies to reply.github.com), and a quoted
    # "From:" line appears in any reply thread.
    if is_forwarded:
        # 2. The From: line inside the forwarded block.
        if not original_raw and block:
            match = _BLOCK_FROM_RE.search(block)
            if match and _email_of(match.group("value")):
                original_raw, source = match.group("value").strip(), "forwarded-block From line"

        # 3. Reply-To, but only when it names a different mailbox than the
        #    forwarder. A Reply-To equal to the envelope tells us nothing.
        if not original_raw:
            reply_to = headers.get("reply-to", "") or str(message.get("reply_to") or "")
            reply_email = _email_of(reply_to)
            if reply_email and reply_email != envelope_email and reply_email not in own_addresses():
                original_raw, source = reply_to, "Reply-To names a different mailbox"
    original_email = _email_of(original_raw)

    # An "original sender" that is one of our own addresses is not the
    # author — it is the forward looping back on itself.
    if original_email and original_email in own_addresses():
        evidence.append(f"discarded {original_email} as the author: it is our own address")
        original_raw, original_email, source = "", "", ""

    # A direct message: the envelope IS the author, and that is the normal
    # case that must keep working exactly as before.
    if not is_forwarded:
        return _result(
            is_forwarded=False, determined=bool(envelope_email),
            original_raw=envelope_raw, original_email=envelope_email,
            forwarder_raw="", forwarder_email="",
            recipients=_recipients(message, headers, block),
            confidence=1.0 if envelope_email else 0.0,
            evidence=["direct message; the envelope sender is the author"] if envelope_email else [],
            reason="" if envelope_email else "no parsable sender address on the message",
            markers=[])

    if not original_email:
        return _result(
            is_forwarded=True, determined=False,
            original_raw="", original_email="",
            forwarder_raw=envelope_raw, forwarder_email=envelope_email,
            recipients=_recipients(message, headers, block),
            confidence=0.0, evidence=evidence,
            reason=("this message was forwarded but carries no recoverable original "
                    "sender — no Resent-From/X-Original-From header, no From line in a "
                    "forwarded block, and no distinct Reply-To"),
            markers=forwarded_markers)

    evidence.append(f"original sender from {source}")
    # A header the forwarder set explicitly is stronger evidence than a
    # line parsed out of body text a human could have typed.
    confidence = 0.95 if source.startswith("header") else (
        0.85 if "forwarded-block" in source else 0.75)

    return _result(
        is_forwarded=True, determined=True,
        original_raw=original_raw, original_email=original_email,
        forwarder_raw=envelope_raw, forwarder_email=envelope_email,
        recipients=_recipients(message, headers, block),
        confidence=confidence, evidence=evidence, reason="",
        markers=forwarded_markers)


def _recipients(message: dict[str, Any], headers: dict[str, str], block: str) -> list[str]:
    """Everyone the message was addressed to, original recipients from the
    forwarded block included, with our own addresses removed — those are
    the forwarding hop, not correspondents."""
    values = [str(message.get("to") or headers.get("to") or ""),
              str(message.get("cc") or headers.get("cc") or "")]
    if block:
        for match in _BLOCK_TO_RE.finditer(block):
            values.append(match.group("value"))
    ours = own_addresses()
    out: list[str] = []
    for value in values:
        for address in _all_emails(value):
            if address not in ours and address not in out:
                out.append(address)
    return out


def _result(**kw) -> dict[str, Any]:
    return {
        "is_forwarded": kw["is_forwarded"],
        "determined": kw["determined"],
        "original_sender": kw["original_raw"],
        "original_sender_email": kw["original_email"],
        "original_sender_name": _display_of(kw["original_raw"]),
        "forwarder": kw["forwarder_raw"],
        "forwarder_email": kw["forwarder_email"],
        "original_recipients": kw["recipients"],
        "confidence": round(float(kw["confidence"]), 2),
        "evidence": kw["evidence"],
        "forwarding_markers": kw["markers"],
        "reason": kw["reason"],
    }


def effective_sender(message: dict[str, Any]) -> str:
    """The address every identity decision should use — the author, not
    the envelope. Empty when the author is not known, and empty must be
    treated as "do not act", never as a reason to use the envelope."""
    analysis = analyse(message)
    return analysis["original_sender"] if analysis["determined"] else ""


def resolve_sender(message: dict[str, Any]) -> dict[str, Any]:
    """A copy of the message with `sender` set to the ORIGINAL author, plus
    the forwarding facts alongside it.

    Returning a copy rather than mutating means an undetermined forward
    cannot silently keep the envelope in `sender` — the caller has to look
    at `forwarding.determined` to get a usable address at all."""
    analysis = analyse(message)
    resolved = dict(message)
    resolved["forwarding"] = analysis
    resolved["forwarder"] = analysis["forwarder"] or message.get("sender") or ""
    if analysis["determined"]:
        resolved["sender"] = analysis["original_sender"]
        resolved["sender_email"] = analysis["original_sender_email"]
    else:
        # Deliberately blanked. Leaving the envelope here is the defect.
        resolved["sender"] = ""
        resolved["sender_email"] = ""
    return resolved


def safe_reply_address(message: dict[str, Any]) -> tuple[str, str]:
    """(address, reason). The address a reply may be sent to, or '' with
    the reason it is being withheld.

    Three refusals, each one a bug this module exists to prevent:
    an unknown author, an author that is one of our own addresses, and a
    forward whose original sender could not be recovered."""
    analysis = analyse(message)
    if not analysis["determined"]:
        return "", (analysis["reason"] or "the original sender could not be determined")
    address = analysis["original_sender_email"]
    if not address:
        return "", "no parsable address for the original sender"
    if address in own_addresses():
        return "", f"{address} is our own address, not a correspondent"
    return address, "original sender identified"
