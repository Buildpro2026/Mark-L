import json

from actions import twilio_integration as tw


def _isolate(monkeypatch, tmp_path, cfg=None):
    cfg_path = tmp_path / "api_keys.json"
    cfg_path.write_text(json.dumps({"twilio": cfg or {}}), encoding="utf-8")
    monkeypatch.setattr(tw, "API_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(tw, "DB_PATH", tmp_path / "test_comms.db")


# ── status states ────────────────────────────────────────────────────────

def test_status_is_not_configured_with_no_credentials(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, {})
    s = tw.get_status()
    assert s["state"] == "NOT_CONFIGURED"


def test_status_is_not_configured_with_partial_credentials(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, {"account_sid": "AC123", "auth_token": "", "from_number": "+15551234567"})
    assert tw.get_status()["state"] == "NOT_CONFIGURED"


def test_status_is_configured_with_full_credentials(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, {"account_sid": "AC123", "auth_token": "secret", "from_number": "+15551234567"})
    s = tw.get_status()
    assert s["state"] == "CONFIGURED"
    assert s["from_number"] == "+15551234567"


def test_check_connection_reports_not_configured_without_network_call(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, {})
    # No requests monkeypatch provided — if this tried a real network call it
    # would hang/error in a sandboxed test env; NOT_CONFIGURED must short-circuit.
    assert tw.check_connection()["state"] == "NOT_CONFIGURED"


def test_check_connection_connected_on_http_200(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, {"account_sid": "AC123", "auth_token": "secret", "from_number": "+15551234567"})
    import requests

    class _Resp:
        status_code = 200
        def json(self): return {"status": "active"}
        text = ""

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    s = tw.check_connection()
    assert s["state"] == "CONNECTED"
    assert s["account_status"] == "active"


def test_check_connection_error_on_http_failure(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, {"account_sid": "AC123", "auth_token": "bad", "from_number": "+15551234567"})
    import requests

    class _Resp:
        status_code = 401
        text = "Authenticate"
        def json(self): return {}

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    assert tw.check_connection()["state"] == "ERROR"


def test_check_connection_error_on_network_exception(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, {"account_sid": "AC123", "auth_token": "secret", "from_number": "+15551234567"})
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("no network")))
    assert tw.check_connection()["state"] == "ERROR"


# ── send_sms / place_call: never fake success without configuration ───────

def test_send_sms_refuses_when_not_configured(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, {})
    r = tw.send_sms("+15559876543", "hello")
    assert r["ok"] is False
    assert r["state"] == "NOT_CONFIGURED"
    # still logged, so the attempt is auditable
    history = tw.get_history()
    assert history[0]["status"] == "not_configured"


def test_place_call_refuses_when_not_configured(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, {})
    r = tw.place_call("+15559876543", "hi")
    assert r["ok"] is False
    assert r["state"] == "NOT_CONFIGURED"


def test_send_sms_success_logs_and_returns_sid(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, {"account_sid": "AC123", "auth_token": "secret", "from_number": "+15551234567"})
    import requests

    class _Resp:
        status_code = 201
        def json(self): return {"sid": "SM123", "status": "queued"}

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
    r = tw.send_sms("+15559876543", "Running 10 min late")
    assert r["ok"] is True
    assert r["sid"] == "SM123"
    history = tw.get_history(kind="sms")
    assert history[0]["sid"] == "SM123"
    assert history[0]["body"] == "Running 10 min late"


def test_send_sms_failure_from_twilio_reports_error_and_logs(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, {"account_sid": "AC123", "auth_token": "secret", "from_number": "+15551234567"})
    import requests

    class _Resp:
        status_code = 400
        def json(self): return {"message": "Invalid 'To' Phone Number"}

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
    r = tw.send_sms("not-a-number", "hi")
    assert r["ok"] is False
    assert r["state"] == "ERROR"
    assert "Invalid" in r["detail"]
    history = tw.get_history(kind="sms")
    assert history[0]["status"] == "failed"


def test_place_call_success_logs_and_returns_sid(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, {"account_sid": "AC123", "auth_token": "secret", "from_number": "+15551234567"})
    import requests

    class _Resp:
        status_code = 201
        def json(self): return {"sid": "CA123", "status": "queued"}

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
    r = tw.place_call("+15559876543", "This is Jarvis calling.")
    assert r["ok"] is True
    assert r["sid"] == "CA123"


def test_send_sms_network_exception_is_captured_not_raised(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, {"account_sid": "AC123", "auth_token": "secret", "from_number": "+15551234567"})
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("timeout")))
    r = tw.send_sms("+15559876543", "hi")
    assert r["ok"] is False
    assert r["state"] == "ERROR"


# ── history / missed calls ──────────────────────────────────────────────

def test_get_history_orders_newest_first_and_respects_limit(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, {})
    tw._log(direction="outbound", kind="sms", body="first")
    tw._log(direction="outbound", kind="sms", body="second")
    history = tw.get_history(limit=1)
    assert len(history) == 1
    assert history[0]["body"] == "second"


def test_get_missed_calls_filters_to_missed_inbound_only(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, {})
    tw._log(direction="inbound", kind="call", from_number="+1111", status="no-answer")
    tw._log(direction="inbound", kind="call", from_number="+2222", status="completed")
    tw._log(direction="outbound", kind="call", to_number="+3333", status="no-answer")
    missed = tw.get_missed_calls()
    assert len(missed) == 1
    assert missed[0]["from_number"] == "+1111"


def test_update_by_sid_updates_the_matching_row(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, {})
    row_id = tw._log(direction="outbound", kind="sms", sid="SM999", status="queued")
    tw.update_by_sid("SM999", status="delivered")
    history = tw.get_history()
    assert history[0]["status"] == "delivered"


# ── contacts ──────────────────────────────────────────────────────────────

def test_lookup_contact_accepts_a_literal_phone_number():
    r = tw.lookup_contact("+15559876543")
    assert r["resolved"] is True
    assert r["number"] == "+15559876543"


def test_lookup_contact_honestly_reports_no_database_for_a_name():
    r = tw.lookup_contact("John")
    assert r["resolved"] is False
    assert "no contacts database" in r["detail"].lower()


# ── inbound webhook helpers ──────────────────────────────────────────────

def test_record_inbound_sms_and_call(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, {})
    tw.record_inbound_sms("+15551111111", "+15550000000", "hey", "SM1")
    tw.record_inbound_call("+15552222222", "+15550000000", "CA1")
    history = tw.get_history()
    kinds = {h["kind"] for h in history}
    assert kinds == {"sms", "call"}
    assert all(h["direction"] == "inbound" for h in history)


def test_voicemail_twiml_contains_record_and_transcribe():
    xml = tw.voicemail_twiml()
    assert "<Record" in xml
    assert 'transcribe="true"' in xml
