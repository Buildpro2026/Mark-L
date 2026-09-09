"""The daily BuildPro matching run.

buildpro_matching.score_match() was already a good scorer — weighted
factors, a note per factor, score=None rather than a fabricated number
when neither side has comparable data. What was missing was job intake
(add_job inserted whatever it was handed, so the same posting from two
sources became two jobs and double-counted every match) and the run
itself: nothing iterated open jobs against candidates and said what to do.

The rule these pin: every number in the report is COUNTED. "8 strong
matches" means eight scores came back at or above the threshold. If there
are three, it says three.
"""
import pathlib

import pytest

from actions import buildpro_daily as bpd
from actions import buildpro_data as bd


@pytest.fixture(autouse=True)
def _db(monkeypatch, tmp_path):
    monkeypatch.setattr(bd, "DB_PATH", tmp_path / "bp.db")


@pytest.fixture
def _no_live_sources(monkeypatch):
    """Matching tests must not spend the run on blocked network calls —
    only the two discovery tests exercise the live source."""
    monkeypatch.setattr(bpd, "available_sources", lambda: [])


JOB = {
    "title": "Senior Project Manager — Data Center",
    "company": "Turner", "location": "Phoenix, AZ",
    "description": "Lead mission critical data center construction. 10+ years experience.",
    "requirements": ["scheduling", "budgeting", "data center"],
    "salary": "$160,000", "url": "https://example.com/jobs/1",
}


def _strong_candidate():
    return bd.add_candidate(
        "Dana Reeves", email="dana@example.com", title="Senior Project Manager",
        specialty="data center", years_experience=12, location="Phoenix, AZ",
        skills="scheduling, budgeting, data center",
        desired_compensation="$155,000", availability="available")


def _weak_candidate():
    return bd.add_candidate("Sam Lee", email="sam@example.com", title="Estimator",
                            specialty="residential", years_experience=3,
                            location="Boston, MA", skills="takeoff")


# ══ NORMALISATION ════════════════════════════════════════════════════════

def test_a_posting_is_normalised_into_the_scorer_fields():
    n = bpd.normalize_job(JOB, source="test_board")
    assert n["title"] == "Senior Project Manager — Data Center"
    assert n["location"] == "Phoenix, AZ"
    assert n["required_skills"] == "scheduling, budgeting, data center"
    assert n["compensation"] == "$160,000"
    assert n["specialty"] == "data center"


def test_years_of_experience_are_read_out_of_the_description():
    # Postings state this in prose far more often than in a field, and
    # leaving it unparsed made the scorer skip experience on almost every
    # real job while the posting plainly said it.
    assert bpd.normalize_job(JOB)["min_years_experience"] == 10


def test_a_posting_with_no_title_is_rejected_rather_than_stored():
    assert bpd.normalize_job({"company": "Nobody"}) == {}


def test_a_field_the_posting_does_not_have_is_absent_not_defaulted():
    n = bpd.normalize_job({"title": "Estimator"})
    for invented in ("compensation", "required_skills", "min_years_experience"):
        assert invented not in n, f"{invented} was invented"


def test_seniority_is_derived_only_when_the_title_implies_it():
    assert bpd.seniority_of("VP of Construction") == "executive"
    assert bpd.seniority_of("Senior Superintendent") == "senior"
    assert bpd.seniority_of("Widget Wrangler") == "", "an unknown title was guessed at"


def test_project_types_come_from_the_text():
    assert "data center" in bpd.project_types_in("Mission critical data center build")
    assert bpd.project_types_in("A generic role") == []


# ══ DEDUPLICATION ════════════════════════════════════════════════════════

def test_the_same_posting_reworded_is_the_same_job():
    # A repost with new copy is the same role; treating it as new is how a
    # board of forty postings becomes a board of four hundred.
    first = bpd.job_fingerprint(JOB)
    reposted = bpd.job_fingerprint({**JOB, "description": "Totally reworded copy.",
                                    "url": "https://example.com/jobs/999"})
    assert first == reposted


def test_a_different_company_is_a_different_job():
    assert bpd.job_fingerprint(JOB) != bpd.job_fingerprint({**JOB, "company": "Skanska"})


def test_a_different_location_is_a_different_job():
    assert bpd.job_fingerprint(JOB) != bpd.job_fingerprint({**JOB, "location": "Austin, TX"})


