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


def available_sources() -> list[JobSource]:
    """Every configured job source. Empty is honest: this deployment has
    no job-board credential of any kind, and discover_jobs() reports
    NOT_CONFIGURED rather than returning invented postings."""
    return []


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
            results.append({"source": source.name, "state": OK, **outcome})
        except Exception as exc:
            logger.warning("job source %s failed: %s", source.name, exc)
            results.append({"source": source.name, "state": FAILED,
                            "detail": str(exc)[:300]})
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
        "top": scored_matches[0] if scored_matches else None,
        "duration_ms": int((time.time() - started) * 1000),
    }


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
