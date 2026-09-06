"""actions/buildpro_client_intake.py — the client-inquiry counterpart to
candidate_intake.py (2026-08-30, Lee's spec). Mocks each real integration
it calls (gmail_integration, hubspot_integration, buildpro_data,
business_intelligence) so these tests cover the ORCHESTRATION rather than
re-testing those modules' own already-covered internals.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from actions import buildpro_client_intake as intake


def _message(sender="John Client <john@example.com>", subject="Need help staffing a project",
             snippet="Please call me at 214-555-0100 to discuss.", message_id="m1"):
    return {"id": message_id, "sender": sender, "subject": subject, "snippet": snippet}


def _stub_common(monkeypatch, hubspot_configured=False):
    monkeypatch.setattr(intake.buildpro_data, "upsert_client", lambda *a, **k: (1, "created"))
    monkeypatch.setattr(intake.buildpro_data, "update_client", lambda *a, **k: True)
    monkeypatch.setattr(intake.hubspot_integration, "is_configured", lambda: hubspot_configured)
    monkeypatch.setattr(intake.biz_intel, "add_entry", lambda **k: None)
    monkeypatch.setattr(intake.gmail_integration, "create_draft", lambda *a, **k: {"ok": True, "draft_id": "d1"})


# ── sender parsing (same contract as candidate_intake) ──────────────────

def test_parse_sender_name_and_angle_bracket_email():
    assert intake._parse_sender("John Client <john@example.com>") == ("John Client", "john@example.com")


def test_parse_sender_empty_string():
    assert intake._parse_sender("") == ("", "")


# ── phone extraction / time zone inference ───────────────────────────────

def test_extracts_a_us_phone_number_from_free_text():
    assert intake._extract_phone("call me at 214-555-0100 please") == "214-555-0100"


def test_no_phone_number_returns_none():
    assert intake._extract_phone("no number here at all") is None


def test_known_area_code_infers_a_real_timezone():
    tz, inferred = intake._infer_timezone("214-555-0100")  # Dallas -> Central
    assert tz == "America/Chicago"
    assert inferred is True


def test_unknown_or_missing_phone_falls_back_to_home_timezone_honestly():
    tz, inferred = intake._infer_timezone(None)
    assert tz == intake._HOME_TZ
    assert inferred is False


def test_deadline_is_5pm_today_when_still_before_close(monkeypatch):
    fixed_now = datetime(2026, 8, 31, 10, 0, tzinfo=ZoneInfo("America/Chicago"))  # a Monday, 10am

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(intake, "datetime", _FixedDatetime)
    deadline = intake._contact_by_deadline("America/Chicago")
    assert deadline.hour == 17
    assert deadline.date() == fixed_now.date()


def test_deadline_rolls_to_next_business_day_after_close(monkeypatch):
    fixed_now = datetime(2026, 8, 31, 18, 0, tzinfo=ZoneInfo("America/Chicago"))  # Monday, 6pm — past close

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(intake, "datetime", _FixedDatetime)
    deadline = intake._contact_by_deadline("America/Chicago")
    assert deadline.date() == (fixed_now.date() + timedelta(days=1))
    assert deadline.weekday() < 5  # never lands on a weekend


# ── process_client_email orchestration ───────────────────────────────────

def test_refuses_when_no_email_can_be_determined(monkeypatch):
    result = intake.process_client_email(_message(sender=""))
    assert result["ok"] is False


def test_creates_local_client_record_with_real_fields(monkeypatch):
    calls = {}
    monkeypatch.setattr(intake.buildpro_data, "upsert_client",
                         lambda name, **kw: (calls.setdefault("args", (name, kw)), (5, "created"))[1])
    monkeypatch.setattr(intake.buildpro_data, "update_client", lambda *a, **k: True)
    monkeypatch.setattr(intake.hubspot_integration, "is_configured", lambda: False)
    monkeypatch.setattr(intake.biz_intel, "add_entry", lambda **k: None)
    monkeypatch.setattr(intake.gmail_integration, "create_draft", lambda *a, **k: {"ok": True, "draft_id": "d1"})

    result = intake.process_client_email(_message())

    assert result["ok"] is True
    assert result["client_id"] == 5
    name, kwargs = calls["args"]
    assert name == "John Client"
    assert kwargs["email"] == "john@example.com"
    assert kwargs["source"] == "gmail_intake"
    assert kwargs["phone"] == "214-555-0100"


def test_hubspot_not_configured_still_completes_the_rest_of_the_chain(monkeypatch):
    _stub_common(monkeypatch, hubspot_configured=False)
    result = intake.process_client_email(_message())
    assert result["ok"] is True
    assert result["hubspot_ok"] is False
    assert result["hubspot_company_id"] is None
    assert result["welcome_email_drafted"] is True


def test_hubspot_upsert_links_company_id_back_to_local_record(monkeypatch):
    linked = {}
    _stub_common(monkeypatch, hubspot_configured=True)
    monkeypatch.setattr(intake.buildpro_data, "update_client",
                         lambda cid, **kw: linked.setdefault("call", (cid, kw)))
    monkeypatch.setattr(intake.hubspot_integration, "upsert_company",
                         lambda name, props, approved: {"ok": True, "record": {"id": "hs-co-1"}, "action": "created"})

    result = intake.process_client_email(_message())

    assert result["hubspot_ok"] is True
    assert result["hubspot_company_id"] == "hs-co-1"
    assert linked["call"] == (1, {"hubspot_company_id": "hs-co-1"})


def test_email_never_mentions_representation_or_contracts(monkeypatch):
    # Lee's explicit instruction (2026-08-30): contracts are his own call
    # after he's personally made contact — the auto-drafted client email
    # must never presuppose representation, unlike the old candidate flow.
    _stub_common(monkeypatch)
    captured = {}
    monkeypatch.setattr(intake.gmail_integration, "create_draft",
                         lambda to, subject, body: (captured.setdefault("body", body), {"ok": True, "draft_id": "d1"})[1])

    intake.process_client_email(_message())

    lowered = captured["body"].lower()
    assert "agreement" not in lowered
    assert "contract" not in lowered
    assert "represent" not in lowered


def test_welcome_email_defaults_to_draft_not_send(monkeypatch):
    _stub_common(monkeypatch)
    send_calls = []
    monkeypatch.setattr(intake.gmail_integration, "send_email", lambda *a, **k: send_calls.append(1))

    result = intake.process_client_email(_message())

    assert send_calls == []
    assert result["welcome_email_drafted"] is True
    assert result["welcome_email_sent"] is False


def test_auto_send_true_actually_sends(monkeypatch):
    _stub_common(monkeypatch)
    send_calls = []
    monkeypatch.setattr(intake.gmail_integration, "send_email",
                         lambda to, subject, body, approved: (send_calls.append(approved), {"ok": True, "message_id": "sent-1"})[1])

    result = intake.process_client_email(_message(), auto_send=True)

    assert send_calls == [True]
    assert result["welcome_email_sent"] is True
    assert result["welcome_email_drafted"] is False


def test_contact_by_deadline_and_timezone_are_in_the_result(monkeypatch):
    _stub_common(monkeypatch)
    result = intake.process_client_email(_message())
    assert result["timezone"] == "America/Chicago"
    assert result["timezone_inferred"] is True
    assert result["contact_by"]
    assert result["contact_by_display"]


def test_no_phone_number_falls_back_honestly_without_inventing_a_timezone(monkeypatch):
    _stub_common(monkeypatch)
    result = intake.process_client_email(_message(snippet="just curious about your services"))
    assert result["phone_found"] is None
    assert result["timezone"] == intake._HOME_TZ
    assert result["timezone_inferred"] is False
