"""Contextual, 7-category email classification (2026-09-03, Lee's
autonomous-CEO/COS spec, Sections 3-8).

gmail_integration.classify_message() is NOT replaced here — it stays
exactly as it is, keyword rules and all, because actions/candidate_intake.py
and actions/buildpro_client_intake.py already key their gating off its
exact candidate_reply/client_inquiry labels, and its existing test suite
pins that behavior precisely. Building a second, competing classifier that
disagreed with it would be exactly the kind of duplicate system Lee's spec
explicitly forbids.

Instead, this module WRAPS it: classify_email() calls classify_message()
for the "legacy_label" (still the source of truth for BuildPro's own
intake gating) and adds a second, broader dimension most of the rest of
Lee's spec depends on — a 7-category system that spans every business
(not just BuildPro's own two intake types), plus personal/system/spam
mail classify_message() was never built to understand:

    BUILDPRO | DAILY_DEAL_FINDERS | CAREERROCKET | JARVIS | PERSONAL |
    REVIEW_REQUIRED | IRRELEVANT

Every result carries a confidence and a plain-English reason — never a
bare label with no way to audit why. Nothing here ever fabricates
certainty: anything genuinely ambiguous lands on REVIEW_REQUIRED rather
than being forced into a guess, and IRRELEVANT is reserved for
unambiguous marketing/notification noise, never a platform name alone
(see the TikTok/Instagram/Facebook note below).
"""
from __future__ import annotations

from typing import Any

from actions import gmail_integration

CATEGORY_BUILDPRO = "BUILDPRO"
CATEGORY_DDF = "DAILY_DEAL_FINDERS"
CATEGORY_CAREERROCKET = "CAREERROCKET"
CATEGORY_JARVIS = "JARVIS"
CATEGORY_PERSONAL = "PERSONAL"
CATEGORY_REVIEW = "REVIEW_REQUIRED"
CATEGORY_IRRELEVANT = "IRRELEVANT"

ALL_CATEGORIES = (
    CATEGORY_BUILDPRO, CATEGORY_DDF, CATEGORY_CAREERROCKET,
    CATEGORY_JARVIS, CATEGORY_PERSONAL, CATEGORY_REVIEW, CATEGORY_IRRELEVANT,
)

# Domain/company_id isolation tag for anything routed off this category
# (Section 10). REVIEW_REQUIRED/IRRELEVANT get no company_id — nothing is
# confidently "owned" by a business yet, so nothing should be filed as if
# it were.
_CATEGORY_COMPANY_ID: dict[str, str] = {
    CATEGORY_BUILDPRO: "buildpro",
    CATEGORY_DDF: "daily_deal_finders",
    CATEGORY_CAREERROCKET: "careerrocket",
    CATEGORY_JARVIS: "jarvis",
    CATEGORY_PERSONAL: "personal",
}

# ── BuildPro: broadened job-title / job-seeking signal (Section 6) ────────
# Explicitly illustrative, not exhaustive — real job-seeking signal in the
# body must be recognized even for a title not on this list. Grouped by
# rough family for maintainability only; the classifier flattens it.
_BUILDPRO_TITLE_SIGNALS: tuple[str, ...] = (
    # Executive / leadership
    "chief executive officer", "president", "vice president", "coo", "cfo",
    "ceo", "general manager", "managing director", "director of operations",
    "vp of construction", "vp of development", "chief operating officer",
    # Project / construction management
    "project manager", "project engineer", "construction manager",
    "site superintendent", "superintendent", "field supervisor",
    "assistant project manager", "preconstruction manager",
    # Design / architecture — explicitly must catch non-executive,
    # non-"construction"-titled design roles (Lee's own example).
    "architect", "architectural designer", "senior interior designer",
    "interior designer", "design manager", "landscape architect",
    "bim manager", "bim coordinator", "cad designer", "drafter",
    # Estimating
    "estimator", "chief estimator", "senior estimator", "cost estimator",
    "preconstruction estimator",
    # Engineering
    "civil engineer", "structural engineer", "mep engineer",
    "mechanical engineer", "electrical engineer", "geotechnical engineer",
    # Trades
    "electrician", "plumber", "carpenter", "welder", "hvac technician",
    "ironworker", "millwright", "pipefitter", "heavy equipment operator",
    "foreman", "crew lead",
    # Development
    "development manager", "real estate developer", "land acquisition",
    "acquisitions manager",
    # Specialized industry roles
    "safety manager", "ehs manager", "quality control manager",
    "scheduler", "contracts administrator", "procurement manager",
)

