"""A strong match on a freshly-uploaded candidate reaches Lee and the
Brain immediately, rather than waiting for the next scheduled cycle.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    from actions import buildpro_data, operating_memory
    monkeypatch.setattr(buildpro_data, "DB_PATH", tmp_path / "bp.db")
    monkeypatch.setattr(operating_memory, "DB_PATH", tmp_path / "om.db")


def test_a_strong_match_notifies_and_is_remembered(monkeypatch, tmp_path):
    from actions import cross_system as cs, buildpro_data as bd, notifications, brain_memory

    job_id = bd.add_job("Senior Project Manager", location="Phoenix, AZ", source="test")
    candidate_id = bd.add_candidate("Dana Reeves", email="dana@example.com",
                                    title="Senior Project Manager", years_experience=12,
                                    location="Phoenix, AZ")

    alerts = []
    monkeypatch.setattr(notifications, "business_alert",
                        lambda **k: alerts.append(k) or {"ok": True})
    monkeypatch.setattr(cs, "_strong_match_threshold", lambda: 0.0)   # force "strong"

    result = cs.match_new_candidate(candidate_id)
    assert result["ok"] is True
    if result["matches"]:
        assert alerts, "a strong match on intake was never surfaced"
        remembered = brain_memory.recall_for("Dana Reeves Senior Project Manager")
        assert remembered, "the match outcome never reached the Brain"


def test_a_weak_match_does_not_notify(monkeypatch, tmp_path):
    from actions import cross_system as cs, buildpro_data as bd, notifications

    bd.add_job("Estimator", location="Boston, MA", source="test")
    candidate_id = bd.add_candidate("Someone Else", email="x@example.com",
                                    title="Totally Unrelated Role")

    alerts = []
    monkeypatch.setattr(notifications, "business_alert",
                        lambda **k: alerts.append(k) or {"ok": True})

    cs.match_new_candidate(candidate_id)
    assert alerts == []


def test_a_notification_failure_does_not_break_intake(monkeypatch, tmp_path):
    from actions import cross_system as cs, buildpro_data as bd, notifications

    bd.add_job("Senior Project Manager", location="Phoenix, AZ", source="test")
    candidate_id = bd.add_candidate("Dana Reeves", email="dana@example.com",
                                    title="Senior Project Manager", years_experience=12,
                                    location="Phoenix, AZ")

    monkeypatch.setattr(cs, "_strong_match_threshold", lambda: 0.0)
    monkeypatch.setattr(notifications, "business_alert",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("twilio down")))

    result = cs.match_new_candidate(candidate_id)   # must not raise
    assert result["ok"] is True


def test_scores_below_min_score_are_never_returned_as_matches(monkeypatch, tmp_path):
    from actions import cross_system as cs, buildpro_data as bd

    bd.add_job("Estimator", location="Boston, MA", source="test")
    candidate_id = bd.add_candidate("Nobody Related", email="n@example.com",
                                    title="Totally Different Field")
    result = cs.match_new_candidate(candidate_id, min_score=90.0)
    assert all(float(m["score"]) >= 90.0 for m in result["matches"])
