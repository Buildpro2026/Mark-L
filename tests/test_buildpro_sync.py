import pytest

from actions import buildpro_sync as sync
from actions import buildpro_data as bd
from actions import hubspot_integration as hubspot
from core.headless import config as _hc


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(bd, "DB_PATH", tmp_path / "test_sync.db")
    cfg_path = tmp_path / "api_keys.json"
    cfg_path.write_text('{"hubspot_token": "test-token"}', encoding="utf-8")
    monkeypatch.setattr(hubspot, "CONFIG_PATH", cfg_path)
    # get_token() checks core.headless.config.HUBSPOT_TOKEN (the real env
    # var) FIRST, before ever reading CONFIG_PATH — on a dev machine with
    # a real .env this silently ignored every CONFIG_PATH override below,
    # including the two "not configured" tests that write an empty token.
    monkeypatch.setattr(_hc, "HUBSPOT_TOKEN", None)


def _contact(cid, first="Jane", last="Doe", email="jane@example.com", phone="+15551234567"):
    return {"id": cid, "properties": {"firstname": first, "lastname": last, "email": email, "phone": phone}}


def _company(cid, name="Acme Co", industry="Construction", phone="+15559998888"):
    return {"id": cid, "properties": {"name": name, "industry": industry, "phone": phone}}


def _page(records, next_after=None):
    paging = {"next": {"after": next_after}} if next_after else {}
    return {"ok": True, "results": records, "paging": paging}


# ── configuration ────────────────────────────────────────────────────────

def test_sync_contacts_not_configured_without_token(monkeypatch, tmp_path):
    cfg_path = tmp_path / "empty.json"
    cfg_path.write_text('{"hubspot_token": ""}', encoding="utf-8")
    monkeypatch.setattr(hubspot, "CONFIG_PATH", cfg_path)

    result = sync.sync_contacts()
    assert result == {"ok": False, "state": "NOT_CONFIGURED", "created": 0, "updated": 0, "pulled": 0, "errors": []}
    assert bd.list_candidates() == []


def test_sync_companies_not_configured_without_token(monkeypatch, tmp_path):
    cfg_path = tmp_path / "empty.json"
    cfg_path.write_text('{"hubspot_token": ""}', encoding="utf-8")
    monkeypatch.setattr(hubspot, "CONFIG_PATH", cfg_path)

    result = sync.sync_companies()
    assert result["state"] == "NOT_CONFIGURED"
    assert bd.list_clients() == []


# ── contacts -> candidates ───────────────────────────────────────────────

def test_sync_contacts_creates_new_candidates(monkeypatch):
    monkeypatch.setattr(hubspot, "get_contacts", lambda limit, after=None: _page([_contact("hs-1"), _contact("hs-2", first="John", last="Smith")]))

    result = sync.sync_contacts()
    assert result["state"] == "SYNCED"
    assert result["created"] == 2
    assert result["updated"] == 0
    candidates = bd.list_candidates()
    assert len(candidates) == 2
    names = {c["name"] for c in candidates}
    assert names == {"Jane Doe", "John Smith"}
    assert all(c["source"] == "hubspot" for c in candidates)


def test_sync_contacts_updates_existing_candidate_not_duplicate(monkeypatch):
    bd.add_candidate("Old Name", hubspot_contact_id="hs-1", source="hubspot")
    monkeypatch.setattr(hubspot, "get_contacts", lambda limit, after=None: _page([_contact("hs-1", first="New", last="Name")]))

    result = sync.sync_contacts()
    assert result["created"] == 0
    assert result["updated"] == 1
    candidates = bd.list_candidates()
    assert len(candidates) == 1
    assert candidates[0]["name"] == "New Name"


def test_sync_contacts_sets_last_synced_ts(monkeypatch):
    monkeypatch.setattr(hubspot, "get_contacts", lambda limit, after=None: _page([_contact("hs-1")]))
    sync.sync_contacts()
    candidate = bd.get_candidate_by_hubspot_id("hs-1")
    assert candidate["last_synced_ts"] is not None


