from actions import google_tasks_integration as gt


class _Execute:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeTasksService:
    def __init__(self, list_result=None, insert_result=None, patch_result=None, get_result=None):
        self._list_result = list_result or {"items": []}
        self._insert_result = insert_result
        self._patch_result = patch_result
        self._get_result = get_result
        self.inserted_bodies = []
        self.patched = []
        self.deleted = []

    def tasks(self):
        return self

    def list(self, tasklist, maxResults=None, showCompleted=None, showHidden=None):
        return _Execute(self._list_result)

    def get(self, tasklist, task):
        return _Execute(self._get_result)

    def insert(self, tasklist, body):
        self.inserted_bodies.append(body)
        return _Execute(self._insert_result)

    def patch(self, tasklist, task, body):
        self.patched.append((task, body))
        return _Execute(self._patch_result)

    def delete(self, tasklist, task):
        self.deleted.append(task)
        return _Execute(None)


def _raw_task():
    return {
        "id": "t1", "title": "Follow up with candidate", "notes": "call about offer",
        "status": "needsAction", "due": "2026-08-20T00:00:00.000Z",
        "selfLink": "https://tasks.google.com/t1",
    }


# ── list_tasks ───────────────────────────────────────────────────

def test_list_tasks_normalizes_fields(monkeypatch):
    fake = FakeTasksService(list_result={"items": [_raw_task()]})
    monkeypatch.setattr(gt, "_service", lambda: fake)

    result = gt.list_tasks()
    assert result["ok"] is True
    assert len(result["tasks"]) == 1
    task = result["tasks"][0]
    assert task["title"] == "Follow up with candidate"
    assert task["status"] == "needsAction"
    assert task["due"] == "2026-08-20T00:00:00.000Z"


def test_list_tasks_not_authorized(monkeypatch):
    def raise_not_authorized():
        raise RuntimeError("Google account not yet authorized.")

    monkeypatch.setattr(gt, "_service", raise_not_authorized)
    result = gt.list_tasks()
    assert result["ok"] is False
    assert result["state"] == "NOT_AUTHORIZED"
    assert result["tasks"] == []


def test_list_tasks_api_error_does_not_crash(monkeypatch):
    def raise_error():
        raise Exception("insufficient scope")

    monkeypatch.setattr(gt, "_service", raise_error)
    result = gt.list_tasks()
    assert result["ok"] is False
    assert result["state"] == "ERROR"
    assert result["tasks"] == []


# ── get_task ────────────────────────────────────────────

def test_get_task_returns_normalized_task(monkeypatch):
    fake = FakeTasksService(get_result=_raw_task())
    monkeypatch.setattr(gt, "_service", lambda: fake)

    result = gt.get_task("t1")
    assert result["ok"] is True
    assert result["task"]["id"] == "t1"


def test_get_task_not_authorized(monkeypatch):
    def raise_not_authorized():
        raise RuntimeError("Google account not yet authorized.")

    monkeypatch.setattr(gt, "_service", raise_not_authorized)
    result = gt.get_task("t1")
    assert result["ok"] is False
    assert result["state"] == "NOT_AUTHORIZED"


# ── create_task — the approval gate ────────────────────────

def test_create_task_refuses_without_approval_and_never_touches_the_api(monkeypatch):
    calls = []
    monkeypatch.setattr(gt, "_service", lambda: calls.append("called"))

    result = gt.create_task("Follow up with candidate")
    assert result["ok"] is False
    assert result["state"] == "NOT_APPROVED"
    assert calls == []


def test_create_task_succeeds_when_approved(monkeypatch):
    fake = FakeTasksService(insert_result={"id": "t1", "selfLink": "https://tasks.google.com/t1"})
    monkeypatch.setattr(gt, "_service", lambda: fake)

    result = gt.create_task(
        "Follow up with candidate", notes="call about offer", due_iso="2026-08-20T00:00:00.000Z", approved=True,
    )
    assert result["ok"] is True
    assert result["task_id"] == "t1"
    assert fake.inserted_bodies[0]["title"] == "Follow up with candidate"
    assert fake.inserted_bodies[0]["notes"] == "call about offer"
    assert fake.inserted_bodies[0]["due"] == "2026-08-20T00:00:00.000Z"


