"""Retiring the business records the old email classifier created wrongly.

The old rules turned any message mentioning "project" into a
client_inquiry, so GitHub and other platforms are recorded in business
intelligence as leads. These tests pin the two rules that make the cleanup
safe: nothing is deleted, and only a conclusively automated sender is
touched.
"""
import pytest

from actions import email_action_cleanup as cleanup
from actions import business_intelligence as biz


GITHUB_ENTRY = {
    "id": 1,
    "title": "[Email:client_inquiry] Re: [Mark-L] construction project managers",
    "content": "From: GitHub <notifications@github.com>\nSnippet: a new comment",
    "data": {},
}
REAL_CLIENT_ENTRY = {
    "id": 2,
    "title": "[Email:client_inquiry] Hiring two PMs",
    "content": "From: Dana <dana@buildersinc.com>\nSnippet: we are looking to hire",
    "data": {},
}
REAL_CANDIDATE_ENTRY = {
    "id": 3,
    "title": "[Email:candidate_reply] Application",
    "content": "From: jane.doe@gmail.com\nSnippet: my resume is attached",
    "data": {},
}


# ══ WHAT GETS IDENTIFIED ═════════════════════════════════════════════════

def test_a_machine_sender_recorded_as_a_client_is_identified():
    found = cleanup.find_false_business_entries([GITHUB_ENTRY])
    assert len(found) == 1
    assert found[0]["id"] == 1
    assert found[0]["sender_kind"] == cleanup.email_evidence.SENDER_MACHINE
    assert "automated system" in found[0]["reason"]


def test_a_real_client_is_left_alone():
    assert cleanup.find_false_business_entries([REAL_CLIENT_ENTRY]) == []


def test_a_real_candidate_is_left_alone():
    assert cleanup.find_false_business_entries([REAL_CANDIDATE_ENTRY]) == []


def test_an_entry_whose_sender_cannot_be_re_evaluated_is_left_alone():
    unknown = {"id": 4, "title": "[Email:client_inquiry] Something",
               "content": "no sender line here", "data": {}}
    assert cleanup.find_false_business_entries([unknown]) == []


def test_unrelated_entries_are_never_considered():
    other = {"id": 5, "title": "Weekly revenue summary",
             "content": "From: notifications@github.com", "data": {}}
    assert cleanup.find_false_business_entries([other]) == []


def test_the_sender_is_read_from_structured_data_when_present():
    entry = {"id": 6, "title": "[Email:candidate_reply] x",
             "content": "", "data": {"sender": "noreply@ats.example.com"}}
    assert len(cleanup.find_false_business_entries([entry])) == 1


def test_a_json_encoded_data_blob_is_understood():
    entry = {"id": 7, "title": "[Email:client_inquiry] x", "content": "",
             "data": '{"sender": "notifications@gitlab.com"}'}
    assert len(cleanup.find_false_business_entries([entry])) == 1


def test_a_mixed_batch_separates_correctly():
    found = cleanup.find_false_business_entries(
        [GITHUB_ENTRY, REAL_CLIENT_ENTRY, REAL_CANDIDATE_ENTRY])
    assert [f["id"] for f in found] == [1]


# ══ NOTHING IS DELETED ═══════════════════════════════════════════════════

@pytest.fixture
def _store(monkeypatch, tmp_path):
    monkeypatch.setattr(biz, "DB_PATH", tmp_path / "bi.db")
    return biz


def test_a_dry_run_changes_nothing(monkeypatch, _store):
    monkeypatch.setattr(biz, "list_entries", lambda **k: [GITHUB_ENTRY])
    written = []
    monkeypatch.setattr(biz, "add_entry", lambda **k: written.append(k))

    result = cleanup.clean_up(dry_run=True)
    assert result["ok"] is True
    assert len(result["found"]) == 1
    assert result["corrected"] == 0
    assert written == [], "a dry run wrote to the store"


def test_a_real_run_supersedes_rather_than_deletes(monkeypatch, _store):
    monkeypatch.setattr(biz, "list_entries", lambda **k: [GITHUB_ENTRY])
    written = []
    monkeypatch.setattr(biz, "add_entry", lambda **k: written.append(k) or 99)

    result = cleanup.clean_up(dry_run=False)
    assert result["corrected"] == 1
    assert len(written) == 1
    correction = written[0]
    assert correction["title"] == cleanup.CORRECTION_TITLE
    assert correction["related_id"] == 1
    assert correction["data"]["supersedes"] == 1
    assert "retained for audit" in correction["content"]


def test_the_cleanup_never_calls_a_delete_path():
    import inspect
    source = inspect.getsource(cleanup)
    for forbidden in ("DELETE FROM", "delete_entry", "drop table", "DROP TABLE"):
        assert forbidden not in source, f"{forbidden} appears in the cleanup path"


def test_a_clean_store_reports_nothing_to_do(monkeypatch, _store):
    monkeypatch.setattr(biz, "list_entries", lambda **k: [REAL_CLIENT_ENTRY])
    result = cleanup.clean_up(dry_run=False)
    assert result["found"] == []
    assert result["corrected"] == 0
    assert "No falsely-derived" in result["summary"]


def test_a_store_failure_is_reported_not_swallowed(monkeypatch, _store):
    def _boom(**k):
        raise RuntimeError("db is locked")
    monkeypatch.setattr(biz, "list_entries", _boom)
    result = cleanup.clean_up(dry_run=True)
    assert result["ok"] is False
    assert "locked" in result["detail"]
