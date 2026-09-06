"""BuildPro candidate <-> job matching engine.

Transparent, rule-based scoring — no ML, no invented data. Each factor
(title/role, specialty, years of experience, skills, location,
compensation, availability) is scored independently out of its own weight,
and a factor is skipped entirely (not scored 0) when either side is missing
the relevant field, so a thin record doesn't get unfairly penalized. The
final score is earned-points / possible-points (only counting evaluated
factors) * 100, so it's always relative to what was actually comparable —
and the rationale says exactly which factors were used and which were
skipped for missing data, per "do not fabricate missing information."

score_match() is the pure scoring function; generate_matches_for_job() /
generate_matches_for_candidate() are the persistence-aware wrappers that
score against the whole candidate/job pool and upsert into
buildpro_data.buildpro_matches via add_match/update_match (find_match first,
so re-running re-scores in place instead of duplicating rows).
"""
from __future__ import annotations

import re
from typing import Any

from actions import buildpro_data as bd

# (label, weight) — weights sum to 100 when every factor is evaluable.
_FACTOR_WEIGHTS = {
    "title": 20,
    "specialty": 20,
    "experience": 20,
    "skills": 20,
    "location": 10,
    "compensation": 5,
    "availability": 5,
}

_STOPWORDS = {"the", "a", "an", "and", "of", "for", "to", "in", "on", "at"}
_NUMBER_RE = re.compile(r"[\d,]+(?:\.\d+)?")


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w and w not in _STOPWORDS}


def _score_title(candidate: dict[str, Any], job: dict[str, Any]) -> tuple[float, str] | None:
    ct, jt = (candidate.get("title") or "").strip(), (job.get("title") or "").strip()
    if not ct or not jt:
        return None
    weight = _FACTOR_WEIGHTS["title"]
    if ct.lower() == jt.lower():
        return weight, f"Title matches exactly ('{ct}')."
    cw, jw = _words(ct), _words(jt)
    if cw and jw and (cw & jw):
        shared = ", ".join(sorted(cw & jw))
        return weight * 0.5, f"Title partially overlaps (shared: {shared}) — candidate '{ct}' vs job '{jt}'."
    return 0.0, f"Title doesn't overlap — candidate '{ct}' vs job '{jt}'."


def _score_specialty(candidate: dict[str, Any], job: dict[str, Any]) -> tuple[float, str] | None:
    cs, js = (candidate.get("specialty") or "").strip(), (job.get("specialty") or "").strip()
    if not cs or not js:
        return None
    weight = _FACTOR_WEIGHTS["specialty"]
    if cs.lower() == js.lower():
        return weight, f"Specialty matches exactly ('{cs}')."
    if cs.lower() in js.lower() or js.lower() in cs.lower():
        return weight * 0.6, f"Specialty partially matches — candidate '{cs}' vs job '{js}'."
    return 0.0, f"Specialty doesn't match — candidate '{cs}' vs job '{js}'."


def _score_experience(candidate: dict[str, Any], job: dict[str, Any]) -> tuple[float, str] | None:
    cy, jy = candidate.get("years_experience"), job.get("min_years_experience")
    if cy is None or jy is None:
        return None
    weight = _FACTOR_WEIGHTS["experience"]
    if cy >= jy:
        return weight, f"{cy} years meets the {jy}-year requirement."
    if cy >= jy - 2:
        return weight * 0.5, f"{cy} years is close to the {jy}-year requirement (within 2 years)."
    return 0.0, f"{cy} years is below the {jy}-year requirement."


def _score_skills(candidate: dict[str, Any], job: dict[str, Any]) -> tuple[float, str] | None:
    cand_skills = set(bd.split_keywords(candidate.get("skills")))
    job_skills = set(bd.split_keywords(job.get("required_skills")))
    if not cand_skills or not job_skills:
        return None
    weight = _FACTOR_WEIGHTS["skills"]
    overlap = cand_skills & job_skills
    ratio = len(overlap) / len(job_skills)
    if overlap:
        return weight * ratio, f"{len(overlap)}/{len(job_skills)} required skill(s) matched: {', '.join(sorted(overlap))}."
    return 0.0, f"None of the {len(job_skills)} required skill(s) matched."


def _score_location(candidate: dict[str, Any], job: dict[str, Any]) -> tuple[float, str] | None:
    cl, jl = (candidate.get("location") or "").strip(), (job.get("location") or "").strip()
    if not cl or not jl:
        return None
    weight = _FACTOR_WEIGHTS["location"]
    if "remote" in cl.lower() or "remote" in jl.lower():
        return weight, "Remote — location is not a constraint."
    if cl.lower() == jl.lower() or cl.lower() in jl.lower() or jl.lower() in cl.lower():
        return weight, f"Location matches ('{cl}' / '{jl}')."
    return 0.0, f"Location differs — candidate '{cl}' vs job '{jl}'."


def _score_compensation(candidate: dict[str, Any], job: dict[str, Any]) -> tuple[float, str] | None:
    cc, jc = (candidate.get("desired_compensation") or "").strip(), (job.get("compensation") or "").strip()
    if not cc or not jc:
        return None
    weight = _FACTOR_WEIGHTS["compensation"]
    cand_nums = [float(n.replace(",", "")) for n in _NUMBER_RE.findall(cc)]
    job_nums = [float(n.replace(",", "")) for n in _NUMBER_RE.findall(jc)]
    if not cand_nums or not job_nums:
        return weight * 0.5, f"Compensation data present on both sides ('{cc}' / '{jc}') but not numerically comparable — review manually."
    if min(cand_nums) <= max(job_nums):
        return weight, f"Candidate's ask ('{cc}') fits within the job's range ('{jc}')."
    return 0.0, f"Candidate's ask ('{cc}') exceeds the job's range ('{jc}')."