def test_create_task_omits_optional_fields_when_not_given(monkeypatch):
    fake = FakeTasksService(insert_result={"id": "t1"})
    monkeypatch.setattr(gt, "_service", lambda: fake)

    gt.create_task("Bare task", approved=True)
    assert fake.inserted_bodies[0] == {"title": "Bare task"}


def test_create_task_approved_but_not_authorized(monkeypatch):
    def raise_not_authorized():
        raise RuntimeError("Google account not yet authorized.")

    monkeypatch.setattr(gt, "_service", raise_not_authorized)
    result = gt.create_task("x", approved=True)
    assert result["ok"] is False
    assert result["state"] == "NOT_AUTHORIZED"


def test_create_task_insufficient_scope_is_an_honest_error_not_a_crash(monkeypatch):
    # The real-world case this scope addition creates: an old cached token
    # that predates the tasks scope. Must never crash, never fabricate ok=True.
    def raise_scope_error():
        raise Exception("insufficientPermissions: Request had insufficient authentication scopes.")

    monkeypatch.setattr(gt, "_service", raise_scope_error)
    result = gt.create_task("x", approved=True)
    assert result["ok"] is False
    assert result["state"] == "ERROR"
    assert "insufficient" in result["detail"].lower()


# ── update_task — the approval gate + partial patch ────────────────

def test_update_task_refuses_without_approval(monkeypatch):
    calls = []
    monkeypatch.setattr(gt, "_service", lambda: calls.append("called"))

    result = gt.update_task("t1", title="New title")
    assert result["ok"] is False
    assert result["state"] == "NOT_APPROVED"
    assert calls == []


def test_update_task_only_patches_provided_fields(monkeypatch):
    fake = FakeTasksService(patch_result={"id": "t1"})
    monkeypatch.setattr(gt, "_service", lambda: fake)

    gt.update_task("t1", approved=True, title="Rescheduled follow-up")
    task_id, body = fake.patched[0]
    assert task_id == "t1"
    assert body == {"title": "Rescheduled follow-up"}   # notes/due untouched


def test_update_task_can_patch_notes_and_due(monkeypatch):
    fake = FakeTasksService(patch_result={"id": "t1"})
    monkeypatch.setattr(gt, "_service", lambda: fake)

    gt.update_task("t1", approved=True, notes="rescheduled", due_iso="2026-08-25T00:00:00.000Z")
    _, body = fake.patched[0]
    assert body == {"notes": "rescheduled", "due": "2026-08-25T00:00:00.000Z"}


# ── complete_task ───────────────────────────────────

def test_complete_task_refuses_without_approval(monkeypatch):
    calls = []
    monkeypatch.setattr(gt, "_service", lambda: calls.append("called"))

    result = gt.complete_task("t1")
    assert result["ok"] is False
    assert result["state"] == "NOT_APPROVED"
    assert calls == []


def test_complete_task_patches_status_completed(monkeypatch):
    fake = FakeTasksService(patch_result={"id": "t1"})
    monkeypatch.setattr(gt, "_service", lambda: fake)

    result = gt.complete_task("t1", approved=True)
    assert result["ok"] is True
    task_id, body = fake.patched[0]
    assert task_id == "t1"
    assert body == {"status": "completed"}


# ── delete_task ────────────────────────────────────

def test_delete_task_refuses_without_approval(monkeypatch):
    calls = []
    monkeypatch.setattr(gt, "_service", lambda: calls.append("called"))

    result = gt.delete_task("t1")
    assert result["ok"] is False
    assert result["state"] == "NOT_APPROVED"
    assert calls == []


def test_delete_task_succeeds_when_approved(monkeypatch):
    fake = FakeTasksService()
    monkeypatch.setattr(gt, "_service", lambda: fake)

    result = gt.delete_task("t1", approved=True)
    assert result["ok"] is True
    assert fake.deleted == ["t1"]


def test_delete_task_not_authorized(monkeypatch):
    def raise_not_authorized():
        raise RuntimeError("Google account not yet authorized.")

    monkeypatch.setattr(gt, "_service", raise_not_authorized)
    result = gt.delete_task("t1", approved=True)
    assert result["ok"] is False
    assert result["state"] == "NOT_AUTHORIZED"
