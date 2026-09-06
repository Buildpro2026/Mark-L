from actions import calendar_integration as cal


class _Execute:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeCalendarService:
    def __init__(self, list_result=None, insert_result=None, patch_result=None):
        self._list_result = list_result or {"items": []}
        self._insert_result = insert_result
        self._patch_result = patch_result
        self.inserted_bodies = []
        self.patched_bodies = []

    def events(self):
        return self

    def list(self, calendarId, timeMin, singleEvents, orderBy, maxResults=None, timeMax=None):
        return _Execute(self._list_result)

    def insert(self, calendarId, body):
        self.inserted_bodies.append(body)
        return _Execute(self._insert_result)

    def patch(self, calendarId, eventId, body):
        self.patched_bodies.append((eventId, body))
        return _Execute(self._patch_result)


def _raw_event():
    return {
        "id": "e1", "summary": "Interview: Jane Doe",
        "start": {"dateTime": "2026-08-15T09:00:00-05:00"},
        "end": {"dateTime": "2026-08-15T10:00:00-05:00"},
        "location": "Office", "htmlLink": "https://calendar.google.com/e1",
    }


# ── list_upcoming_events ──────────────────────────────────────────────────

def test_list_upcoming_events_normalizes_fields(monkeypatch):
    fake = FakeCalendarService(list_result={"items": [_raw_event()]})
    monkeypatch.setattr(cal, "_service", lambda: fake)

    result = cal.list_upcoming_events()
    assert result["ok"] is True
    assert len(result["events"]) == 1
    event = result["events"][0]
    assert event["summary"] == "Interview: Jane Doe"
    assert event["start"] == "2026-08-15T09:00:00-05:00"
    assert event["location"] == "Office"


def test_list_upcoming_events_handles_all_day_events():
    normalized = cal._normalize_event({
        "id": "e2", "summary": "Company holiday",
        "start": {"date": "2026-08-20"}, "end": {"date": "2026-08-21"},
    })
    assert normalized["start"] == "2026-08-20"
    assert normalized["end"] == "2026-08-21"


def test_list_upcoming_events_not_authorized(monkeypatch):
    def raise_not_authorized():
        raise RuntimeError("Google account not yet authorized.")

    monkeypatch.setattr(cal, "_service", raise_not_authorized)
    result = cal.list_upcoming_events()
    assert result["ok"] is False
    assert result["state"] == "NOT_AUTHORIZED"
    assert result["events"] == []


def test_list_upcoming_events_api_error_does_not_crash(monkeypatch):
    def raise_error():
        raise Exception("network error")

    monkeypatch.setattr(cal, "_service", raise_error)
    result = cal.list_upcoming_events()
    assert result["ok"] is False
    assert result["state"] == "ERROR"


# ── create_event — the approval gate ──────────────────────────────────────

def test_create_event_refuses_without_approval_and_never_touches_the_api(monkeypatch):
    calls = []
    monkeypatch.setattr(cal, "_service", lambda: calls.append("called"))

    result = cal.create_event("Interview", "2026-08-15T09:00:00-05:00", "2026-08-15T10:00:00-05:00")
    assert result["ok"] is False
    assert result["state"] == "NOT_APPROVED"
    assert calls == []


def test_create_event_succeeds_when_approved(monkeypatch):
    fake = FakeCalendarService(insert_result={"id": "e1", "htmlLink": "https://calendar.google.com/e1"})
    monkeypatch.setattr(cal, "_service", lambda: fake)

    result = cal.create_event(
        "Interview: Jane Doe", "2026-08-15T09:00:00-05:00", "2026-08-15T10:00:00-05:00",
        location="Office", approved=True,
    )
    assert result["ok"] is True
    assert result["event_id"] == "e1"
    assert fake.inserted_bodies[0]["summary"] == "Interview: Jane Doe"
    assert fake.inserted_bodies[0]["location"] == "Office"


def test_create_event_approved_but_not_authorized(monkeypatch):
    def raise_not_authorized():
        raise RuntimeError("Google account not yet authorized.")

    monkeypatch.setattr(cal, "_service", raise_not_authorized)
    result = cal.create_event("x", "2026-08-15T09:00:00-05:00", "2026-08-15T10:00:00-05:00", approved=True)
    assert result["ok"] is False
    assert result["state"] == "NOT_AUTHORIZED"


# ── update_event — the approval gate + partial patch ──────────────────────

def test_update_event_refuses_without_approval(monkeypatch):
    calls = []
    monkeypatch.setattr(cal, "_service", lambda: calls.append("called"))

    result = cal.update_event("e1", summary="New title")
    assert result["ok"] is False
    assert result["state"] == "NOT_APPROVED"
    assert calls == []