def test_a_duplicate_inside_one_batch_is_stored_once():
    out = bpd.intake_jobs([JOB, {**JOB, "description": "reworded"}], source="test")
    assert out["stored"] == 1
    assert out["skipped"] == 1
    assert len(bd.list_jobs(status="open")) == 1


def test_a_posting_seen_on_a_later_run_updates_rather_than_duplicates():
    bpd.intake_jobs([JOB], source="test")
    out = bpd.intake_jobs([{**JOB, "salary": "$175,000"}], source="test")
    assert out["stored"] == 0 and out["updated"] == 1
    jobs = bd.list_jobs(status="open")
    assert len(jobs) == 1
    assert "175,000" in (jobs[0].get("compensation") or "")


def test_one_bad_posting_does_not_stop_the_batch():
    out = bpd.intake_jobs([JOB, {"company": "no title here"},
                           {**JOB, "title": "Estimator", "company": "Other"}], source="test")
    assert out["stored"] == 2
    assert out["skipped"] == 1


def test_provenance_is_preserved_on_the_stored_job():
    bpd.intake_jobs([JOB], source="test_board")
    source = bd.list_jobs(status="open")[0]["source"]
    assert "test_board" in source
    assert "fp:" in source
    assert "example.com/jobs/1" in source
    assert "company:Turner" in source


# ══ DISCOVERY IS HONEST ══════════════════════════════════════════════════

def test_discovery_reports_unreachable_rather_than_a_clean_zero(monkeypatch):
    # A research-backed source needs no credential, so one is always
    # configured. When it cannot reach the web it returns zero postings —
    # and "could not reach the web" must never be reported as "there were
    # no jobs". Driven by a stub rather than real blocked network calls.
    class _Unreachable(bpd.JobSource):
        name = "stub"
        last_state = bpd.UNAVAILABLE
        def is_configured(self): return True
        def fetch(self, limit=50): return []

    monkeypatch.setattr(bpd, "available_sources", lambda: [_Unreachable()])
    out = bpd.discover_jobs()
    assert out["ok"] is False
    assert out["state"] == bpd.UNAVAILABLE
    assert out["stored"] == 0
    assert "No job source could be reached" in out["detail"]
    # And the per-source state is not overwritten by intake's own "OK".
    assert out["sources"][0]["state"] == bpd.UNAVAILABLE


def test_a_reachable_source_that_found_nothing_is_not_reported_as_unreachable(monkeypatch):
    class _Empty(bpd.JobSource):
        name = "stub"
        last_state = bpd.OK
        def is_configured(self): return True
        def fetch(self, limit=50): return []

    monkeypatch.setattr(bpd, "available_sources", lambda: [_Empty()])
    out = bpd.discover_jobs()
    assert out["ok"] is True and out["state"] == bpd.OK


def test_a_configured_source_is_available_without_a_credential():
    sources = bpd.available_sources()
    assert sources and sources[0].is_configured() is True


def test_discovery_never_invents_postings(_no_live_sources):
    bpd.discover_jobs()
    assert bd.list_jobs() == [], "discovery stored a job it never retrieved"


# ══ THE RUN ══════════════════════════════════════════════════════════════

def test_a_strong_candidate_is_matched_and_ranked_first():
    bpd.intake_jobs([JOB], source="test")
    _strong_candidate()
    _weak_candidate()

    result = bpd.run_daily_matching()
    assert result["ok"] is True
    assert result["jobs_evaluated"] == 1
    assert result["candidates_evaluated"] == 2
    assert result["strong_matches"] >= 1
    assert result["top"]["candidate_name"] == "Dana Reeves"
    assert result["top"]["score"] >= bpd.STRONG_MATCH_SCORE


def test_a_weak_pairing_is_kept_out_of_the_report():
    # generate_matches_for_job upserts every pairing so the match table
    # stays complete; min_score governs what is worth REPORTING. Without
    # the filter a 0% pairing appeared beside an 87% one.
    bpd.intake_jobs([JOB], source="test")
    _strong_candidate()
    _weak_candidate()
    result = bpd.run_daily_matching(min_score=50.0)
    assert all(m["score"] >= 50.0 for m in result["matches"])
    assert not any(m.get("candidate_name") == "Sam Lee" for m in result["matches"])


def test_the_counts_are_counted_not_asserted():
    bpd.intake_jobs([JOB, {**JOB, "title": "Estimator", "company": "Other"}], source="test")
    for i in range(3):
        bd.add_candidate(f"Person {i}", email=f"p{i}@example.com", title="Estimator")
    result = bpd.run_daily_matching()
    assert result["jobs_evaluated"] == 2
    assert result["candidates_evaluated"] == 3


