"""WebResearchJobSource capturing the structured fields §1 asks for:
employment type, date posted, freshness, confidence and evidence — not
just title/company/location/salary, and none of it fabricated when a
source doesn't state it.
"""
import pytest

from actions import buildpro_daily as bpd
from actions import buildpro_data as bd
from actions import web_research as wr


@pytest.fixture(autouse=True)
def _db(monkeypatch, tmp_path):
    monkeypatch.setattr(bd, "DB_PATH", tmp_path / "bp.db")


def _fake_research_result(**overrides):
    base = {
        "ok": True,
        "results": [{
            "source_url": "https://jobs.example/vp-construction",
            "source_type": "job_board",
            "source_reliability": 0.7,
            "freshness": {"state": "CURRENT", "published_at": "2026-09-01T00:00:00+00:00",
                         "age_days": 8},
            "fields": {
                "job_title": wr.finding("job_title", "VP of Construction", wr.OBSERVED,
                                        "https://jobs.example/vp-construction"),
                "company": wr.finding("company", "Turner", wr.OBSERVED,
                                      "https://jobs.example/vp-construction"),
                "location": wr.finding("location", "Dallas, TX", wr.OBSERVED,
                                       "https://jobs.example/vp-construction"),
                "compensation": wr.finding("compensation", "$220000", wr.OBSERVED,
                                          "https://jobs.example/vp-construction"),
                "employment_type": wr.finding("employment_type", "Full-Time", wr.OBSERVED,
                                              "https://jobs.example/vp-construction"),
                "summary": wr.finding("summary", "Lead all construction operations.", wr.REPORTED,
                                     "https://jobs.example/vp-construction"),
            },
        }],
    }
    base.update(overrides)
    return base


def test_all_leadership_roles_are_searched_by_default():
    source = bpd.WebResearchJobSource()
    assert set(source.roles) == set(bpd.LEADERSHIP_ROLES)
    assert len(source.roles) >= 9


def test_fetch_captures_the_full_structured_fields(monkeypatch):
    monkeypatch.setattr(wr, "research", lambda q, **k: _fake_research_result())
    source = bpd.WebResearchJobSource(roles=["VP of Construction"])
    postings = source.fetch(limit=3)

    assert len(postings) == 1
    p = postings[0]
    assert p["title"] == "VP of Construction"
    assert p["company"] == "Turner"
    assert p["employment_type"] == "Full-Time"
    assert p["date_posted"] == "2026-09-01T00:00:00+00:00"
    assert p["freshness_state"] == "CURRENT"
    assert p["confidence"] == 0.7
    assert p["evidence"] == "OBSERVED"
    assert source.last_state == bpd.OK


def test_a_missing_field_is_not_fabricated(monkeypatch):
    result = _fake_research_result()
    result["results"][0]["fields"]["employment_type"] = wr.unknown("employment_type")
    result["results"][0]["freshness"] = {"state": "UNKNOWN", "published_at": None}
    monkeypatch.setattr(wr, "research", lambda q, **k: result)

    source = bpd.WebResearchJobSource(roles=["VP of Construction"])
    posting = source.fetch(limit=3)[0]
    assert posting["employment_type"] == ""
    assert posting["date_posted"] == ""
    assert posting["freshness_state"] == "UNKNOWN"


def test_the_structured_fields_survive_into_the_stored_job(monkeypatch):
    monkeypatch.setattr(wr, "research", lambda q, **k: _fake_research_result())
    source = bpd.WebResearchJobSource(roles=["VP of Construction"])
    postings = source.fetch(limit=3)

    out = bpd.intake_jobs(postings, source="web_research")
    assert out["stored"] == 1
    job = bd.list_jobs(status="open")[0]
    assert job["employment_type"] == "Full-Time"
    assert "posted:2026-09-01" in job["source"]
    assert "freshness:CURRENT" in job["source"]
    assert "confidence:0.70" in job["source"]
    assert "evidence:OBSERVED" in job["source"]


def test_an_unreachable_role_search_does_not_fabricate_a_posting(monkeypatch):
    monkeypatch.setattr(wr, "research", lambda q, **k: {"ok": False, "detail": "blocked"})
    source = bpd.WebResearchJobSource(roles=["VP of Construction", "Director of Construction"])
    postings = source.fetch(limit=3)
    assert postings == []
    assert source.last_state == bpd.UNAVAILABLE