def test_update_event_only_patches_provided_fields(monkeypatch):
    fake = FakeCalendarService(patch_result={"id": "e1"})
    monkeypatch.setattr(cal, "_service", lambda: fake)

    cal.update_event("e1", approved=True, summary="Rescheduled Interview")
    event_id, body = fake.patched_bodies[0]
    assert event_id == "e1"
    assert body == {"summary": "Rescheduled Interview"}   # location/start/end untouched


def test_update_event_can_patch_start_and_end(monkeypatch):
    fake = FakeCalendarService(patch_result={"id": "e1"})
    monkeypatch.setattr(cal, "_service", lambda: fake)

    cal.update_event("e1", approved=True, start_iso="2026-08-16T09:00:00-05:00", end_iso="2026-08-16T10:00:00-05:00")
    _, body = fake.patched_bodies[0]
    assert body == {
        "start": {"dateTime": "2026-08-16T09:00:00-05:00"},
        "end": {"dateTime": "2026-08-16T10:00:00-05:00"},
    }


# ── timezone safety (_ensure_tz) ────────────────────────────────────────

def test_ensure_tz_leaves_an_offset_aware_datetime_unchanged():
    assert cal._ensure_tz("2026-08-15T09:00:00-05:00") == "2026-08-15T09:00:00-05:00"


def test_ensure_tz_attaches_local_timezone_to_a_naive_datetime():
    result = cal._ensure_tz("2026-08-15T09:00:00")
    # Must gain a UTC offset — no longer naive.
    assert cal.datetime.datetime.fromisoformat(result).tzinfo is not None
    assert result.startswith("2026-08-15T09:00:00")


def test_ensure_tz_passes_through_unparseable_strings():
    assert cal._ensure_tz("not-a-real-date") == "not-a-real-date"


def test_create_event_normalizes_naive_datetimes_before_sending(monkeypatch):
    fake = FakeCalendarService(insert_result={"id": "e1"})
    monkeypatch.setattr(cal, "_service", lambda: fake)

    cal.create_event("Standup", "2026-08-15T09:00:00", "2026-08-15T09:30:00", approved=True)

    sent = fake.inserted_bodies[0]
    assert cal.datetime.datetime.fromisoformat(sent["start"]["dateTime"]).tzinfo is not None
    assert cal.datetime.datetime.fromisoformat(sent["end"]["dateTime"]).tzinfo is not None


# ── conflict detection ───────────────────────────────────────────────────

def test_create_event_refuses_when_a_conflict_exists(monkeypatch):
    fake = FakeCalendarService(list_result={"items": [_raw_event()]})
    monkeypatch.setattr(cal, "_service", lambda: fake)

    result = cal.create_event(
        "Overlapping meeting", "2026-08-15T09:15:00-05:00", "2026-08-15T09:45:00-05:00",
        approved=True,
    )
    assert result["ok"] is False
    assert result["state"] == "CONFLICT"
    assert len(result["conflicts"]) == 1
    assert result["conflicts"][0]["summary"] == "Interview: Jane Doe"
    assert fake.inserted_bodies == []   # never actually created


def test_create_event_succeeds_when_no_conflicts(monkeypatch):
    fake = FakeCalendarService(list_result={"items": []}, insert_result={"id": "e1"})
    monkeypatch.setattr(cal, "_service", lambda: fake)

    result = cal.create_event(
        "Clear slot", "2026-08-15T14:00:00-05:00", "2026-08-15T14:30:00-05:00", approved=True,
    )
    assert result["ok"] is True
    assert len(fake.inserted_bodies) == 1


def test_create_event_ignore_conflicts_bypasses_the_check(monkeypatch):
    fake = FakeCalendarService(list_result={"items": [_raw_event()]}, insert_result={"id": "e2"})
    monkeypatch.setattr(cal, "_service", lambda: fake)

    result = cal.create_event(
        "Double-booked on purpose", "2026-08-15T09:15:00-05:00", "2026-08-15T09:45:00-05:00",
        approved=True, ignore_conflicts=True,
    )
    assert result["ok"] is True
    assert len(fake.inserted_bodies) == 1


def test_conflict_check_failure_does_not_block_creation(monkeypatch):
    # A conflict-check failure (e.g. transient API error) must degrade to
    # "no known conflicts" rather than becoming a hard blocker in its own
    # right — the create still needs approved=True regardless.
    def raise_error(*a, **k):
        raise Exception("transient failure")

    class _FailingService:
        def events(self):
            return self

        def list(self, **kwargs):
            raise Exception("transient failure")

        def insert(self, calendarId, body):
            return _Execute({"id": "e1"})

    monkeypatch.setattr(cal, "_service", lambda: _FailingService())
    result = cal.create_event("x", "2026-08-15T09:00:00-05:00", "2026-08-15T09:30:00-05:00", approved=True)
    assert result["ok"] is True