def _score_availability(candidate: dict[str, Any], job: dict[str, Any]) -> tuple[float, str] | None:
    avail = (candidate.get("availability") or "").strip().lower()
    if not avail or avail == "unknown":
        return None
    weight = _FACTOR_WEIGHTS["availability"]
    if avail == "available":
        return weight, "Candidate is available now."
    if avail == "employed":
        return weight * 0.4, "Candidate is currently employed (may need notice)."
    return 0.0, "Candidate has indicated they are not looking."


_FACTOR_SCORERS = {
    "title": _score_title,
    "specialty": _score_specialty,
    "experience": _score_experience,
    "skills": _score_skills,
    "location": _score_location,
    "compensation": _score_compensation,
    "availability": _score_availability,
}


def score_match(candidate: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    """Pure scoring function — no database access, no side effects. Returns
    {"score": float|None, "rationale": str, "factors": {...}}. `score` is
    None when no factor had enough data on both sides to be evaluated at
    all (never a fabricated/guessed number)."""
    earned = 0.0
    possible = 0.0
    notes: list[str] = []
    factor_detail: dict[str, Any] = {}

    for name, scorer in _FACTOR_SCORERS.items():
        outcome = scorer(candidate, job)
        if outcome is None:
            factor_detail[name] = {"evaluated": False, "reason": "missing data on one or both sides"}
            continue
        points, note = outcome
        weight = _FACTOR_WEIGHTS[name]
        earned += points
        possible += weight
        notes.append(note)
        factor_detail[name] = {"evaluated": True, "points": round(points, 1), "weight": weight, "note": note}

    if possible == 0:
        return {
            "score": None,
            "rationale": "Insufficient data to compute a match score — no comparable fields were set on both the candidate and the job.",
            "factors": factor_detail,
        }

    score = round((earned / possible) * 100, 1)
    skipped = [name for name, d in factor_detail.items() if not d["evaluated"]]
    rationale = " ".join(notes)
    if skipped:
        rationale += f" (Not evaluated — missing data: {', '.join(skipped)}.)"
    return {"score": score, "rationale": rationale, "factors": factor_detail}


def generate_matches_for_job(job_id: int, candidate_status: str | None = None, min_score: float | None = None) -> list[dict[str, Any]]:
    """Score every candidate (optionally filtered by status) against one
    job, upserting each result into buildpro_matches (re-scoring updates
    the existing row via find_match rather than duplicating it). Returns
    the list of {"candidate_id", "match_id", "score", "rationale"} — jobs/
    candidates without enough shared data simply get a None score, they're
    never silently dropped from the return list."""
    job = bd.get_job(job_id)
    if job is None:
        raise ValueError(f"No job with id {job_id}")
    candidates = bd.list_candidates(status=candidate_status, limit=500)

    results = []
    for candidate in candidates:
        outcome = score_match(candidate, job)
        existing = bd.find_match(candidate["id"], job_id)
        if min_score is not None and (outcome["score"] is None or outcome["score"] < min_score):
            results.append({
                "candidate_id": candidate["id"], "match_id": existing["id"] if existing else None,
                "score": outcome["score"], "rationale": outcome["rationale"], "stored": False,
            })
            continue
        if existing:
            bd.update_match(existing["id"], match_score=outcome["score"], match_rationale=outcome["rationale"])
            match_id = existing["id"]
        else:
            match_id = bd.add_match(candidate["id"], job_id, match_score=outcome["score"], match_rationale=outcome["rationale"])
        results.append({
            "candidate_id": candidate["id"], "match_id": match_id,
            "score": outcome["score"], "rationale": outcome["rationale"], "stored": True,
        })
    return results


def generate_matches_for_candidate(candidate_id: int, job_status: str = "open", min_score: float | None = None) -> list[dict[str, Any]]:
    """Mirror of generate_matches_for_job(), scoring one candidate against
    every job in `job_status` (open by default — no point matching a
    candidate against filled/closed roles)."""
    candidate = bd.get_candidate(candidate_id)
    if candidate is None:
        raise ValueError(f"No candidate with id {candidate_id}")
    jobs = bd.list_jobs(status=job_status, limit=500)

    results = []
    for job in jobs:
        outcome = score_match(candidate, job)
        existing = bd.find_match(candidate_id, job["id"])
        if min_score is not None and (outcome["score"] is None or outcome["score"] < min_score):
            results.append({
                "job_id": job["id"], "match_id": existing["id"] if existing else None,
                "score": outcome["score"], "rationale": outcome["rationale"], "stored": False,
            })
            continue
        if existing:
            bd.update_match(existing["id"], match_score=outcome["score"], match_rationale=outcome["rationale"])
            match_id = existing["id"]
        else:
            match_id = bd.add_match(candidate_id, job["id"], match_score=outcome["score"], match_rationale=outcome["rationale"])
        results.append({
            "job_id": job["id"], "match_id": match_id,
            "score": outcome["score"], "rationale": outcome["rationale"], "stored": True,
        })
    return results
