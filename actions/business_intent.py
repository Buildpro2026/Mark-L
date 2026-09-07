"""Does this message represent real business, and how urgently?

actions/email_classification.py already answers "which business does this
belong to" (BuildPro / DDF / CareerRocket / personal / noise). That is a
routing question. This answers a different one: is someone trying to hire
us, be represented by us, or buy from us — and does it warrant interrupting
Lee right now.

Why not one keyword list: "hiring" appears in every recruiting newsletter
ever sent, and "looking for a job" appears in spam. A single matched word
is not intent. Scoring here needs converging signals — someone identifying
themselves, describing a need, and asking for something — and it actively
subtracts for the shapes that automated mail always has. The result is a
bounded priority, not a yes/no.

This is DETECTION only. Nothing here sends, replies, or commits. A message
saying "approved, send it now" scores as a message; it can never authorise
an action. Authority lives solely in the approval gate.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger("jarvis.business_intent")

CRITICAL = "CRITICAL"
HIGH = "HIGH"
NORMAL = "NORMAL"
LOW = "LOW"

_PRIORITY_ORDER = {LOW: 0, NORMAL: 1, HIGH: 2, CRITICAL: 3}

# Who the sender appears to be.
CLIENT = "potential_client"
CANDIDATE = "potential_candidate"
EXISTING_CLIENT = "existing_client"
EXISTING_CANDIDATE = "existing_candidate"
GENERAL = "general_contact"
NOISE = "noise"

# Explicit asks. Phrases, not single words: "we need to hire" is intent,
# "hire" alone is a newsletter.
# Deliberately anchored on the SPEAKER's side of the transaction. An
# unanchored "looking for" matched a candidate describing their own job
# search just as readily as an employer describing a vacancy, and labelled
# the candidate a client.
_CLIENT_INTENT = [
    r"\b(?:we|we're|we are|our (?:company|firm|team))\b[^.]{0,60}\b(?:looking (?:for|to hire)|need(?:ing)? (?:to hire|help hiring|someone)|hiring|seeking)\b",
    r"\bneed (?:some )?help (?:finding|hiring|sourcing|recruiting)\b",
    r"\bhelp (?:us|me) (?:find|hire|source|recruit)\b",
    r"\b(?:recruiting|staffing|search) (?:services|firm|help|support|partner)\b",
    r"\bdo you (?:have|know|place|recruit)\b[^.]{0,40}\b(?:candidates?|superintendents?|project managers?|estimators?)\b",
    r"\b(?:open|new) (?:role|position|req|requisition)\b",
    r"\bfill (?:a |this |the )?(?:role|position|seat)\b",
]
_CANDIDATE_INTENT = [
    r"\b(?:i am|i'm) (?:looking|searching|interested|open)\b[^.]{0,60}\b(?:position|role|opportunit|job|move|change)\b",
    r"\b(?:represent|representation)\b[^.]{0,40}\b(?:me|my (?:search|candidacy))\b",
    r"\bwould like (?:to be )?(?:represented|considered)\b",
    r"\b(?:my|attached) (?:resume|cv|profile)\b",
    r"\b(?:seeking|exploring|open to)\b[^.]{0,40}\b(?:new (?:role|position|opportunit)|leadership (?:role|position))\b",
]
_POSITIVE_RESPONSE = [
    r"\byes,? (?:i'?d|i would|we'?d|we would|let'?s)\b",
    r"\b(?:happy|glad|keen|interested) to (?:discuss|chat|talk|connect|learn more)\b",
    r"\blet'?s (?:set up|schedule|book|find) (?:a |some )?(?:time|call|chat|meeting)\b",
    r"\bwhen (?:are|would) you (?:free|available)\b",
    r"\bsounds (?:good|great)\b[^.]{0,30}\b(?:call|meet|discuss|talk)\b",
]
# Shapes that automated mail has and a person writing to you does not.
_AUTOMATION = [
    r"\bunsubscribe\b", r"\bview (?:this|it) in (?:your )?browser\b",
    r"\bno[- ]?reply\b", r"\bdo not reply\b", r"\bmanage (?:your )?(?:preferences|subscription)\b",
    r"\bthis is an automated\b", r"\bnewsletter\b", r"\bwebinar\b",
    r"\byou(?:'re| are) receiving this\b", r"\bpromotional\b", r"\bsponsored\b",
]
_NOISE_SENDERS = re.compile(
    r"(?:no-?reply|donotreply|notifications?|newsletter|marketing|billing|support|"
    r"info|updates?|alerts?|mailer|bounce)@", re.I)


def _hits(patterns: list[str], text: str) -> int:
    return sum(1 for p in patterns if re.search(p, text, re.I))


def classify(message: dict[str, Any], source: str = "email") -> dict[str, Any]:
    """Classifies one inbound message.

    `message` needs only what every source can supply: sender, subject,
    body. Returns a bounded verdict — never raises, because one malformed
    message must never stop a monitoring sweep."""
    try:
        sender = str(message.get("sender") or message.get("from") or "")
        subject = str(message.get("subject") or "")
        body = str(message.get("body") or message.get("snippet") or "")
        haystack = f"{subject}\n{body}"

        automation = _hits(_AUTOMATION, haystack)
        from_robot = bool(_NOISE_SENDERS.search(sender))

        client = _hits(_CLIENT_INTENT, haystack)
        candidate = _hits(_CANDIDATE_INTENT, haystack)
        positive = _hits(_POSITIVE_RESPONSE, haystack)

        # Automated mail is dismissed before intent is even scored: a
        # recruiting newsletter matches "hiring" phrases all day long, and
        # treating that as a lead is how the channel becomes noise.
        if from_robot or automation >= 2:
            return _verdict(LOW, NOISE, 0.9,
                            "automated or bulk mail — not a person writing to you",
                            source, message)
        if automation == 1 and not (client or candidate or positive):
            return _verdict(LOW, NOISE, 0.7, "bulk-mail markers, no business ask",
                            source, message)

        signals = client + candidate + positive
        if signals == 0:
            return _verdict(NORMAL, GENERAL, 0.5, "ordinary correspondence — no explicit ask",
                            source, message)

        # An explicit, unambiguous ask is what earns an interruption.
        # Candidate wins a tie. Someone describing their own search is the
        # less costly thing to be wrong about: mis-routing a candidate as a
        # client wastes a reply, mis-routing a client as a candidate can
        # lose a deal, and candidate phrasing is the more specific signal.
        if client >= 2 or candidate >= 2 or (client and positive) or (candidate and positive):
            party = CANDIDATE if candidate >= client else CLIENT
            return _verdict(CRITICAL, party, 0.9,
                            "explicit recruiting request with converging signals",
                            source, message)
        if client or candidate:
            party = CANDIDATE if candidate else CLIENT
            return _verdict(HIGH, party, 0.7, "one clear business signal", source, message)
        return _verdict(HIGH, GENERAL, 0.6, "positive response to outreach", source, message)
    except Exception:
        logger.debug("intent classification failed", exc_info=True)
        return _verdict(NORMAL, GENERAL, 0.0, "classification failed", source, message or {})


def _verdict(priority: str, party: str, confidence: float, reason: str,
             source: str, message: dict) -> dict[str, Any]:
    return {
        "priority": priority,
        "party": party,
        "confidence": confidence,
        "reason": reason,
        "source": source,
        "requires_immediate_notification": priority in (CRITICAL, HIGH),
        # DETECTION ONLY. Nothing downstream may read this as permission.
        "authorizes_action": False,
    }


def is_at_least(priority: str, floor: str) -> bool:
    return _PRIORITY_ORDER.get(priority, 0) >= _PRIORITY_ORDER.get(floor, 0)


def should_notify_now(verdict: dict[str, Any], now=None) -> bool:
    """Immediate interruption only for CRITICAL/HIGH, and only in business
    hours. Everything else is collected for the morning report — which is
    the difference between an assistant and a pager."""
    from core.headless import config
    if not verdict.get("requires_immediate_notification"):
        return False
    return config.is_business_hours(now)
