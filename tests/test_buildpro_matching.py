import pytest

from actions import buildpro_matching as mm
from actions import buildpro_data as bd


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch, tmp_path):
    monkeypatch.setattr(bd, "DB_PATH", tmp_path / "test_matching.db")


# ── score_match: pure scoring, representative data ───────────────────────

def test_perfect_match_scores_high():
    candidate = {
        "title": "Electrician", "specialty": "electrical", "years_experience": 8,
        "skills": "osha-30, conduit bending", "location": "Houston, TX",
        "desired_compensation": "$75,000", "availability": "available",
    }
    job = {
        "title": "Electrician", "specialty": "electrical", "min_years_experience": 5,
        "required_skills": "osha-30, conduit bending", "location": "Houston, TX",
        "compensation": "$65,000-90,000",
    }
    result = mm.score_match(candidate, job)
    assert result["score"] == 100.0
    assert "Title matches exactly" in result["rationale"]


def test_completely_mismatched_still_produces_a_score_not_none():
    candidate = {
        "title": "Plumber", "specialty": "plumbing", "years_experience": 1,
        "skills": "pipefitting", "location": "Dallas, TX",
        "desired_compensation": "$120,000", "availability": "not_looking",
    }
    job = {
        "title": "Electrician", "specialty": "electrical", "min_years_experience": 10,
        "required_skills": "osha-30", "location": "Houston, TX",
        "compensation": "$40,000-50,000",
    }
    result = mm.score_match(candidate, job)
    assert result["score"] is not None
    assert result["score"] < 20


def test_no_comparable_data_returns_none_score_not_fabricated():
    result = mm.score_match({}, {})
    assert result["score"] is None
    assert "insufficient data" in result["rationale"].lower()
    assert all(not f["evaluated"] for f in result["factors"].values())


def test_sparse_data_only_scores_on_available_factors():
    candidate = {"title": "Laborer"}
    job = {"title": "General Laborer"}
    result = mm.score_match(candidate, job)
    assert result["score"] is not None
    assert result["factors"]["title"]["evaluated"] is True
    assert result["factors"]["specialty"]["evaluated"] is False
    assert result["factors"]["experience"]["evaluated"] is False
    assert "missing data" in result["rationale"].lower()


def test_experience_partial_credit_when_close():
    candidate = {"years_experience": 3}
    job = {"min_years_experience": 5}
    result = mm.score_match(candidate, job)
    assert result["factors"]["experience"]["points"] == pytest.approx(10.0)  # half credit, within 2 years


def test_experience_zero_credit_when_far_below():
    candidate = {"years_experience": 1}
    job = {"min_years_experience": 10}
    result = mm.score_match(candidate, job)
    assert result["factors"]["experience"]["points"] == 0.0


def test_skills_partial_overlap_gives_proportional_credit():
    candidate = {"skills": "osha-30, welding"}
    job = {"required_skills": "osha-30, welding, crane operation, rigging"}
    result = mm.score_match(candidate, job)
    # 2 of 4 required skills matched -> half of the skills weight (20 * 0.5 = 10)
    assert result["factors"]["skills"]["points"] == pytest.approx(10.0)


def test_location_remote_always_matches():
    candidate = {"location": "Remote"}
    job = {"location": "Houston, TX"}
    result = mm.score_match(candidate, job)
    assert result["factors"]["location"]["points"] == 10.0


def test_compensation_numeric_comparison_within_range():
    candidate = {"desired_compensation": "$60,000"}
    job = {"compensation": "$55,000-70,000"}
    result = mm.score_match(candidate, job)
    assert result["factors"]["compensation"]["points"] == 5.0


def test_compensation_numeric_comparison_exceeds_range():
    candidate = {"desired_compensation": "$150,000"}
    job = {"compensation": "$55,000-70,000"}
    result = mm.score_match(candidate, job)
    assert result["factors"]["compensation"]["points"] == 0.0


def test_compensation_non_numeric_gets_neutral_partial_credit():
    candidate = {"desired_compensation": "negotiable"}
    job = {"compensation": "competitive"}
    result = mm.score_match(candidate, job)
    assert result["factors"]["compensation"]["points"] == 2.5


def test_availability_not_looking_scores_zero():
    candidate = {"availability": "not_looking"}
    job = {"title": "x"}  # ensure at least one other factor is skipped, not this one
    result = mm.score_match(candidate, job)
    assert result["factors"]["availability"]["points"] == 0.0


def test_never_scores_a_factor_when_only_one_side_has_data():
    candidate = {"title": "Electrician"}   # job has no title
    job = {"specialty": "electrical"}       # candidate has no specialty
    result = mm.score_match(candidate, job)
    assert result["factors"]["title"]["evaluated"] is False
    assert result["factors"]["specialty"]["evaluated"] is False


# ── generate_matches_for_job / generate_matches_for_candidate (persistence) ──

def test_generate_matches_for_job_scores_all_candidates_and_stores():
    job = bd.add_job("Electrician", specialty="electrical", min_years_experience=5)
    c1 = bd.add_candidate("Strong Fit", specialty="electrical", years_experience=8)
    c2 = bd.add_candidate("Weak Fit", specialty="plumbing", years_experience=1)

    results = mm.generate_matches_for_job(job)
    assert len(results) == 2
    stored_matches = bd.list_matches(job_id=job)
    assert len(stored_matches) == 2


def test_generate_matches_for_job_updates_existing_match_not_duplicate():
    job = bd.add_job("Electrician", specialty="electrical")
    cand = bd.add_candidate("Candidate", specialty="electrical")

    mm.generate_matches_for_job(job)
    first_count = len(bd.list_matches(job_id=job))
    mm.generate_matches_for_job(job)  # re-run
    second_count = len(bd.list_matches(job_id=job))
    assert first_count == 1
    assert second_count == 1   # re-scored in place, not duplicated


def test_generate_matches_for_job_respects_min_score_filter():
    job = bd.add_job("Electrician", specialty="electrical", min_years_experience=10)
    bd.add_candidate("Bad Fit", specialty="plumbing", years_experience=1)

    results = mm.generate_matches_for_job(job, min_score=90)
    assert results[0]["stored"] is False
    assert bd.list_matches(job_id=job) == []


def test_generate_matches_for_job_raises_for_unknown_job():
    with pytest.raises(ValueError):
        mm.generate_matches_for_job(99999)


def test_generate_matches_for_candidate_scores_open_jobs_only():
    cand = bd.add_candidate("Candidate", specialty="electrical")
    open_job = bd.add_job("Open Role", specialty="electrical", status="open")
    closed_job = bd.add_job("Closed Role", specialty="electrical", status="closed")

    results = mm.generate_matches_for_candidate(cand)
    job_ids = {r["job_id"] for r in results}
    assert open_job in job_ids
    assert closed_job not in job_ids


def test_generate_matches_for_candidate_raises_for_unknown_candidate():
    with pytest.raises(ValueError):
        mm.generate_matches_for_candidate(99999)


def test_generate_matches_stores_rationale_and_score_on_match_row():
    job = bd.add_job("Electrician", specialty="electrical", min_years_experience=5)
    cand = bd.add_candidate("Fit", specialty="electrical", years_experience=8)

    mm.generate_matches_for_job(job)
    match = bd.list_matches(job_id=job)[0]
    assert match["match_score"] is not None
    assert match["match_rationale"]
    assert "specialty" in match["match_rationale"].lower()
