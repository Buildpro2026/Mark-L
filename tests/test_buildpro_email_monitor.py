"""actions/buildpro_email_monitor.py (J3 Part 7) — real Gmail wiring for
the agent the J1/J2 audits both flagged as the next missing capability.
No live network calls: gmail_integration is fully mocked.
"""
from actions import buildpro_email_monitor as bem
from actions import business_intelligence as bi
from actions import gmail_integration


def _msg(id_, sender, subject, snippet="..."):
    return {"id": id_, "sender": sender, "subject": subject, "snippet": snippet}


def test_scan_inbox_reports_auth_failure_honestly(monkeypatch):
    monkeypatch.setattr(gmail_integration, "list_messages",
                         lambda query, max_results: {"ok": False, "state": "NOT_AUTHORIZED", "detail": "no token", "messages": []})
    result = bem.scan_inbox()
    assert result["ok"] is False
    assert result["state"] == "NOT_AUTHORIZED"
    assert result["relevant"] == []


def test_scan_inbox_classifies_and_logs_relevant_messages_only(monkeypatch, tmp_path):
    monkeypatch.setattr(bi, "DB_PATH", tmp_path / "bem_test.db")
    messages = [
        _msg("1", "jane@x.com", "My resume for the PM role"),          # candidate_reply
        _msg("2", "client@acme.com", "Requesting a bid for our project"),  # client_inquiry
        _msg("3", "no-reply@newsletter.com", "Weekly notification"),   # notification — irrelevant
        _msg("4", "friend@x.com", "Lunch tomorrow?"),                  # uncategorized — irrelevant
    ]
    monkeypatch.setattr(gmail_integration, "list_messages",
                         lambda query, max_results: {"ok": True, "messages": messages})
    monkeypatch.setattr(bi, "DB_PATH", tmp_path / "bem_test2.db")

    result = bem.scan_inbox(draft_replies=False)

    assert result["ok"] is True
    assert result["scanned"] == 4
    assert len(result["relevant"]) == 2
    classifications = {r["classification"] for r in result["relevant"]}
    assert classifications == {"candidate_reply", "client_inquiry"}
    assert result["drafts_created"] == []   # draft_replies=False — no drafts attempted

    logged = bi.list_entries(business="buildpro", limit=10)
    assert len(logged) == 2   # only the 2 relevant messages got logged, not the notification/uncategorized ones


def test_scan_inbox_drafts_acknowledgments_only_when_opted_in(monkeypatch, tmp_path):
    monkeypatch.setattr(bi, "DB_PATH", tmp_path / "bem_test3.db")
    messages = [_msg("1", "Jane Doe <jane@x.com>", "My resume for the PM role")]
    monkeypatch.setattr(gmail_integration, "list_messages",
                         lambda query, max_results: {"ok": True, "messages": messages})

    captured = {}
    def _fake_draft(to, subject, body):
        captured.update(to=to, subject=subject, body=body)
        return {"ok": True, "draft_id": "d1"}
    monkeypatch.setattr(gmail_integration, "create_draft", _fake_draft)

    result = bem.scan_inbox(draft_replies=True)

    assert len(result["drafts_created"]) == 1
    assert captured["to"] == "jane@x.com"
    assert captured["subject"] == "Re: My resume for the PM role"
    assert "BuildPro Recruiting" in captured["body"]


def test_scan_inbox_never_drafts_for_irrelevant_messages(monkeypatch, tmp_path):
    monkeypatch.setattr(bi, "DB_PATH", tmp_path / "bem_test4.db")
    messages = [_msg("1", "no-reply@newsletter.com", "Weekly notification")]
    monkeypatch.setattr(gmail_integration, "list_messages",
                         lambda query, max_results: {"ok": True, "messages": messages})
    called = {"n": 0}
    monkeypatch.setattr(gmail_integration, "create_draft", lambda *a: called.__setitem__("n", called["n"] + 1))

    result = bem.scan_inbox(draft_replies=True)
    assert result["drafts_created"] == []
    assert called["n"] == 0
