"""The daily BuildPro matching run — jobs in, ranked matches out.

WHAT WAS MISSING
buildpro_matching.score_match() is a genuinely good scorer: weighted
factors, a note per factor, and score=None rather than a fabricated
number when neither side has comparable data. buildpro_data stores jobs,
candidates and matches. And nothing ran them daily, so "8 strong matches
this morning" was a thing the system could compute and never did.

Two pieces were absent, and only two:

  * JOB INTAKE. add_job() inserted whatever it was handed, with no
    normalisation and no duplicate check — the same posting arriving from
    two sources, or the same source twice, produced two jobs and then
    double-counted every match against them.
  * THE RUN. Nothing iterated open jobs against available candidates,
    ranked the results and said what to do about them.

This module is those two things. It does not re-implement scoring: every
score here comes from buildpro_matching, and a second scorer would be a
second answer to the same question.

EVERY NUMBER IS COUNTED, NEVER ASSERTED
"37 candidates evaluated" means 37 rows were read. "8 strong matches"
means eight scores came back at or above the threshold. If there are
three, the report says three. A report that inflates is worse than no
report, because Lee acts on it.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("jarvis.buildpro_daily")

# What counts as worth Lee's attention. Deliberately explicit rather than
# a magic number buried in a comparison.
STRONG_MATCH_SCORE = 75.0
EXCEPTIONAL_MATCH_SCORE = 90.0

# Truthful states shared with the rest of the system.
OK = "OK"
NOT_CONFIGURED = "NOT_CONFIGURED"
UNAVAILABLE = "UNAVAILABLE"     # configured, but nothing could be reached
FAILED = "FAILED"

_WHITESPACE = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^\w\s]")

# Seniority and project-type vocabulary, used to enrich a raw posting into
# the fields the existing scorer already knows how to compare.
_SENIORITY = (
    ("executive", ("chief", "president", "vp ", "vice president", "coo", "cfo", "ceo")),
    ("director", ("director", "head of", "managing")),
    ("senior", ("senior", "sr.", "lead", "principal", "superintendent", "manager")),
    ("mid", ("engineer", "estimator", "coordinator", "specialist")),
    ("junior", ("junior", "jr.", "assistant", "entry")),
)
_PROJECT_TYPES = (
    "data center", "healthcare", "hospital", "education", "school", "industrial",
    "commercial", "residential", "multifamily", "infrastructure", "highway",
    "bridge", "water", "energy", "solar", "mission critical", "renovation",
)


# ══ NORMALISE ════════════════════════════════════════════════════════════

def _clean(text: Any) -> str:
    return _WHITESPACE.sub(" ", str(text or "").strip())


def _canonical(text: Any) -> str:
    """Lowercased, punctuation-free, single-spaced — for comparison only,
    never for storage. The stored value keeps its real formatting."""
    return _WHITESPACE.sub(" ", _NON_WORD.sub(" ", str(text or "").lower())).strip()


def seniority_of(title: str) -> str:
    """The seniority a title implies, or '' when it implies none. '' is a
    real answer: guessing 'mid' for an unrecognised title would put a
    fabricated value into the match evidence."""
    lowered = f" {_canonical(title)} "
    for level, markers in _SENIORITY:
        if any(marker in lowered for marker in markers):
            return level
    return ""


def project_types_in(text: str) -> list[str]:
    lowered = _canonical(text)
    return [p for p in _PROJECT_TYPES if p in lowered]


def job_fingerprint(job: dict[str, Any]) -> str:
    """A stable identity for one posting.

    Title + company + location, canonicalised. Deliberately NOT the
    description: the same role reposted a week later with reworded copy is
    the same job, and treating it as new is how a board of forty postings
    becomes a board of four hundred. Deliberately not the source URL
    either — the same role listed on two boards is one job."""
    parts = [_canonical(job.get("title")),
             _canonical(job.get("company") or job.get("client_name")),
             _canonical(job.get("location"))]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def normalize_job(raw: dict[str, Any], source: str = "") -> dict[str, Any]:
    """One posting in the shape buildpro_data.add_job() accepts, enriched
    with the fields the existing scorer compares on.

    A field absent from the posting is absent from the result. Nothing is
    defaulted to a plausible value — an invented compensation range would
    be scored against as though it were real."""
    title = _clean(raw.get("title") or raw.get("job_title"))
    if not title:
        return {}

    description = _clean(raw.get("description") or raw.get("summary"))
    normalized: dict[str, Any] = {
        "title": title,
        "description": description,
        "location": _clean(raw.get("location") or raw.get("city")),
        "status": "open",
        "source": _clean(source or raw.get("source")),
    }
    for src_key, dest in (("compensation", "compensation"), ("salary", "compensation"),
                          ("employment_type", "employment_type"),
                          ("specialty", "specialty")):
        value = _clean(raw.get(src_key))
        if value and not normalized.get(dest):
            normalized[dest] = value

    skills = raw.get("required_skills") or raw.get("requirements") or raw.get("skills")
    if isinstance(skills, (list, tuple)):
        skills = ", ".join(str(s).strip() for s in skills if str(s).strip())
    if _clean(skills):
        normalized["required_skills"] = _clean(skills)

    years = raw.get("min_years_experience") or raw.get("years_experience")
    if isinstance(years, (int, float)) and years > 0:
        normalized["min_years_experience"] = int(years)
    else:
        # Postings state this in prose far more often than in a field
        # ("10+ years experience"), and leaving it unparsed meant the
        # experience factor was skipped on almost every real job — the
        # scorer then reported "missing data" for something the posting
        # plainly said.
        found = re.search(r"(\d{1,2})\s*\+?\s*(?:years?|yrs?)", 
                          f"{years or ''} {description}", re.I)
        if found:
            normalized["min_years_experience"] = int(found.group(1))

    # Enrichment derived from what the posting actually says.
    haystack = f"{title} {description}"
    seniority = seniority_of(title)
    projects = project_types_in(haystack)
    if not normalized.get("specialty") and projects:
        normalized["specialty"] = projects[0]

    normalized["_meta"] = {
        "fingerprint": job_fingerprint({**raw, "title": title,
                                        "location": normalized["location"]}),
        "company": _clean(raw.get("company") or raw.get("client_name")),
        "source_url": _clean(raw.get("url") or raw.get("source_url")),
        "seniority": seniority,
        "project_types": projects,
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        # From WebResearchJobSource (or any future adapter that supplies
        # them): when the posting states it was published, how much that
        # kind of source is trusted, and whether the title/fields were
        # directly read off the page. '' rather than a guess when a source
        # (like a manual/API entry) does not supply them.
        "date_posted": _clean(raw.get("date_posted")),
        "freshness_state": _clean(raw.get("freshness_state")),
        "confidence": raw.get("confidence"),
        "evidence": _clean(raw.get("evidence")),
    }
    return normalized


# ══ INTAKE, WITH DEDUPE ══════════════════════════════════════════════════

def _existing_by_fingerprint(fingerprint: str) -> Optional[dict[str, Any]]:
    from actions import buildpro_data
    for job in buildpro_data.list_jobs(limit=500) or []:
        source = str(job.get("source") or "")
        if f"fp:{fingerprint}" in source:
            return job
    return None


def intake_jobs(raw_jobs: list[dict[str, Any]], source: str = "") -> dict[str, Any]:
    """Normalise, deduplicate and store a batch of postings.

    A posting whose fingerprint is already on file UPDATES that job rather
    than inserting a second one — the same role reposted is the same role.
    One bad posting is skipped with its reason; it never stops the batch.
    """
    from actions import buildpro_data

    stored, updated, skipped = [], [], []
    seen_in_batch: set[str] = set()

    for raw in raw_jobs or []:
        try:
            normalized = normalize_job(raw, source=source)
            if not normalized:
                skipped.append({"reason": "no title", "raw": str(raw)[:120]})
                continue
            meta = normalized.pop("_meta")
            fingerprint = meta["fingerprint"]

            if fingerprint in seen_in_batch:
                skipped.append({"reason": "duplicate within this batch",
                                "title": normalized["title"]})
                continue
            seen_in_batch.add(fingerprint)

            # The fingerprint and provenance ride in `source` so no schema
            # migration is needed for what is a small amount of metadata.
            provenance = " ".join(filter(None, [
                normalized.get("source") or source, f"fp:{fingerprint}",
                f"url:{meta['source_url']}" if meta["source_url"] else "",
                f"company:{meta['company']}" if meta["company"] else "",
                f"seniority:{meta['seniority']}" if meta["seniority"] else "",
                f"posted:{meta['date_posted']}" if meta.get("date_posted") else "",
                f"freshness:{meta['freshness_state']}" if meta.get("freshness_state") else "",
                f"confidence:{meta['confidence']:.2f}" if isinstance(meta.get("confidence"), (int, float)) else "",
                f"evidence:{meta['evidence']}" if meta.get("evidence") else "",
            ]))
            normalized["source"] = provenance[:500]

            existing = _existing_by_fingerprint(fingerprint)
            if existing:
                buildpro_data.update_job(existing["id"], **{
                    k: v for k, v in normalized.items() if v not in (None, "")})
                updated.append({"id": existing["id"], "title": normalized["title"]})
                continue

            job_id = buildpro_data.add_job(**normalized)
            stored.append({"id": job_id, "title": normalized["title"],
                           "fingerprint": fingerprint})
        except Exception as exc:
            logger.warning("could not intake a job posting: %s", exc)
            skipped.append({"reason": str(exc)[:200],
                            "title": _clean(raw.get("title")) if isinstance(raw, dict) else ""})

    return {"ok": True, "state": OK, "source": source,
            "received": len(raw_jobs or []), "stored": len(stored),
            "updated": len(updated), "skipped": len(skipped),
            "new_jobs": stored, "updated_jobs": updated, "skipped_jobs": skipped}


# ══ DISCOVERY (source adapters) ══════════════════════════════════════════

class JobSource:
    """A place postings come from. Adapters return raw dicts; intake_jobs
    normalises and dedupes them, so an adapter never needs to know the
    storage shape."""

    name = "base"

    def is_configured(self) -> bool:
        raise NotImplementedError

    def fetch(self, limit: int = 50) -> list[dict[str, Any]]:
        raise NotImplementedError


# The roles BuildPro recruits for. Used to BUILD SEARCHES, never to
# generate postings — every job returned still comes off a real page.
LEADERSHIP_ROLES: tuple[str, ...] = (
    "VP of Construction", "Vice President of Construction",
    "Director of Construction", "Senior Director of Construction",
    "Construction Executive", "Regional Construction Manager",
    "General Manager Construction", "Construction Operations Executive",
    "COO Construction", "President Construction",
)


class WebResearchJobSource(JobSource):
    """Public job postings, found with the general research engine.

    Not a job-board API and not a scraper for one specific site: it
    searches for the role, reads the pages that come back, and takes
    whatever a posting actually states. A page that yields no title is
    skipped rather than filled in.

    Configured whenever the research engine can reach the web at all —
    there is no credential, which is the point. When the web is
    unreachable it returns nothing and the run reports NOT_CONFIGURED
    like any other unavailable source."""

    name = "web_research"

    def __init__(self, roles: Optional[list[str]] = None, location: str = ""):
        # The full leadership-role list, not a prefix of it — the roles
        # this class exists to find are the whole point of it.
        self.roles = roles or list(LEADERSHIP_ROLES)
        self.location = location
        # Whether the last fetch could reach the web at all. "Configured
        # but unreachable" and "reachable but nothing matched" are
        # different facts and must not both report OK.
        self.last_state = OK

    def is_configured(self) -> bool:
        try:
            from actions import web_research  # noqa: F401
            return True
        except Exception:
            return False

    def fetch(self, limit: int = 50) -> list[dict[str, Any]]:
        from actions import web_research as wr

        per_role = max(1, limit // max(len(self.roles), 1))
        postings: list[dict[str, Any]] = []
        reachable = False
        for role in self.roles:
            query = f"{role} jobs" + (f" {self.location}" if self.location else "")
            outcome = wr.research(query, max_sources=min(per_role, 3),
                                  extractor=wr.extract_job, record=False)
            if not outcome.get("ok"):
                logger.info("job search for %r found nothing readable: %s",
                            role, outcome.get("detail"))
                continue
            reachable = True
            for result in outcome.get("results") or []:
                fields = result.get("fields") or {}

                def _value(name):
                    entry = fields.get(name) or {}
                    return entry.get("value")

                title = _value("job_title")
                if not title:
                    continue     # a posting with no title is not a posting

                def _evidence(name):
                    return (fields.get(name) or {}).get("evidence")

                freshness = result.get("freshness") or {}
                posting = {
                    "title": title,
                    "company": _value("company") or "",
                    "location": _value("location") or self.location,
                    "salary": _value("compensation") or "",
                    "employment_type": _value("employment_type") or "",
                    "description": (fields.get("summary") or {}).get("value") or "",
                    "url": result.get("source_url") or "",
                    "source": f"web:{result.get('source_type') or 'unknown'}",
                    "role_searched": role,
                    # Carried through so a job's provenance records not just
                    # WHERE it came from but how much to trust it: the
                    # page's own stated age (freshness), how reliable this
                    # kind of source is taken to be, and whether the title
                    # was directly read off the page or merely a query echo.
                    "date_posted": freshness.get("published_at") or "",
                    "freshness_state": freshness.get("state") or "UNKNOWN",
                    "confidence": result.get("source_reliability"),
                    "evidence": _evidence("job_title") or "OBSERVED",
                }
                postings.append(posting)
        self.last_state = OK if reachable else UNAVAILABLE
        return postings


def available_sources() -> list[JobSource]:
    """Every configured job source.

    The research-backed source needs no credential and so is always
    present; whether it RETURNS anything depends on the web being
    reachable, which is a different question and reported separately. A
    credentialled board adapter can be added here without touching
    anything else."""
    return [WebResearchJobSource()]


def discover_jobs(limit_per_source: int = 25) -> dict[str, Any]:
    """Pull postings from every configured source and take them through
    intake.

    With no source configured this returns NOT_CONFIGURED and stores
    nothing. It never reports a successful discovery it did not perform —
    the manual/API intake path (intake_jobs) is what carries BuildPro
    until a board credential exists."""
    sources = [s for s in available_sources() if s.is_configured()]
    if not sources:
        return {
            "ok": False, "state": NOT_CONFIGURED, "stored": 0, "updated": 0,
            "sources": [],
            "detail": ("No job-board source is configured, so no jobs were "
                       "retrieved. Jobs added through intake_jobs() (manual entry, "
                       "a client email, or an API once credentialled) are matched "
                       "normally."),
        }

    results, stored, updated = [], 0, 0
    for source in sources:
        try:
            raw = source.fetch(limit=limit_per_source)
            outcome = intake_jobs(raw, source=source.name)
            stored += outcome["stored"]
            updated += outcome["updated"]
            # A source that could not reach the web returned zero postings
            # for a reason that is NOT "there were none" — reporting that
            # as OK is the false-success failure this codebase exists to
            # avoid.
            state = getattr(source, "last_state", OK)
            # `**outcome` must come FIRST: intake_jobs() returns its own
            # "state": OK, and spreading it after this key silently
            # overwrote an UNAVAILABLE source with a clean OK — the exact
            # false success this branch exists to prevent.
            results.append({**outcome, "source": source.name, "state": state})
        except Exception as exc:
            logger.warning("job source %s failed: %s", source.name, exc)
            results.append({"source": source.name, "state": FAILED,
                            "detail": str(exc)[:300]})

    reachable = [r for r in results if r.get("state") == OK]
    if not reachable:
        return {
            "ok": False, "state": UNAVAILABLE, "stored": stored, "updated": updated,
            "sources": results,
            "detail": ("No job source could be reached, so no postings were "
                       "retrieved. Jobs added through intake_jobs() are matched "
                       "normally."),
        }
    return {"ok": True, "state": OK, "stored": stored, "updated": updated,
            "sources": results}


# ══ THE DAILY RUN ════════════════════════════════════════════════════════

def run_daily_matching(min_score: float = 50.0, top_n: int = 10) -> dict[str, Any]:
    """Score every open job against every available candidate and rank the
    result.

    Uses buildpro_matching.generate_matches_for_job(), which already
    upserts each score into buildpro_matches — so a re-run updates rows
    rather than duplicating them, and this function inherits that.

    One job that fails to score does not stop the run; it is recorded and
    the rest continue."""
    from actions import buildpro_data, buildpro_matching

    started = time.time()
    try:
        jobs = buildpro_data.list_jobs(status="open", limit=200) or []
        candidates = buildpro_data.list_candidates(limit=500) or []
    except Exception as exc:
        logger.exception("could not read the BuildPro tables")
        return {"ok": False, "state": FAILED, "detail": str(exc)[:300],
                "jobs_evaluated": 0, "candidates_evaluated": 0, "matches": []}

    # Names, so the report reads "Dana Reeves → Senior PM" rather than
    # "candidate 1 → job 4". Looked up once here instead of per match.
    candidate_names = {c["id"]: c.get("name") for c in candidates if c.get("id")}

    all_matches: list[dict[str, Any]] = []
    failed_jobs: list[dict[str, Any]] = []
    for job in jobs:
        try:
            scored = buildpro_matching.generate_matches_for_job(
                job["id"], min_score=min_score) or []
            for match in scored:
                match["job_id"] = job["id"]
                match.setdefault("job_title", job.get("title"))
                if not match.get("candidate_name"):
                    match["candidate_name"] = candidate_names.get(match.get("candidate_id"))
                all_matches.append(match)
        except Exception as exc:
            logger.warning("scoring failed for job %s: %s", job.get("id"), exc)
            failed_jobs.append({"job_id": job.get("id"), "detail": str(exc)[:200]})

    # generate_matches_for_job() upserts EVERY pairing it scores so the
    # match table stays complete; min_score governs what is worth
    # REPORTING. Without this filter a 0% pairing appeared in the morning
    # report next to an 87% one, which makes the report useless.
    scored_matches = [m for m in all_matches
                      if m.get("score") is not None and float(m["score"]) >= min_score]
    scored_matches.sort(key=lambda m: -float(m["score"]))

    strong = [m for m in scored_matches if float(m["score"]) >= STRONG_MATCH_SCORE]
    exceptional = [m for m in scored_matches if float(m["score"]) >= EXCEPTIONAL_MATCH_SCORE]

    top = scored_matches[0] if scored_matches else None

    # HUBSPOT on the top match only — one lookup per run, not one per
    # match. Isolated: a HubSpot outage must not cost the matching result
    # that is the actual point of this run.
    #
    # A STRONG match with no existing HubSpot record moves one step
    # further than a read: prepare_hubspot_writeback() proposes creating
    # the company via the existing approval-gated agent rather than only
    # reporting the absence — but it still never writes anything itself,
    # and a match below the strong threshold gets the read-only context
    # only, so a weak/irrelevant match can never spend an approval slot.
    if top is not None:
        jobs_by_id = {j["id"]: j for j in jobs}
        job = jobs_by_id.get(top.get("job_id"))
        company = company_from_source((job or {}).get("source") or "")
        if company:
            try:
                from actions import cross_system
                if float(top["score"]) >= STRONG_MATCH_SCORE:
                    top["hubspot"] = cross_system.prepare_hubspot_writeback(
                        company, job_id=top.get("job_id"))
                else:
                    top["hubspot"] = cross_system.hubspot_context_for_employer(company)
                top["employer"] = company

                # RESEARCH what is needed. A strong match against an
                # employer with no existing CRM history is exactly the
                # "the CEO does not yet have enough information to decide"
                # case — one bounded, isolated web lookup, distilled into
                # the Brain, not blocking the report if it fails or the
                # web is unreachable.
                if top["hubspot"].get("state") == "APPROVAL_REQUIRED":
                    try:
                        from actions import web_research
                        research = web_research.research(
                            f"{company} construction company", max_sources=1)
                        if research.get("ok"):
                            top["employer_research"] = {
                                "sources_read": len(research.get("sources_read") or []),
                                "confidence": research.get("confidence"),
                            }
                    except Exception:
                        logger.debug("employer research failed for %r", company, exc_info=True)
            except Exception:
                logger.debug("hubspot context lookup failed for %r", company, exc_info=True)

    return {
        "ok": True, "state": OK,
        # Every one of these is a count of something that happened.
        "jobs_evaluated": len(jobs),
        "candidates_evaluated": len(candidates),
        "matches_scored": len(scored_matches),
        "strong_matches": len(strong),
        "exceptional_matches": len(exceptional),
        "failed_jobs": failed_jobs,
        "matches": scored_matches[:top_n],
        "top": top,
        "duration_ms": int((time.time() - started) * 1000),
    }


_COMPANY_FROM_SOURCE_RE = re.compile(r"\bcompany:(\S+)")


def company_from_source(source: str) -> str:
    """The employer name tucked into a job's provenance string by
    intake_jobs() (there is no dedicated company column on buildpro_jobs).
    '' when the source carries none — never guessed."""
    match = _COMPANY_FROM_SOURCE_RE.search(source or "")
    return match.group(1).replace("_", " ") if match else ""


def recommended_action(match: dict[str, Any]) -> str:
    """What to do about one match. Tied to the score, so a 96% and a 52%
    do not get the same instruction."""
    score = float(match.get("score") or 0)
    if score >= EXCEPTIONAL_MATCH_SCORE:
        return "Contact candidate today"
    if score >= STRONG_MATCH_SCORE:
        return "Contact candidate"
    return "Review before contacting"


def _match_line(match: dict[str, Any], rank: int) -> list[str]:
    name = match.get("candidate_name") or f"candidate {match.get('candidate_id')}"
    job = match.get("job_title") or f"job {match.get('job_id')}"
    lines = [f"#{rank} {name} → {job}", f"{float(match['score']):.0f}% match"]

    # "Why" comes from the factors the scorer actually evaluated — not a
    # generated explanation of a number.
    reasons = [detail.get("note") for detail in (match.get("factors") or {}).values()
               if isinstance(detail, dict) and detail.get("evaluated") and detail.get("note")]
    if reasons:
        lines.append("Why:")
        lines.extend(f"  - {reason}" for reason in reasons[:6])
    elif match.get("rationale"):
        lines.append(f"Why: {match['rationale']}")
    hubspot = match.get("hubspot")
    if hubspot and hubspot.get("state") in ("OK", "RECORD_EXISTS", "APPROVAL_REQUIRED", "WRITE_FAILED"):
        lines.append(f"HubSpot: {hubspot['detail']}")
    lines.append(f"Recommended action: {recommended_action(match)}")
    return lines


def format_daily_report(result: dict[str, Any], top_n: int = 3) -> str:
    """The morning BuildPro summary, built entirely from counted values.

    When nothing matched, it says so plainly. A report that pads an empty
    morning with encouraging language is one Lee stops reading."""
    if not result.get("ok"):
        return ("DAILY BUILDPRO MATCHES\n\nThe matching run did not complete: "
                f"{result.get('detail') or 'unknown error'}")

    lines = ["DAILY BUILDPRO MATCHES", ""]
    lines.append(f"{result['jobs_evaluated']} open job(s) evaluated")
    lines.append(f"{result['candidates_evaluated']} candidate(s) evaluated")
    lines.append(f"{result['strong_matches']} strong match(es)")
    lines.append(f"{result['exceptional_matches']} match(es) above "
                 f"{EXCEPTIONAL_MATCH_SCORE:.0f}%")

    if result.get("failed_jobs"):
        lines.append(f"{len(result['failed_jobs'])} job(s) could not be scored")

    if not result.get("matches"):
        lines.append("")
        if result["jobs_evaluated"] == 0:
            lines.append("No open jobs on file, so there was nothing to match against.")
        elif result["candidates_evaluated"] == 0:
            lines.append("No candidates on file yet.")
        else:
            lines.append("No candidate scored above the reporting threshold today.")
        return "\n".join(lines)

    for rank, match in enumerate(result["matches"][:top_n], start=1):
        lines.append("")
        lines.extend(_match_line(match, rank))
    return "\n".join(lines)


def run_and_report(min_score: float = 50.0, top_n: int = 10) -> dict[str, Any]:
    """The whole daily job: discover, match, rank, report, and record the
    outcome where the next decision will read it."""
    discovery = discover_jobs()
    result = run_daily_matching(min_score=min_score, top_n=top_n)
    report = format_daily_report(result)

    try:
        from actions import ceo_decision
        ceo_decision.record_outcome(
            {"source": "buildpro_daily_matching", "kind": "matching",
             "title": "daily BuildPro matching", "business": "buildpro"},
            ok=bool(result.get("ok")),
            detail=(f"{result.get('strong_matches', 0)} strong match(es) from "
                    f"{result.get('jobs_evaluated', 0)} job(s)"),
            verified=bool(result.get("ok")))
    except Exception:
        logger.debug("could not record the matching outcome", exc_info=True)

    return {**result, "discovery": discovery, "report": report}