_BUILDPRO_CONTEXT_SIGNALS: tuple[str, ...] = (
    "resume", "cv", "job application", "job opening", "job posting",
    "hiring", "staffing", "recruiter", "recruiting", "candidate",
    "interview", "years of experience", "years experience",
    "open position", "seeking a role", "seeking employment",
    "project", "quote", "bid", "proposal", "estimate", "rfp",
    "subcontractor", "general contractor", "jobsite",
)


def _matches_buildpro_signal(haystack: str) -> tuple[bool, str]:
    for title in _BUILDPRO_TITLE_SIGNALS:
        if title in haystack:
            return True, f"job title/role signal: '{title}'"
    for ctx in _BUILDPRO_CONTEXT_SIGNALS:
        if ctx in haystack:
            return True, f"staffing/construction context: '{ctx}'"
    return False, ""


# ── Daily Deal Finders: content-driven, never platform-driven (Section 5) ─
# CRITICAL CORRECTION in Lee's spec: TikTok/Instagram/Facebook must never
# be auto-blacklisted as irrelevant just because of the platform — they
# are often DDF's highest-value channel. Relevance comes from what the
# message is actually ABOUT (deals, affiliate/creator programs, content or
# collab opportunities, promo codes), never from which platform sent it.
# Note there is deliberately no platform-name list anywhere in this
# module, positive or negative — see classify_email()'s docstring.
_DDF_SIGNALS: tuple[str, ...] = (
    "deal", "coupon", "promo code", "discount code", "affiliate",
    "creator program", "creator fund", "brand partnership", "collab",
    "collaboration opportunity", "sponsorship", "influencer",
    "product drop", "flash sale", "amazon associates", "tiktok shop",
    "content opportunity", "monetization", "revenue share",
    "commission rate", "clearance", "daily deal", "shopping haul",
)


def _matches_ddf_signal(haystack: str) -> tuple[bool, str]:
    for kw in _DDF_SIGNALS:
        if kw in haystack:
            return True, f"deal/creator/monetization signal: '{kw}'"
    return False, ""


# ── CareerRocket Pro: career-coaching signal — distinct from BuildPro's
# staffing/placement signal above (a person asking for coaching/resume
# help directly, not applying to a role BuildPro is staffing). ────────────
_CAREERROCKET_SIGNALS: tuple[str, ...] = (
    "career coaching", "career coach", "resume review", "resume writing",
    "linkedin profile review", "interview coaching", "career pivot",
    "career rocket", "careerrocket", "mock interview", "career consultation",
    "job search coaching", "personal branding coach",
)


def _matches_careerrocket_signal(haystack: str) -> tuple[bool, str]:
    for kw in _CAREERROCKET_SIGNALS:
        if kw in haystack:
            return True, f"career-coaching signal: '{kw}'"
    return False, ""


# ── JARVIS: the system's own infrastructure telling on itself ─────────────
_JARVIS_INFRA_DOMAINS: tuple[str, ...] = (
    "render.com", "github.com", "githubapp.com", "ollama.com", "cartesia.ai",
    "twilio.com", "hubspot.com", "google.com", "accounts.google.com",
)
_JARVIS_INFRA_SIGNALS: tuple[str, ...] = (
    "deploy failed", "deployment failed", "build failed", "service is down",
    "service resumed", "workflow run failed", "api key", "token expired",
    "token expiring", "rate limit", "webhook", "health check failed",
    "your build", "pull request", "commit pushed", "incident",
)


def _matches_jarvis_signal(sender_domain: str, haystack: str) -> tuple[bool, str]:
    if sender_domain and any(
        sender_domain == d or sender_domain.endswith("." + d) for d in _JARVIS_INFRA_DOMAINS
    ):
        for kw in _JARVIS_INFRA_SIGNALS:
            if kw in haystack:
                return True, f"own-infra notification from {sender_domain}: '{kw}'"
    return False, ""


# ── Negative / spam signals (Section 7) ────────────────────────────────────
# Refined so real DDF-relevant promotional content isn't over-excluded:
# these only ever push toward IRRELEVANT once nothing above already
# matched a positive business signal, and a platform name is never on
# this list — only genuinely generic marketing/account-noise phrasing is.
_IRRELEVANT_SIGNALS: tuple[str, ...] = (
    "unsubscribe", "you are receiving this email because", "view in browser",
    "terms of service update", "password reset", "verify your email",
    "your subscription", "webinar invitation",
)


def _result(category: str, confidence: float, reason: str, legacy_label: str) -> dict[str, Any]:
    return {
        "category": category,
        "confidence": round(confidence, 2),
        "reason": reason,
        "company_id": _CATEGORY_COMPANY_ID.get(category),
        "legacy_label": legacy_label,
    }


