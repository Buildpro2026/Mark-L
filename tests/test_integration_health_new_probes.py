"""§9: integration_health gets live probes for web_research and the BuildPro
sqlite store, not just the credential-backed integrations it already had.
Neither is env-var-gated — the honest question for each is "does it work
right now", so these confirm live success/failure both surface correctly.
"""
import pytest

from actions import integration_health as health


def test_web_research_probe_reports_configured_on_a_successful_search(monkeypatch):
    from actions import web_research
    monkeypatch.setattr(web_research, "search", lambda q, max_results=1: {"ok": True, "sources": [{}]})
    result = health._web_research()
    assert result["state"] == health.CONFIGURED


def test_web_research_probe_reports_unavailable_when_search_fails(monkeypatch):
    from actions import web_research
    monkeypatch.setattr(web_research, "search",
                         lambda q, max_results=1: {"ok": False, "detail": "network unreachable"})
    result = health._web_research()
    assert result["state"] == health.UNAVAILABLE
    assert "network unreachable" in result["detail"]


def test_buildpro_store_probe_reports_configured_against_a_real_temp_db(monkeypatch, tmp_path):
    from actions import buildpro_data as bd
    monkeypatch.setattr(bd, "DB_PATH", tmp_path / "health_check.db")
    result = health._buildpro_store()
    assert result["state"] == health.CONFIGURED


def test_buildpro_store_probe_reports_unavailable_when_connect_raises(monkeypatch):
    from actions import buildpro_data as bd

    def _boom():
        raise OSError("disk full")
    monkeypatch.setattr(bd, "_connect", _boom)
    result = health._buildpro_store()
    assert result["state"] == health.UNAVAILABLE
    assert "disk full" in result["detail"]


def test_job_discovery_capability_requires_both_new_probes():
    assert set(health.CAPABILITY_REQUIREMENTS["job_discovery"]) == {"web_research", "buildpro_store"}


def test_check_all_never_raises_even_if_a_new_probe_throws(monkeypatch):
    from actions import web_research
    def _boom(q, max_results=1):
        raise RuntimeError("dns failure")
    monkeypatch.setattr(web_research, "search", _boom)
    report = health.check_all()
    # A raising probe is isolated, not fatal to the whole survey.
    assert report["integrations"]["web_research"]["state"] == health.UNAVAILABLE
    assert "web_research" in report["degraded"]
    assert report["capabilities"]["job_discovery"] is False
