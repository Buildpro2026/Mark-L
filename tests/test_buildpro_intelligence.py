import time

import pytest

from actions import buildpro_intelligence as bi
from actions import buildpro_data as bd


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch, tmp_path):
    monkeypatch.setattr(bd, "DB_PATH", tmp_path / "test_intel.db")


def test_report_on_empty_database_has_no_urgent_actions():
    report = bi.generate_morning_report_data()
    assert report["counts"]["candidate_count"] == 0
    assert report["top_matches"] == []
    assert report["recommended_actions"] == ["No urgent recruiting follow-ups identified."]


def test_report_includes_top_matches_with_names():
    job = bd.add_job("Electrician")
    cand = bd.add_candidate("Top Candidate")
    bd.add_match(cand, job, match_score=95, match_rationale="Great fit")

    report = bi.generate_morning_report_data()
    assert len(report["top_matches"]) == 1
    assert report["top_matches"][0]["candidate_name"] == "Top Candidate"


def test_report_flags_unsubmitted_high_scoring_matches():
    job = bd.add_job("Electrician")
    cand = bd.add_candidate("Strong Candidate")
    bd.add_match(cand, job, match_score=90, status="proposed")

    report = bi.generate_morning_report_data()
    actions = " ".join(report["recommended_actions"])
    assert "ready to submit" in actions


def test_report_does_not_flag_already_submitted_high_scores():
    job = bd.add_job("Electrician")
    cand = bd.add_candidate("Candidate")
    bd.add_match(cand, job, match_score=90, status="submitted")

    report = bi.generate_morning_report_data()
    actions = " ".join(report["recommended_actions"])
    assert "ready to submit" not in actions


def test_report_includes_candidate_and_client_followups():
    stale_cand = bd.add_candidate("Stale Candidate", status="new")
    stale_client = bd.add_client("Stale Prospect", status="prospect")
    conn = bd._connect()
    old_ts = time.time() - 30 * 86400
    conn.execute("UPDATE buildpro_candidates SET updated_ts = ? WHERE id = ?", (old_ts, stale_cand))
    conn.execute("UPDATE buildpro_clients SET updated_ts = ? WHERE id = ?", (old_ts, stale_client))
    conn.commit()
    conn.close()

    report = bi.generate_morning_report_data()
    assert len(report["candidate_followups"]) == 1
    assert len(report["client_followups"]) == 1
    actions = " ".join(report["recommended_actions"])
    assert "candidate(s)" in actions and "need follow-up" in actions


def test_report_includes_new_jobs_within_window():
    bd.add_job("Brand New Role")
    report = bi.generate_morning_report_data()
    assert len(report["new_jobs"]) == 1
    actions = " ".join(report["recommended_actions"])
    assert "new job(s) opened" in actions


def test_report_flags_unmatched_open_jobs():
    bd.add_job("Unscored Role", status="open")
    report = bi.generate_morning_report_data()
    assert len(report["unmatched_open_jobs"]) == 1
    actions = " ".join(report["recommended_actions"])
    assert "no candidate matches scored yet" in actions


def test_report_excludes_matched_jobs_from_unmatched_list():
    job = bd.add_job("Scored Role", status="open")
    cand = bd.add_candidate("Candidate")
    bd.add_match(cand, job, match_score=50)
    report = bi.generate_morning_report_data()
    assert report["unmatched_open_jobs"] == []


def test_highest_priority_prospects_ranks_construction_related_first():
    bd.add_client("Software Prospect", status="prospect", industry="Software")
    bd.add_client("Construction Prospect", status="prospect", industry="Construction")
    prospects = bi.get_highest_priority_prospects()
    assert prospects[0]["name"] == "Construction Prospect"
    assert prospects[0]["likely_construction_related"] is True
    assert prospects[1]["likely_construction_related"] is False


def test_report_includes_last_hubspot_sync_state():
    bd.record_sync_run("candidates", "hubspot", created_count=5)
    report = bi.generate_morning_report_data()
    assert report["last_hubspot_sync"]["candidates"]["created_count"] == 5
    assert report["last_hubspot_sync"]["clients"] is None  # never synced


def test_report_never_sends_anything_pure_data_only():
    """No email/SMS/social-posting side effects — generate_morning_report_data
    is read-only over buildpro_data."""
    report = bi.generate_morning_report_data()
    assert isinstance(report, dict)
    assert "generated_ts" in report


def test_report_counts_match_summary():
    bd.add_candidate("A")
    bd.add_client("B", status="active")
    bd.add_client("C", status="prospect")
    bd.add_job("D", status="open")

    report = bi.generate_morning_report_data()
    summary = bd.summary()
    assert report["counts"]["candidate_count"] == summary["candidate_count"]
    assert report["counts"]["client_count"] == summary["client_count"]
    assert report["counts"]["prospect_count"] == summary["prospect_count"]
    assert report["counts"]["active_jobs"] == summary["active_jobs"]
