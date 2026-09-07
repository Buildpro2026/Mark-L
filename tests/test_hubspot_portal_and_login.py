"""HubSpot: the right portal, and API access never standing in for a login.

The failure this guards against is quiet by nature. A token can be valid,
every call can succeed, and JARVIS can be reading a sandbox or another
company's CRM the whole time — with every health check green. The second
failure is subtler still: reporting "HubSpot works" on API access alone,
when the human cannot actually sign in to the portal.
"""
import pytest

from actions import business_intent as intent
from actions import hubspot_integration as hs
from actions import integration_health as ih


def _portal(monkeypatch, portal_id="1234567", ok=True):
    def _fake_request(method, path, **kwargs):
        if not ok:
            return {"ok": False, "state": "UNAVAILABLE", "detail": "401 Unauthorized",
                    "status_code": 401}
        if path.startswith("/account-info"):
            return {"ok": True, "data": {"portalId": int(portal_id), "uiDomain": "app.hubspot.com",
                                         "accountType": "STANDARD", "timeZone": "US/Central"}}
        return {"ok": True, "data": {"results": []}}
    monkeypatch.setattr(hs, "is_configured", lambda: True)
    monkeypatch.setattr(hs, "_request", _fake_request)


# ══ PORTAL IDENTITY ══════════════════════════════════════════════════════

def test_the_portal_id_is_actually_reported(monkeypatch):
    """Before this, nothing surfaced which portal the token belonged to."""
    _portal(monkeypatch, "7654321")
    identity = hs.get_portal_identity()
    assert identity["verified"] is True
    assert identity["portal_id"] == "7654321"
    assert identity["ui_domain"] == "app.hubspot.com"


def test_an_unpinned_expected_portal_is_unverified_not_success(monkeypatch):
    """'We never checked' and 'we checked and it matched' are different
    facts. Only one means JARVIS is reading the right CRM."""
    _portal(monkeypatch, "1111111")
    from core.headless import config
    monkeypatch.setattr(config, "HUBSPOT_EXPECTED_PORTAL_ID", None)
    result = hs.verify_expected_portal()
    assert result["portal_match"] == "UNVERIFIED"
    assert "1111111" in result["detail"], "the detail should name the id to pin"


def test_a_matching_portal_is_reported_as_a_match(monkeypatch):
    _portal(monkeypatch, "2222222")
    from core.headless import config
    monkeypatch.setattr(config, "HUBSPOT_EXPECTED_PORTAL_ID", "2222222")
    assert hs.verify_expected_portal()["portal_match"] == "MATCH"


def test_a_token_on_the_wrong_portal_is_a_failure_not_healthy(monkeypatch):
    """Every call succeeds, so only an explicit comparison catches this."""
    _portal(monkeypatch, "9999999")
    from core.headless import config
    monkeypatch.setattr(config, "HUBSPOT_EXPECTED_PORTAL_ID", "2222222")

    result = hs.verify_expected_portal()
    assert result["portal_match"] == "MISMATCH"

    health = ih._probe("hubspot", ih._hubspot)
    assert health["state"] == ih.AUTH_ERROR, (
        "a token reading the wrong company's CRM must not report CONFIGURED"
    )


def test_an_unreachable_hubspot_is_not_a_portal_match(monkeypatch):
    _portal(monkeypatch, ok=False)
    assert hs.verify_expected_portal()["portal_match"] == "UNKNOWN"


# ══ API vs HUMAN LOGIN ═══════════════════════════════════════════════════

def test_health_never_claims_the_human_login_works(monkeypatch):
    """A private-app token cannot see logins, auth methods or 2FA. The
    health report must say so on every result rather than let silence be
    read as success."""
    _portal(monkeypatch, "3333333")
    from core.headless import config
    monkeypatch.setattr(config, "HUBSPOT_EXPECTED_PORTAL_ID", "3333333")

    health = ih._probe("hubspot", ih._hubspot)
    assert health["state"] == ih.CONFIGURED           # API is genuinely fine
    assert health["human_login_verifiable"] is False   # and says nothing about login
    assert "HubSpot UI" in health["human_login_note"]


def test_an_unconfigured_hubspot_also_reports_the_login_caveat():
    from unittest.mock import patch
    with patch.object(hs, "is_configured", return_value=False):
        health = ih._probe("hubspot", ih._hubspot)
    assert health["state"] == ih.NOT_CONFIGURED
    assert health["human_login_verifiable"] is False


# ══ OWNERS / USERS ═══════════════════════════════════════════════════════

def _owners(monkeypatch, rows):
    monkeypatch.setattr(hs, "is_configured", lambda: True)
    monkeypatch.setattr(hs, "_request",
                        lambda m, p, **k: {"ok": True, "data": {"results": rows}})


def test_owners_are_retrievable_with_their_active_state(monkeypatch):
    _owners(monkeypatch, [
        {"id": "1", "email": "lee@buildprorecruiters.com", "firstName": "Lee", "archived": False},
        {"id": "2", "email": "old@buildprorecruiters.com", "firstName": "Old", "archived": True},
    ])
    result = hs.get_owners()
    assert result["ok"] is True and result["count"] == 2
    archived = [o for o in result["owners"] if o["archived"]]
    assert [o["email"] for o in archived] == ["old@buildprorecruiters.com"]


