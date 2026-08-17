"""actions/airtable_integration.py — generic, schema-agnostic Airtable REST
client. No live network calls: requests.request is always monkeypatched.
"""
import json

from actions import airtable_integration as at


def _isolate(monkeypatch, tmp_path, token=""):
    cfg_path = tmp_path / "api_keys.json"
    cfg_path.write_text(json.dumps({"airtable_token": token}), encoding="utf-8")
    monkeypatch.setattr(at, "CONFIG_PATH", cfg_path)


class _Resp:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self):
        return self._payload


# ── configuration ────────────────────────────────────────────────────────

def test_is_configured_false_without_token(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="")
    assert at.is_configured() is False


def test_is_configured_true_with_token(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-secret-abc")
    assert at.is_configured() is True


def test_get_status_reports_not_configured_without_token(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="")
    s = at.get_status()
    assert s["configured"] is False
    assert s["state"] == "NOT_CONFIGURED"


def test_get_status_reports_configured_with_token(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-secret-abc")
    s = at.get_status()
    assert s["configured"] is True
    assert s["state"] == "CONFIGURED"


def test_missing_config_file_is_not_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(at, "CONFIG_PATH", tmp_path / "does_not_exist.json")
    assert at.is_configured() is False


# ── list_records / get_record — read-only ────────────────────────────────

def test_list_records_short_circuits_without_network_when_not_configured(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="")
    calls = []
    monkeypatch.setattr(at.requests, "request", lambda *a, **k: calls.append(1))

    result = at.list_records("appXXX", "Leads")
    assert result["ok"] is False
    assert result["state"] == "NOT_CONFIGURED"
    assert result["records"] == []
    assert calls == []


def test_list_records_requires_base_id_and_table_name(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-secret")
    result = at.list_records("", "Leads")
    assert result["ok"] is False


def test_list_records_success_returns_real_records(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-secret")

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        assert method == "GET"
        assert "appXXX/Leads" in url
        assert headers["Authorization"] == "Bearer pat-secret"
        return _Resp(200, {"records": [{"id": "rec1", "fields": {"Name": "Jane"}}]})

    monkeypatch.setattr(at.requests, "request", fake_request)
    result = at.list_records("appXXX", "Leads")
    assert result["ok"] is True
    assert result["records"][0]["fields"]["Name"] == "Jane"


def test_list_records_forwards_view_and_filter_formula(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-secret")
    captured = {}

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        captured.update(kwargs.get("params", {}))
        return _Resp(200, {"records": []})

    monkeypatch.setattr(at.requests, "request", fake_request)
    at.list_records("appXXX", "Leads", view="Grid view", filter_by_formula="{Status}='New'")
    assert captured["view"] == "Grid view"
    assert captured["filterByFormula"] == "{Status}='New'"


def test_get_record_success(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-secret")

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        assert "appXXX/Leads/rec1" in url
        return _Resp(200, {"id": "rec1", "fields": {"Name": "Jane"}})

    monkeypatch.setattr(at.requests, "request", fake_request)
    result = at.get_record("appXXX", "Leads", "rec1")
    assert result["ok"] is True
    assert result["record"]["id"] == "rec1"


def test_api_error_is_reported_not_fabricated(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-secret")
    monkeypatch.setattr(at.requests, "request",
                         lambda *a, **k: _Resp(404, {"error": {"message": "Table not found"}}))

    result = at.list_records("appXXX", "NoSuchTable")
    assert result["ok"] is False
    assert result["state"] == "ERROR"
    assert result["detail"] == "Table not found"


def test_network_exception_is_captured_not_raised(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-secret")

    def raise_exc(*a, **k):
        raise Exception("connection refused")

    monkeypatch.setattr(at.requests, "request", raise_exc)
    result = at.list_records("appXXX", "Leads")
    assert result["ok"] is False
    assert result["state"] == "ERROR"


# ── create_record — the approval gate ────────────────────────────────────

def test_create_record_refuses_without_approval_and_never_touches_the_api(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-secret")
    calls = []
    monkeypatch.setattr(at.requests, "request", lambda *a, **k: calls.append(1))

    result = at.create_record("appXXX", "Leads", {"Name": "Jane"})
    assert result["ok"] is False
    assert result["state"] == "NOT_APPROVED"
    assert calls == []


def test_create_record_refuses_empty_fields_even_when_approved(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-secret")
    calls = []
    monkeypatch.setattr(at.requests, "request", lambda *a, **k: calls.append(1))

    result = at.create_record("appXXX", "Leads", {}, approved=True)
    assert result["ok"] is False
    assert calls == []


def test_create_record_succeeds_when_approved_and_sends_fields_unchanged(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-secret")
    captured = {}

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        assert method == "POST"
        captured["body"] = kwargs.get("json")
        return _Resp(200, {"records": [{"id": "rec1", "fields": {"Name": "Jane", "Status": "New"}}]})

    monkeypatch.setattr(at.requests, "request", fake_request)
    result = at.create_record("appXXX", "Leads", {"Name": "Jane", "Status": "New"}, approved=True)

    assert result["ok"] is True
    assert result["record"]["id"] == "rec1"
    # fields passed through exactly as given — no invented/renamed keys
    assert captured["body"] == {"records": [{"fields": {"Name": "Jane", "Status": "New"}}]}


def test_create_record_not_configured(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="")
    result = at.create_record("appXXX", "Leads", {"Name": "Jane"}, approved=True)
    assert result["ok"] is False
    assert result["state"] == "NOT_CONFIGURED"


# ── update_record — the approval gate + partial patch ────────────────────

def test_update_record_refuses_without_approval(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-secret")
    calls = []
    monkeypatch.setattr(at.requests, "request", lambda *a, **k: calls.append(1))

    result = at.update_record("appXXX", "Leads", "rec1", {"Status": "Contacted"})
    assert result["ok"] is False
    assert result["state"] == "NOT_APPROVED"
    assert calls == []


def test_update_record_only_patches_provided_fields(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-secret")
    captured = {}

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        assert method == "PATCH"
        assert "appXXX/Leads/rec1" in url
        captured["body"] = kwargs.get("json")
        return _Resp(200, {"id": "rec1", "fields": {"Status": "Contacted"}})

    monkeypatch.setattr(at.requests, "request", fake_request)
    result = at.update_record("appXXX", "Leads", "rec1", {"Status": "Contacted"}, approved=True)

    assert result["ok"] is True
    assert captured["body"] == {"fields": {"Status": "Contacted"}}


def test_update_record_api_failure_is_reported(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="pat-secret")
    monkeypatch.setattr(at.requests, "request",
                         lambda *a, **k: _Resp(422, {"error": {"message": "Unknown field name: \"Bogus\""}}))

    result = at.update_record("appXXX", "Leads", "rec1", {"Bogus": "x"}, approved=True)
    assert result["ok"] is False
    assert "Bogus" in result["detail"]
