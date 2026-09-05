"""Regression coverage for dashboard/server.py's _verify_to_health_status():
the single place that decides whether the /3d health panel calls a Buffer
(or HubSpot) failure AUTH_FAILED, RATE_LIMITED, or RUNTIME_FAILED. A wrong
mapping here silently mislabels a real Buffer-side token rejection as "our
bug" (RUNTIME_FAILED) instead of telling a human to go fix the token -- which
is exactly what almost happened when buffer_integration's status strings
were changed from "UNAVAILABLE:401"/"UNAVAILABLE:403" to "AUTH_FAILED:401"/
"AUTH_FAILED:403" without updating this function to match."""
from dashboard.server import _verify_to_health_status


def test_not_configured_short_circuits_before_any_status_check():
    assert _verify_to_health_status({"configured": False, "status": "NOT CONFIGURED"}) == "NOT_CONFIGURED"


def test_buffer_style_auth_failed_status_is_classified_auth_failed():
    assert _verify_to_health_status({"configured": True, "status": "AUTH_FAILED:401"}) == "AUTH_FAILED"
    assert _verify_to_health_status({"configured": True, "status": "AUTH_FAILED:403"}) == "AUTH_FAILED"


def test_hubspot_style_unavailable_401_403_is_still_classified_auth_failed():
    """hubspot_integration.verify_hubspot() still returns the older
    "UNAVAILABLE:<code>" shape (untouched by the Buffer fix) -- this mapping
    must keep honoring both status vocabularies."""
    assert _verify_to_health_status({"configured": True, "status": "UNAVAILABLE:401"}) == "AUTH_FAILED"
    assert _verify_to_health_status({"configured": True, "status": "UNAVAILABLE:403"}) == "AUTH_FAILED"


def test_rate_limited_status_is_classified_rate_limited_not_auth_failed():
    assert _verify_to_health_status({"configured": True, "status": "RATE_LIMITED:429"}) == "RATE_LIMITED"


def test_other_failures_fall_back_to_runtime_failed():
    assert _verify_to_health_status({"configured": True, "status": "UNAVAILABLE:500"}) == "RUNTIME_FAILED"
    assert _verify_to_health_status({"configured": True, "status": "ERROR:boom"}) == "RUNTIME_FAILED"