def test_an_address_absent_from_the_portal_cannot_be_a_login(monkeypatch):
    """HubSpot requires the login address to be an active user of the
    specific account — the company domain being right is not enough."""
    _owners(monkeypatch, [{"id": "1", "email": "someone.else@example.com", "archived": False}])
    assert hs.find_owner_by_email("lee@buildprorecruiters.com") is None


def test_an_archived_user_is_found_but_flagged(monkeypatch):
    _owners(monkeypatch, [{"id": "1", "email": "lee@buildprorecruiters.com", "archived": True}])
    owner = hs.find_owner_by_email("lee@buildprorecruiters.com")
    assert owner is not None and owner["archived"] is True


def test_owner_lookup_degrades_when_hubspot_is_unavailable(monkeypatch):
    monkeypatch.setattr(hs, "is_configured", lambda: True)
    monkeypatch.setattr(hs, "_request",
                        lambda m, p, **k: {"ok": False, "state": "UNAVAILABLE", "detail": "500"})
    assert hs.get_owners()["ok"] is False
    assert hs.find_owner_by_email("anyone@x.com") is None


# ══ CRM MUST NOT BE UNQUESTIONED AUTHORITY ═══════════════════════════════

def test_an_unconfigured_crm_yields_unknown_not_not_a_client(monkeypatch):
    monkeypatch.setattr(hs, "is_configured", lambda: False)
    result = intent.crm_relationship("someone@acme.com")
    assert result["party"] == intent.UNKNOWN
    assert result["trusted"] is False


def test_an_unverified_portal_makes_crm_absence_meaningless(monkeypatch):
    """A token on a sandbox returns zero matches for every real client.
    Treating that silence as 'not a client' would mislabel the whole book
    of business while every health check stayed green."""
    _portal(monkeypatch, "9999999")
    from core.headless import config
    monkeypatch.setattr(config, "HUBSPOT_EXPECTED_PORTAL_ID", "2222222")

    result = intent.crm_relationship("realclient@acme.com")
    assert result["party"] == intent.UNKNOWN
    assert result["trusted"] is False
    assert "proves nothing" in result["detail"]


def test_absence_is_only_evidence_in_a_confirmed_portal(monkeypatch):
    _portal(monkeypatch, "2222222")
    from core.headless import config
    monkeypatch.setattr(config, "HUBSPOT_EXPECTED_PORTAL_ID", "2222222")
    monkeypatch.setattr(hs, "search_contacts",
                        lambda *a, **k: {"ok": True, "results": []})

    result = intent.crm_relationship("new@prospect.com")
    assert result["party"] == intent.UNKNOWN
    assert result["trusted"] is True, "a confirmed-portal miss IS real evidence"
    assert result["crm_state"] == "NO_MATCH_IN_VERIFIED_PORTAL"


def test_a_matched_contact_in_a_confirmed_portal_is_an_existing_client(monkeypatch):
    _portal(monkeypatch, "2222222")
    from core.headless import config
    monkeypatch.setattr(config, "HUBSPOT_EXPECTED_PORTAL_ID", "2222222")
    monkeypatch.setattr(hs, "search_contacts",
                        lambda *a, **k: {"ok": True, "results": [{"id": "555"}]})

    result = intent.crm_relationship("known@client.com")
    assert result["party"] == intent.EXISTING_CLIENT
    assert result["trusted"] is True and result["contact_id"] == "555"


def test_a_crm_lookup_failure_never_downgrades_to_not_a_client(monkeypatch):
    _portal(monkeypatch, "2222222")
    from core.headless import config
    monkeypatch.setattr(config, "HUBSPOT_EXPECTED_PORTAL_ID", "2222222")
    monkeypatch.setattr(hs, "search_contacts",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert intent.crm_relationship("x@y.com")["party"] == intent.UNKNOWN


# ══ SECRETS ══════════════════════════════════════════════════════════════

def test_the_diagnostic_never_prints_a_credential(monkeypatch, capsys):
    monkeypatch.setenv("HUBSPOT_TOKEN", "pat-na1-supersecrettokenvalue999")
    _portal(monkeypatch, "4444444")
    _owners_rows = [{"id": "1", "email": "lee@buildprorecruiters.com", "archived": False}]
    monkeypatch.setattr(hs, "get_owners",
                        lambda **k: {"ok": True, "state": "OK", "owners": _owners_rows, "count": 1})

    from actions import hubspot_diagnostic
    hubspot_diagnostic.print_report("lee@buildprorecruiters.com")
    out = capsys.readouterr().out
    assert "supersecrettokenvalue" not in out
    assert "4444444" in out, "the portal id is an identifier and should be shown"


def test_portal_identity_returns_no_credential(monkeypatch):
    monkeypatch.setenv("HUBSPOT_TOKEN", "pat-na1-anothersecretvalue123")
    _portal(monkeypatch, "5555555")
    assert "anothersecretvalue" not in str(hs.get_portal_identity())
