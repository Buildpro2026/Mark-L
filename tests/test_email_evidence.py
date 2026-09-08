"""Evidence-based email classification, and the gate that stops a bad
classification from becoming a real email to a real stranger.

The live incident: a GitHub notification titled
"Re: [Buildpro2026/Mark-L] Add candidate matching for construction project
managers" hit gmail_integration's client_inquiry keyword rule on the bare
word "project" and got a business reply drafted to notifications@github.com.
The old automated-sender guard only matched "no-reply"/"noreply"/
"do-not-reply" — none of which appear in GitHub's address.

These tests pin the fix at the level it was actually made: precedence.
Content says what an email is about; the sender says whether a
relationship exists. Excluding github.com would satisfy the first test
below and none of the rest.
"""
import pytest

from actions import email_evidence as ev
from actions import gmail_integration as gmail


def _msg(**kw):
    base = {"subject": "", "sender": "", "snippet": "", "body": "",
            "attachments": [], "headers": {}}
    base.update(kw)
    return base


# ══ THE INCIDENT ═════════════════════════════════════════════════════════

GITHUB = _msg(
    subject="Re: [Buildpro2026/Mark-L] Add candidate matching for construction project managers",
    sender="GitHub <notifications@github.com>",
    body=("@buildpro2026 pushed 1 commit. This project now estimates candidate "
          "fit for construction hiring. View it on GitHub or unsubscribe."),
    headers={"List-Unsubscribe": "<mailto:unsub@github.com>",
             "List-Id": "Buildpro2026/Mark-L", "Reply-To": "reply@reply.github.com"},
)


def test_the_github_notification_is_automated_not_a_client():
    result = ev.classify(GITHUB)
    assert result["category"] == ev.AUTOMATED_SYSTEM
    assert result["sender_kind"] == ev.SENDER_MACHINE


def test_the_github_notification_can_never_trigger_outreach():
    allowed, reason = ev.can_act_autonomously(ev.classify(GITHUB))
    assert allowed is False
    assert "automated" in reason.lower() or "not a category" in reason.lower()


def test_legacy_classify_message_no_longer_calls_github_a_client():
    # The exact call the intake path makes. Before the precedence gate this
    # returned "client_inquiry" and a draft went out.
    assert gmail.classify_message(GITHUB) == "notification"


def test_the_fix_is_precedence_not_a_github_exclusion():
    # Same shape, a platform nobody wrote an exclusion for. If the fix were
    # a domain blocklist, this would still be misclassified.
    invented = _msg(
        subject="[acme-tracker] New comment on your construction project estimate",
        sender="notifications@some-tracker-nobody-listed.io",
        body="A new comment was posted. Manage your notification settings here.",
        headers={"List-Unsubscribe": "<mailto:x@y.io>"},
    )
    assert ev.classify(invented)["category"] == ev.AUTOMATED_SYSTEM
    assert gmail.classify_message(invented) == "notification"


# ══ SENDER IDENTITY IS STRUCTURAL ════════════════════════════════════════

def test_list_unsubscribe_header_marks_a_machine_sender():
    assert ev.sender_identity(_msg(sender="hello@company.com",
                                   headers={"List-Unsubscribe": "<x>"}))["kind"] == ev.SENDER_MACHINE


def test_a_role_address_marks_a_machine_sender():
    for local in ("notifications", "billing", "no-reply", "alerts", "security"):
        identity = ev.sender_identity(_msg(sender=f"{local}@anywhere.com"))
        assert identity["kind"] == ev.SENDER_MACHINE, local


def test_an_ordinary_mailbox_is_treated_as_a_person():
    assert ev.sender_identity(_msg(sender="jane.doe@acme.com"))["kind"] == ev.SENDER_HUMAN


def test_bulk_mail_with_unsubscribe_content_is_a_newsletter():
    news = _msg(subject="This week in construction tech",
                sender="digest@industryweekly.com",
                body="Top stories this week. Unsubscribe or update your preferences.",
                headers={"List-Unsubscribe": "<x>", "List-Id": "weekly"})
    assert ev.classify(news)["category"] == ev.NEWSLETTER_MARKETING


# ══ THE OTHER CATEGORIES ═════════════════════════════════════════════════

