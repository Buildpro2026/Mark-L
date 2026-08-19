import json

from actions import hubspot_integration as hs
from core.headless import config as _hc


def _isolate(monkeypatch, tmp_path, token=""):
    cfg_path = tmp_path / "api_keys.json"
    cfg_path.write_text(json.dumps({"hubspot_token": token}), encoding="utf-8")
    monkeypatch.setattr(hs, "CONFIG_PATH", cfg_path)
    # get_token() checks core.headless.config.HUBSPOT_TOKEN (the real env
    # var) FIRST, before ever reading CONFIG_PATH — on a dev machine with
    # a real .env this silently ignored the token=... arg above.
    monkeypatch.setattr(_hc, "HUBSPOT_TOKEN", token or None)


class _Resp:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self):
        if self._payload == {} and self.text and not self.text.strip().startswith("{"):
            raise ValueError("not json")
        return self._payload


def _fake_request(payload=None, status_code=200):
    def _f(method, url, headers=None, timeout=None, **kwargs):
        return _Resp(status_code, payload)
    return _f


# ── configuration / auth ────────────────────────────────────────────────

def test_is_configured_false_without_token(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="")
    assert hs.is_configured() is False


def test_is_configured_true_with_token(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-na2-secret")
    assert hs.is_configured() is True


def test_verify_hubspot_not_configured_short_circuits_without_network(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="")
    r = hs.verify_hubspot()
    assert r == {"configured": False, "verified": False, "status": "NOT_CONFIGURED"}


def test_verify_hubspot_success(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-na2-secret")
    calls = []

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        calls.append((method, url, headers))
        return _Resp(200, {"portalId": 123, "accountType": "STANDARD"})

    monkeypatch.setattr(hs.requests, "request", fake_request)
    r = hs.verify_hubspot()
    assert r["configured"] is True
    assert r["verified"] is True
    assert r["status"] == "VERIFIED"
    assert r["account"]["portalId"] == 123
    method, url, headers = calls[0]
    assert method == "GET"
    assert url.endswith("/account-info/v3/details")
    assert headers["Authorization"] == "Bearer pat-na2-secret"


def test_verify_hubspot_reports_401(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="bad-token")
    monkeypatch.setattr(hs.requests, "request", _fake_request({"message": "Authentication credentials not found"}, 401))
    r = hs.verify_hubspot()
    assert r["verified"] is False
    assert "401" in r["status"]


def test_request_wrapper_captures_network_exception(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-na2-secret")

    def raise_exc(*a, **k):
        raise ConnectionError("no network")

    monkeypatch.setattr(hs.requests, "request", raise_exc)
    r = hs.get_contacts()
    assert r["ok"] is False
    assert r["state"] == "ERROR"
    assert r["results"] == []


# ── contacts ─────────────────────────────────────────────────────────────

def test_get_contacts_not_configured(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="")
    r = hs.get_contacts()
    assert r["ok"] is False
    assert r["state"] == "NOT_CONFIGURED"
    assert r["results"] == []


def test_get_contacts_success(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-na2-secret")
    payload = {"results": [{"id": "1", "properties": {"email": "a@b.com"}}], "total": 1}
    monkeypatch.setattr(hs.requests, "request", _fake_request(payload, 200))
    r = hs.get_contacts(limit=5)
    assert r["ok"] is True
    assert len(r["results"]) == 1
    assert r["results"][0]["id"] == "1"


def test_search_contacts_sends_contains_token_filter(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-na2-secret")
    captured = {}

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return _Resp(200, {"results": [], "total": 0})

    monkeypatch.setattr(hs.requests, "request", fake_request)
    hs.search_contacts("jane@example.com")
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/crm/v3/objects/contacts/search")
    body = captured["json"]
    f = body["filterGroups"][0]["filters"][0]
    assert f["propertyName"] == "email"
    assert f["operator"] == "CONTAINS_TOKEN"
    assert f["value"] == "jane@example.com"


def test_create_contact_wraps_properties(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-na2-secret")
    captured = {}

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        captured["method"] = method
        captured["json"] = kwargs.get("json")
        return _Resp(201, {"id": "42", "properties": {"email": "new@x.com"}})

    monkeypatch.setattr(hs.requests, "request", fake_request)
    r = hs.create_contact({"email": "new@x.com", "firstname": "New"}, approved=True)
    assert r["ok"] is True
    assert r["record"]["id"] == "42"
    assert captured["method"] == "POST"
    assert captured["json"] == {"properties": {"email": "new@x.com", "firstname": "New"}}


def test_update_contact_uses_patch(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-na2-secret")
    captured = {}

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        captured["method"] = method
        captured["url"] = url
        return _Resp(200, {"id": "42", "properties": {"firstname": "Updated"}})

    monkeypatch.setattr(hs.requests, "request", fake_request)
    r = hs.update_contact("42", {"firstname": "Updated"}, approved=True)
    assert r["ok"] is True
    assert captured["method"] == "PATCH"
    assert captured["url"].endswith("/crm/v3/objects/contacts/42")


def test_get_contact_not_found_reports_error_not_fabricated_record(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-na2-secret")
    monkeypatch.setattr(hs.requests, "request", _fake_request({"message": "resource not found"}, 404))
    r = hs.get_contact("does-not-exist")
    assert r["ok"] is False
    assert r["state"] == "ERROR"
    assert "record" not in r


# ── companies ────────────────────────────────────────────────────────────

def test_get_companies_success(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-na2-secret")
    payload = {"results": [{"id": "9", "properties": {"name": "Acme"}}], "total": 1}
    monkeypatch.setattr(hs.requests, "request", _fake_request(payload, 200))
    r = hs.get_companies()
    assert r["ok"] is True
    assert r["results"][0]["properties"]["name"] == "Acme"


def test_create_company_wraps_properties(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-na2-secret")
    captured = {}

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        captured["json"] = kwargs.get("json")
        return _Resp(201, {"id": "7", "properties": {"name": "NewCo"}})

    monkeypatch.setattr(hs.requests, "request", fake_request)
    r = hs.create_company({"name": "NewCo"}, approved=True)
    assert r["ok"] is True
    assert captured["json"] == {"properties": {"name": "NewCo"}}


def test_update_company_uses_patch(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-na2-secret")
    captured = {}

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        captured["method"] = method
        return _Resp(200, {"id": "7", "properties": {"name": "Renamed"}})

    monkeypatch.setattr(hs.requests, "request", fake_request)
    hs.update_company("7", {"name": "Renamed"}, approved=True)
    assert captured["method"] == "PATCH"


# ── write safeguards: nothing writes without approved=True ──────────────

def test_create_contact_refuses_without_approval_and_never_touches_the_api(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-na2-secret")
    calls = []
    monkeypatch.setattr(hs.requests, "request", lambda *a, **k: calls.append(1))

    r = hs.create_contact({"email": "new@x.com"})
    assert r["ok"] is False
    assert r["state"] == "NOT_APPROVED"
    assert calls == []


def test_update_contact_refuses_without_approval(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-na2-secret")
    calls = []
    monkeypatch.setattr(hs.requests, "request", lambda *a, **k: calls.append(1))

    r = hs.update_contact("42", {"firstname": "X"})
    assert r["ok"] is False
    assert r["state"] == "NOT_APPROVED"
    assert calls == []


def test_create_company_refuses_without_approval(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-na2-secret")
    calls = []
    monkeypatch.setattr(hs.requests, "request", lambda *a, **k: calls.append(1))

    r = hs.create_company({"name": "NewCo"})
    assert r["ok"] is False
    assert r["state"] == "NOT_APPROVED"
    assert calls == []


def test_update_company_refuses_without_approval(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-na2-secret")
    calls = []
    monkeypatch.setattr(hs.requests, "request", lambda *a, **k: calls.append(1))

    r = hs.update_company("7", {"name": "X"})
    assert r["ok"] is False
    assert r["state"] == "NOT_APPROVED"
    assert calls == []


# ── upsert_contact — idempotent create-or-update, deduplicated by email ──

def test_upsert_contact_refuses_without_approval(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-na2-secret")
    calls = []
    monkeypatch.setattr(hs.requests, "request", lambda *a, **k: calls.append(1))

    r = hs.upsert_contact("jane@example.com", {"firstname": "Jane"})
    assert r["ok"] is False
    assert r["state"] == "NOT_APPROVED"
    assert calls == []


def test_upsert_contact_requires_email(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-na2-secret")
    r = hs.upsert_contact("", {"firstname": "Jane"}, approved=True)
    assert r["ok"] is False


def test_upsert_contact_creates_when_no_existing_match(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-na2-secret")
    calls = []

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        calls.append(method)
        if method == "POST" and url.endswith("/search"):
            return _Resp(200, {"results": [], "total": 0})   # no existing match
        if method == "POST":
            return _Resp(201, {"id": "new-1", "properties": kwargs["json"]["properties"]})
        raise AssertionError(f"unexpected call: {method} {url}")

    monkeypatch.setattr(hs.requests, "request", fake_request)
    r = hs.upsert_contact("new@example.com", {"firstname": "New"}, approved=True)

    assert r["ok"] is True
    assert r["action"] == "created"
    assert r["record"]["properties"]["email"] == "new@example.com"
    assert calls == ["POST", "POST"]   # one search, one create — never a PATCH


def test_upsert_contact_updates_the_existing_match_instead_of_creating_a_duplicate(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-na2-secret")
    calls = []

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        calls.append(method)
        if method == "POST" and url.endswith("/search"):
            return _Resp(200, {"results": [{"id": "existing-1", "properties": {"email": "jane@example.com"}}], "total": 1})
        if method == "PATCH":
            return _Resp(200, {"id": "existing-1", "properties": {"email": "jane@example.com", "firstname": "Jane"}})
        raise AssertionError(f"unexpected call: {method} {url}")

    monkeypatch.setattr(hs.requests, "request", fake_request)
    r = hs.upsert_contact("jane@example.com", {"firstname": "Jane"}, approved=True)

    assert r["ok"] is True
    assert r["action"] == "updated"
    assert r["record"]["id"] == "existing-1"
    assert calls == ["POST", "PATCH"]   # search + patch — never a second create


def test_upsert_contact_called_twice_never_creates_two_records(monkeypatch, tmp_path):
    # The core idempotency guarantee: the second call must see the first
    # call's result via search and update it, not create a duplicate.
    _isolate(monkeypatch, tmp_path, token="pat-na2-secret")
    created_records = []

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        if method == "POST" and url.endswith("/search"):
            if created_records:
                return _Resp(200, {"results": [{"id": "id-1", "properties": created_records[0]}], "total": 1})
            return _Resp(200, {"results": [], "total": 0})
        if method == "POST":
            props = kwargs["json"]["properties"]
            created_records.append(props)
            return _Resp(201, {"id": "id-1", "properties": props})
        if method == "PATCH":
            return _Resp(200, {"id": "id-1", "properties": kwargs["json"]["properties"]})
        raise AssertionError(f"unexpected call: {method} {url}")

    monkeypatch.setattr(hs.requests, "request", fake_request)
    first = hs.upsert_contact("dupe@example.com", {"firstname": "First"}, approved=True)
    second = hs.upsert_contact("dupe@example.com", {"firstname": "Second"}, approved=True)

    assert first["action"] == "created"
    assert second["action"] == "updated"
    assert len(created_records) == 1   # exactly one contact ever created


def test_upsert_contact_propagates_search_failure_without_writing(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-na2-secret")
    calls = []

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        calls.append(method)
        return _Resp(500, {"message": "internal error"})

    monkeypatch.setattr(hs.requests, "request", fake_request)
    r = hs.upsert_contact("jane@example.com", {"firstname": "Jane"}, approved=True)
    assert r["ok"] is False
    assert calls == ["POST"]   # only the search — never attempted a write after a failed search


# ── upsert_company — same idempotent pattern, deduplicated by name ──────

def test_upsert_company_creates_when_no_existing_match(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-na2-secret")

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        if method == "POST" and url.endswith("/search"):
            return _Resp(200, {"results": [], "total": 0})
        if method == "POST":
            return _Resp(201, {"id": "co-1", "properties": kwargs["json"]["properties"]})
        raise AssertionError

    monkeypatch.setattr(hs.requests, "request", fake_request)
    r = hs.upsert_company("Acme Construction", {"industry": "construction"}, approved=True)
    assert r["ok"] is True
    assert r["action"] == "created"
    assert r["record"]["properties"]["name"] == "Acme Construction"


def test_upsert_company_updates_existing_match(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-na2-secret")

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        if method == "POST" and url.endswith("/search"):
            return _Resp(200, {"results": [{"id": "co-1", "properties": {"name": "Acme Construction"}}], "total": 1})
        if method == "PATCH":
            return _Resp(200, {"id": "co-1", "properties": {"name": "Acme Construction", "industry": "construction"}})
        raise AssertionError

    monkeypatch.setattr(hs.requests, "request", fake_request)
    r = hs.upsert_company("Acme Construction", {"industry": "construction"}, approved=True)
    assert r["ok"] is True
    assert r["action"] == "updated"


def test_upsert_company_refuses_without_approval(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-na2-secret")
    calls = []
    monkeypatch.setattr(hs.requests, "request", lambda *a, **k: calls.append(1))

    r = hs.upsert_company("Acme", {"industry": "construction"})
    assert r["ok"] is False
    assert r["state"] == "NOT_APPROVED"
    assert calls == []


def test_search_companies_not_configured(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="")
    r = hs.search_companies("Acme")
    assert r["ok"] is False
    assert r["state"] == "NOT_CONFIGURED"