def test_sync_contacts_paginates_across_multiple_pages(monkeypatch):
    calls = []

    def fake_get_contacts(limit, after=None):
        calls.append(after)
        if after is None:
            return _page([_contact("hs-1")], next_after="cursor-2")
        if after == "cursor-2":
            return _page([_contact("hs-2", first="Page", last="Two")])
        return _page([])

    monkeypatch.setattr(hubspot, "get_contacts", fake_get_contacts)
    result = sync.sync_contacts()
    assert result["created"] == 2
    assert calls == [None, "cursor-2"]


def test_sync_contacts_stops_pagination_on_fetch_error(monkeypatch):
    def fake_get_contacts(limit, after=None):
        if after is None:
            return _page([_contact("hs-1")], next_after="cursor-2")
        return {"ok": False, "detail": "rate limited", "results": []}

    monkeypatch.setattr(hubspot, "get_contacts", fake_get_contacts)
    result = sync.sync_contacts()
    assert result["created"] == 1   # first page's record still got synced
    assert result["state"] == "PARTIAL"
    assert "rate limited" in result["errors"][0]


def test_sync_contacts_one_bad_record_does_not_abort_the_run(monkeypatch):
    monkeypatch.setattr(hubspot, "get_contacts", lambda limit, after=None: _page([_contact("hs-1"), _contact("hs-2")]))

    original_add = bd.add_candidate

    def flaky_add_candidate(*args, **kwargs):
        if kwargs.get("hubspot_contact_id") == "hs-1":
            raise RuntimeError("simulated local DB failure")
        return original_add(*args, **kwargs)

    monkeypatch.setattr(bd, "add_candidate", flaky_add_candidate)
    result = sync.sync_contacts()
    assert result["created"] == 1
    assert result["state"] == "PARTIAL"
    assert len(result["errors"]) == 1
    assert "hs-1" in result["errors"][0]
    # the good record still made it in — local data isn't corrupted by the bad one
    assert len(bd.list_candidates()) == 1


def test_sync_contacts_records_sync_run(monkeypatch):
    monkeypatch.setattr(hubspot, "get_contacts", lambda limit, after=None: _page([_contact("hs-1")]))
    sync.sync_contacts(limit=1)
    last = bd.get_last_sync("candidates")
    assert last is not None
    assert last["source"] == "hubspot"
    assert last["created_count"] == 1


# ── companies -> clients ─────────────────────────────────────────────────

def test_sync_companies_creates_new_clients(monkeypatch):
    monkeypatch.setattr(hubspot, "get_companies", lambda limit, after=None: _page([_company("hs-co-1")]))

    result = sync.sync_companies()
    assert result["state"] == "SYNCED"
    assert result["created"] == 1
    clients = bd.list_clients()
    assert clients[0]["name"] == "Acme Co"
    assert clients[0]["industry"] == "Construction"
    assert clients[0]["source"] == "hubspot"


def test_sync_companies_updates_existing_client_not_duplicate(monkeypatch):
    bd.add_client("Old Co Name", hubspot_company_id="hs-co-1", source="hubspot")
    monkeypatch.setattr(hubspot, "get_companies", lambda limit, after=None: _page([_company("hs-co-1", name="New Co Name")]))

    result = sync.sync_companies()
    assert result["updated"] == 1
    assert result["created"] == 0
    clients = bd.list_clients()
    assert len(clients) == 1
    assert clients[0]["name"] == "New Co Name"


def test_sync_companies_falls_back_to_domain_when_name_missing(monkeypatch):
    record = {"id": "hs-co-2", "properties": {"domain": "example.com", "industry": ""}}
    monkeypatch.setattr(hubspot, "get_companies", lambda limit, after=None: _page([record]))
    sync.sync_companies()
    clients = bd.list_clients()
    assert clients[0]["name"] == "example.com"


# ── sync_all ─────────────────────────────────────────────────────────────

def test_sync_all_runs_both_contacts_and_companies(monkeypatch):
    monkeypatch.setattr(hubspot, "get_contacts", lambda limit, after=None: _page([_contact("hs-1")]))
    monkeypatch.setattr(hubspot, "get_companies", lambda limit, after=None: _page([_company("hs-co-1")]))

    result = sync.sync_all()
    assert result["contacts"]["created"] == 1
    assert result["companies"]["created"] == 1
    assert len(bd.list_candidates()) == 1
    assert len(bd.list_clients()) == 1