def test_a_receipt_is_transactional_not_a_client():
    receipt = _msg(subject="Your receipt from Stripe",
                   sender="receipts@stripe.com",
                   body="Payment received for your project subscription. Invoice attached.")
    assert ev.classify(receipt)["category"] == ev.TRANSACTIONAL


def test_a_verification_email_is_transactional():
    verify = _msg(subject="Your HubSpot verification code",
                  sender="noreply@hubspot.com",
                  body="Your verification code is below. Do not reply to this message.")
    assert ev.classify(verify)["category"] == ev.TRANSACTIONAL


def test_a_job_board_digest_is_a_job_alert_not_a_candidate():
    alert = _msg(subject="8 new jobs matching Project Manager",
                 sender="jobalerts-noreply@linkedin.com",
                 body="New jobs matching your search. Apply now on LinkedIn. Unsubscribe.")
    assert ev.classify(alert)["category"] == ev.RECRUITING_JOB_ALERT


def test_a_personal_note_stays_personal():
    note = _msg(subject="Dinner Saturday?",
                sender="mum@family.net",
                body="Happy birthday! Hope you're well and see you at dinner. The kids miss you.")
    assert ev.classify(note)["category"] == ev.PERSONAL


def test_a_vendor_email_is_standard_business_not_a_prospect():
    vendor = _msg(subject="Updated pricing for your account",
                  sender="alex@toolsupplier.com",
                  body="Hi Lee, here is our updated pricing sheet for the coming quarter.")
    assert ev.classify(vendor)["category"] == ev.STANDARD_BUSINESS


# ══ KEYWORD FALSE POSITIVES ══════════════════════════════════════════════

@pytest.mark.parametrize("word", ["construction", "project", "hiring",
                                  "recruiting", "job", "candidate", "company"])
def test_an_industry_word_alone_never_creates_a_client_or_candidate(word):
    msg = _msg(subject=f"Thoughts on {word}",
               sender="colleague@somewhere.com",
               body=f"Interesting piece about {word} trends this year. Worth a read.")
    result = ev.classify(msg)
    assert result["category"] not in (ev.CLIENT_PROSPECT, ev.CANDIDATE)
    assert any("vocabulary" in c for c in result["contradictory_evidence"])


def test_industry_words_from_a_machine_sender_are_doubly_refused():
    msg = _msg(subject="Your construction project hiring digest",
               sender="digest@platform.com",
               body="We are looking to hire? See candidate matches. Unsubscribe.",
               headers={"List-Unsubscribe": "<x>"})
    result = ev.classify(msg)
    assert result["category"] not in (ev.CLIENT_PROSPECT, ev.CANDIDATE)
    assert any("not a person" in c for c in result["contradictory_evidence"])


# ══ REAL CANDIDATES AND CLIENTS STILL WORK ═══════════════════════════════

def test_a_real_candidate_is_recognised():
    msg = _msg(subject="Application for Superintendent",
               sender="Jane Doe <jane.doe@gmail.com>",
               body="Hello, I am applying for the superintendent role. "
                    "My experience includes 12 years on commercial sites.")
    result = ev.classify(msg)
    assert result["category"] == ev.CANDIDATE
    assert result["confidence"] >= ev.ACTION_CONFIDENCE_FLOOR
    assert ev.can_act_autonomously(result)[0] is True


def test_a_resume_attachment_from_a_person_is_candidate_evidence():
    msg = _msg(subject="Hi", sender="bob@gmail.com", body="Please see attached.",
               attachments=[{"filename": "Bob_Resume.pdf"}])
    result = ev.classify(msg)
    assert result["category"] == ev.CANDIDATE
    assert any("resume attachment" in e for e in result["evidence"])


def test_a_resume_attachment_from_a_machine_is_not_a_candidate():
    msg = _msg(subject="Your document is ready", sender="noreply@docsigner.com",
               body="Your file has been processed. Do not reply to this message.",
               attachments=[{"filename": "resume_template.pdf"}])
    assert ev.classify(msg)["category"] != ev.CANDIDATE


def test_a_real_client_is_recognised():
    msg = _msg(subject="Hiring help",
               sender="Dana <dana@buildersinc.com>",
               body="Hi Lee — we are looking to hire two project managers this quarter "
                    "and are interested in your services.")
    result = ev.classify(msg)
    assert result["category"] == ev.CLIENT_PROSPECT
    assert ev.can_act_autonomously(result)[0] is True