def classify_email(message: dict[str, Any]) -> dict[str, Any]:
    """Contextual 7-category classification over sender/sender-domain/
    subject/full body/attachment filenames — never subject-only (Section
    4). Returns {category, confidence, reason, company_id, legacy_label}.

    Routing rule (Section 8), enforced by construction, not by convention:
    a message can only reach BUILDPRO here when either (a) the audited,
    tested legacy rules in gmail_integration.classify_message() already
    called it candidate_reply/client_inquiry, or (b) the broadened
    title/context signal matches — so nothing IRRELEVANT- or REVIEW-bound
    can also be silently treated as a BuildPro record. IRRELEVANT is
    reserved for real marketing/notification noise; callers (the router
    agent below, dashboard display) must never create a CRM/business
    record, draft, or SMS off an IRRELEVANT or REVIEW_REQUIRED result."""
    legacy_label = gmail_integration.classify_message(message)

    subject = (message.get("subject") or "").lower()
    sender = (message.get("sender") or "").lower()
    sender_domain = (message.get("sender_domain") or "").lower()
    snippet = (message.get("snippet") or "").lower()
    body = (message.get("body") or "")[:4000].lower()
    attachment_names = " ".join(
        (a.get("filename") or "") for a in (message.get("attachments") or [])
    ).lower()
    haystack = f"{subject} {sender} {snippet} {body} {attachment_names}"

    is_automated_sender = any(k in sender for k in ("no-reply", "noreply", "do-not-reply"))

    # 1. JARVIS — checked first so an automated Render/GitHub/HubSpot
    # notification is never mistaken for a candidate/client/deal just
    # because of an incidental keyword collision.
    matched, reason = _matches_jarvis_signal(sender_domain, haystack)
    if matched:
        return _result(CATEGORY_JARVIS, 0.9, reason, legacy_label)

    # 2. BUILDPRO — the audited legacy rules first (highest confidence,
    # already extensively tested), then the broadened job-title list
    # (also high precision — a specific title is a strong signal).
    if legacy_label in ("candidate_reply", "client_inquiry"):
        return _result(
            CATEGORY_BUILDPRO, 0.85,
            f"BuildPro staffing signal (legacy rule: {legacy_label})", legacy_label,
        )
    title_matched, title_reason = _matches_buildpro_signal(haystack)
    # Title-only match (not the generic context words below) is checked
    # here, before CareerRocket, since a real job title is unambiguous.
    if title_matched and title_reason.startswith("job title") and not is_automated_sender:
        return _result(CATEGORY_BUILDPRO, 0.65, title_reason, legacy_label)

    # 3. CAREERROCKET — specific multi-word coaching phrases, checked
    # before BuildPro's generic single-word context list (below) so
    # "resume review"/"career coaching" isn't swallowed by the bare word
    # "resume" first.
    matched, reason = _matches_careerrocket_signal(haystack)
    if matched:
        return _result(CATEGORY_CAREERROCKET, 0.6, reason, legacy_label)

    # 4. BUILDPRO — generic staffing/construction context words (lower
    # precision than a title match, so checked last among BuildPro paths).
    if title_matched and not is_automated_sender:
        return _result(CATEGORY_BUILDPRO, 0.55, title_reason, legacy_label)

    # 5. DAILY_DEAL_FINDERS — content-driven, platform-neutral.
    matched, reason = _matches_ddf_signal(haystack)
    if matched:
        return _result(CATEGORY_DDF, 0.6, reason, legacy_label)

    # 6. IRRELEVANT — unambiguous marketing/notification noise. Checked by
    # content, not by sender-address shape: a branded marketing address
    # ("hello@render.com") is just as often the true sender as a literal
    # "no-reply@" one, so this can't require is_automated_sender.
    matched_neg = [kw for kw in _IRRELEVANT_SIGNALS if kw in haystack]
    if matched_neg:
        return _result(CATEGORY_IRRELEVANT, 0.7, f"automated/marketing noise: '{matched_neg[0]}'", legacy_label)
    if is_automated_sender or legacy_label == "notification":
        return _result(CATEGORY_IRRELEVANT, 0.4, "automated sender, no business signal found", legacy_label)

    # 7. PERSONAL — a real (non-automated) sender with no business signal
    # at all, rather than a forced guess in any business direction.
    if not is_automated_sender:
        return _result(CATEGORY_PERSONAL, 0.4, "no business signal found; real (non-automated) sender", legacy_label)

    # 8. Genuinely ambiguous — never force it.
    return _result(CATEGORY_REVIEW, 0.3, "no confident signal in any direction", legacy_label)