def test_no_jobs_produces_an_honest_empty_result():
    _strong_candidate()
    result = bpd.run_daily_matching()
    assert result["ok"] is True
    assert result["jobs_evaluated"] == 0
    assert result["matches"] == []


def test_a_job_that_cannot_be_scored_does_not_stop_the_run(monkeypatch):
    from actions import buildpro_matching
    bpd.intake_jobs([JOB, {**JOB, "title": "Estimator", "company": "Other"}], source="test")
    _strong_candidate()

    calls = {"n": 0}

    def _flaky(job_id, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("scorer blew up")
        return []
    monkeypatch.setattr(buildpro_matching, "generate_matches_for_job", _flaky)

    result = bpd.run_daily_matching()
    assert result["ok"] is True
    assert len(result["failed_jobs"]) == 1
    assert calls["n"] == 2, "the run stopped at the first failure"


def test_a_broken_database_is_reported_not_faked(monkeypatch):
    monkeypatch.setattr(bd, "list_jobs",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("no such table")))
    result = bpd.run_daily_matching()
    assert result["ok"] is False
    assert result["state"] == bpd.FAILED
    assert result["matches"] == []


# ══ THE REPORT ═══════════════════════════════════════════════════════════

def test_the_report_carries_the_real_numbers_and_the_evidence():
    bpd.intake_jobs([JOB], source="test")
    _strong_candidate()
    report = bpd.format_daily_report(bpd.run_daily_matching())

    assert "DAILY BUILDPRO MATCHES" in report
    assert "1 open job(s) evaluated" in report
    assert "1 candidate(s) evaluated" in report
    assert "Dana Reeves" in report
    assert "Why:" in report
    assert "Recommended action:" in report
    # The evidence is the scorer's own factor notes, not generated prose.
    assert "data center" in report.lower()


def test_an_empty_morning_says_so_rather_than_padding():
    report = bpd.format_daily_report(bpd.run_daily_matching())
    assert "No open jobs on file" in report
    assert "strong match" in report   # the count is still reported, as zero


def test_no_candidates_is_reported_distinctly():
    bpd.intake_jobs([JOB], source="test")
    report = bpd.format_daily_report(bpd.run_daily_matching())
    assert "No candidates on file" in report


def test_a_failed_run_is_never_reported_as_matches():
    report = bpd.format_daily_report({"ok": False, "detail": "database is locked"})
    assert "did not complete" in report
    assert "locked" in report


def test_the_recommended_action_scales_with_the_score():
    assert bpd.recommended_action({"score": 96}) == "Contact candidate today"
    assert bpd.recommended_action({"score": 80}) == "Contact candidate"
    assert bpd.recommended_action({"score": 52}) == "Review before contacting"


def test_the_run_records_its_outcome_for_learning(monkeypatch, _no_live_sources):
    from actions import ceo_decision
    recorded = []
    monkeypatch.setattr(ceo_decision, "record_outcome",
                        lambda item, **k: recorded.append((item, k)) or {"ok": True})
    bpd.intake_jobs([JOB], source="test")
    _strong_candidate()
    bpd.run_and_report()
    assert recorded, "the matching outcome never reached memory"
    assert recorded[0][0]["source"] == "buildpro_daily_matching"


# ══ THROUGH THE REAL TOOL EXECUTOR ═══════════════════════════════════════

def test_the_daily_report_runs_through_the_real_tool_executor(_no_live_sources):
    import asyncio
    from core.headless.tool_executor import ToolExecutor, ToolContext

    bpd.intake_jobs([JOB], source="test")
    _strong_candidate()
    result = asyncio.run(ToolExecutor(ToolContext()).execute(
        "buildpro_matching", {"action": "daily_report"}))

    assert "DAILY BUILDPRO MATCHES" in result
    assert "Dana Reeves" in result
    # No live source in this test, and the tool says discovery did not
    # retrieve anything rather than implying it ran cleanly.
    assert "did not retrieve" in result.lower() or "not_configured" in result.lower()


def test_job_intake_runs_through_the_real_tool_executor():
    import asyncio
    from core.headless.tool_executor import ToolExecutor, ToolContext

    result = asyncio.run(ToolExecutor(ToolContext()).execute(
        "buildpro_matching", {"action": "intake_jobs", "jobs": [JOB], "source": "voice"}))
    assert "1 new" in result
    assert len(bd.list_jobs(status="open")) == 1