def test_a_known_contact_raises_confidence_but_cannot_rescue_a_machine():
    machine = _msg(subject="We are looking to hire", sender="noreply@ats.com",
                   body="We are looking to hire. Do not reply to this message.")
    assert ev.classify(machine, known_contact=True)["category"] != ev.CLIENT_PROSPECT


# ══ CONFIDENCE AND THE ACTION FLOOR ══════════════════════════════════════

def test_every_result_carries_confidence_and_its_evidence():
    result = ev.classify(GITHUB)
    assert 0.0 <= result["confidence"] <= 1.0
    assert isinstance(result["evidence"], list) and result["evidence"]
    assert isinstance(result["contradictory_evidence"], list)


def test_a_low_confidence_actionable_guess_becomes_needs_review():
    downgraded = ev._result(ev.CLIENT_PROSPECT, 0.4, ["weak hint"], [],
                            {"kind": ev.SENDER_HUMAN, "domain": "x.com"},
                            {"candidate": [], "client": []})
    assert downgraded["category"] == ev.UNKNOWN_NEEDS_REVIEW
    assert downgraded["downgraded_from"] == ev.CLIENT_PROSPECT


def test_an_unidentifiable_sender_is_needs_review_not_a_guess():
    assert ev.classify(_msg(sender="", subject="?"))["category"] == ev.UNKNOWN_NEEDS_REVIEW


def test_acting_requires_affirmative_relationship_evidence():
    no_evidence = {"category": ev.CLIENT_PROSPECT, "confidence": 0.9,
                   "sender_kind": ev.SENDER_HUMAN,
                   "relationship_evidence": {"candidate": [], "client": []}}
    allowed, reason = ev.can_act_autonomously(no_evidence)
    assert allowed is False
    assert "affirmative evidence" in reason


# ══ THE DRAFT PATH ═══════════════════════════════════════════════════════

def test_the_monitor_withholds_a_draft_to_an_automated_sender(monkeypatch):
    from actions import buildpro_email_monitor as monitor
    from actions import business_intelligence as biz

    monkeypatch.setattr(gmail, "list_messages",
                        lambda **k: {"ok": True, "messages": [GITHUB]})
    monkeypatch.setattr(biz, "add_entry", lambda **k: None)

    def _must_not_draft(*a, **k):
        raise AssertionError("drafted a reply to an automated sender")
    monkeypatch.setattr(gmail, "create_draft", _must_not_draft)
    # Force the legacy label through so the gate itself is what stops it,
    # not the upstream classification fix — both layers must hold.
    monkeypatch.setattr(gmail, "classify_message", lambda m: "client_inquiry")

    out = monitor.scan_inbox(draft_replies=True)
    assert out["drafts_created"] == []
    assert len(out["drafts_blocked"]) == 1
    assert "automated" in out["drafts_blocked"][0]["reason"].lower()


def test_the_monitor_still_drafts_for_a_real_candidate(monkeypatch):
    from actions import buildpro_email_monitor as monitor
    from actions import business_intelligence as biz

    # Phrased so BOTH layers recognise it: the monitor still filters on
    # gmail_integration.classify_message()'s legacy labels (untouched
    # here), and the new gate then has to agree. "my resume" satisfies
    # both; "I am applying" alone satisfies only the new one, because the
    # legacy keyword list predates it. That asymmetry is pre-existing and
    # is deliberately not widened here — loosening the legacy rules would
    # add false positives, which is the opposite of this fix.
    real = _msg(id="m1", subject="Application for Superintendent",
                sender="jane.doe@gmail.com",
                body="I am applying for the role and my resume is attached. "
                     "My experience includes 12 years on site.")
    monkeypatch.setattr(gmail, "list_messages", lambda **k: {"ok": True, "messages": [real]})
    monkeypatch.setattr(biz, "add_entry", lambda **k: None)
    monkeypatch.setattr(gmail, "create_draft",
                        lambda to, subj, body: {"ok": True, "draft_id": "d1"})

    out = monitor.scan_inbox(draft_replies=True)
    assert len(out["drafts_created"]) == 1
    assert out["drafts_blocked"] == []
