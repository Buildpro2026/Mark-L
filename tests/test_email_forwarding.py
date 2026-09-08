"""Who wrote this email, when it arrived through info@buildprorecruiters.com.

THE INCIDENT: info@ forwards into Gmail. Every identity path read
message["sender"] and took the envelope at face value, so a candidate's
application arrived, JARVIS read the sender as info@buildprorecruiters.com,
decided Lee was the candidate, and drafted a welcome email back to Lee.

The envelope says how a message was DELIVERED. It does not say who WROTE
it. These tests pin that separation, and pin the refusals: an
undetermined forward must never fall back to the envelope, because
falling back to the envelope is the bug.
"""
import pytest

from actions import email_forwarding as fw
from actions import email_evidence as ev


LEE = "info@buildprorecruiters.com"
LEE2 = "lchandler@buildprorecruiters.com"


def _msg(**kw):
    base = {"id": "m1", "subject": "", "sender": "", "snippet": "", "body": "",
            "attachments": [], "headers": {}, "to": "", "cc": ""}
    base.update(kw)
    return base


GMAIL_FORWARD_BODY = """FYI

---------- Forwarded message ---------
From: John Smith <john@example.com>
Date: Mon, 8 Sep 2026 at 09:14
Subject: Application for Site Superintendent
To: <info@buildprorecruiters.com>

Hello, I am applying for the superintendent role. My experience includes
12 years on commercial sites.
"""

OUTLOOK_FORWARD_BODY = """-----Original Message-----
From: Dana Reeves <dana@buildersinc.com>
Sent: Monday, September 8, 2026 9:14 AM
To: info@buildprorecruiters.com
Subject: Hiring help

We are looking to hire two project managers this quarter.
"""


# ══ DIRECT MAIL STILL WORKS EXACTLY AS BEFORE ════════════════════════════

def test_a_direct_candidate_email_is_not_treated_as_forwarded():
    msg = _msg(sender="Jane Doe <jane@example.com>",
               subject="Application", body="I am applying for the role.")
    a = fw.analyse(msg)
    assert a["is_forwarded"] is False
    assert a["determined"] is True
    assert a["original_sender_email"] == "jane@example.com"
    assert a["confidence"] == 1.0


def test_a_direct_employer_email_is_not_treated_as_forwarded():
    msg = _msg(sender="Dana <dana@buildersinc.com>",
               body="We are looking to hire two project managers.")
    a = fw.analyse(msg)
    assert a["is_forwarded"] is False
    assert a["original_sender_email"] == "dana@buildersinc.com"


def test_a_reply_to_on_a_direct_message_does_not_invent_a_forward():
    # GitHub-shaped: a routing Reply-To on a perfectly ordinary message.
    msg = _msg(sender="notifications@github.com",
               headers={"Reply-To": "reply@reply.github.com"})
    assert fw.analyse(msg)["is_forwarded"] is False


def test_a_message_with_no_sender_at_all_is_undetermined():
    a = fw.analyse(_msg(sender=""))
    assert a["determined"] is False
    assert a["reason"]


# ══ FORWARDED CANDIDATE ══════════════════════════════════════════════════

def test_a_forwarded_candidate_resolves_to_the_original_person():
    msg = _msg(sender=f"Lee Chandler <{LEE}>",
               subject="Fwd: Application for Site Superintendent",
               body=GMAIL_FORWARD_BODY)
    a = fw.analyse(msg)
    assert a["is_forwarded"] is True
    assert a["determined"] is True
    assert a["original_sender_email"] == "john@example.com"
    assert a["original_sender_name"] == "John Smith"


def test_the_forwarder_is_kept_separately_and_is_not_the_author():
    msg = _msg(sender=f"Lee Chandler <{LEE}>",
               subject="Fwd: Application", body=GMAIL_FORWARD_BODY)
    a = fw.analyse(msg)
    assert a["forwarder_email"] == LEE
    assert a["original_sender_email"] != LEE
    assert a["original_sender_email"] == "john@example.com"


def test_outer_sender_lee_inner_sender_candidate():
    # Lee's exact scenario, stated as the test name.
    msg = _msg(sender=f"Lee <{LEE}>", subject="Fwd: application",
               body=GMAIL_FORWARD_BODY)
    assert fw.analyse(msg)["original_sender_email"] == "john@example.com"
    assert fw.effective_sender(msg).endswith("<john@example.com>") or \
           "john@example.com" in fw.effective_sender(msg)


# ══ FORWARDED EMPLOYER ═══════════════════════════════════════════════════

def test_a_forwarded_employer_resolves_to_the_original_company_contact():
    msg = _msg(sender=f"Lee <{LEE}>", subject="FW: Hiring help",
               body=OUTLOOK_FORWARD_BODY)
    a = fw.analyse(msg)
    assert a["is_forwarded"] is True
    assert a["original_sender_email"] == "dana@buildersinc.com"
    assert a["original_sender_name"] == "Dana Reeves"


def test_the_apple_style_forward_marker_is_understood():
    body = ("Begin forwarded message:\n\nFrom: Ann Lee <ann@site.com>\n"
            "Subject: Quote\n\nWe need a quote.")
    msg = _msg(sender=f"Lee <{LEE}>", body=body)
    assert fw.analyse(msg)["original_sender_email"] == "ann@site.com"


# ══ HEADER-BASED FORWARDING ══════════════════════════════════════════════

def test_a_resent_from_header_identifies_the_author():
    msg = _msg(sender=f"Lee <{LEE}>",
               headers={"Resent-From": "Pat Rowe <pat@builder.co>",
                        "X-Forwarded-For": LEE})
    a = fw.analyse(msg)
    assert a["original_sender_email"] == "pat@builder.co"
    assert a["confidence"] >= 0.9, "an explicit header is the strongest evidence"


def test_an_original_sender_header_beats_a_body_line():
    msg = _msg(sender=f"Lee <{LEE}>", subject="Fwd: x",
               headers={"X-Original-From": "real@author.com"},
               body=GMAIL_FORWARD_BODY)
    assert fw.analyse(msg)["original_sender_email"] == "real@author.com"


def test_a_reply_to_inside_a_forward_is_used_when_nothing_better_exists():
    msg = _msg(sender=f"Lee <{LEE}>", subject="Fwd: enquiry",
               headers={"Reply-To": "Original Person <orig@company.com>",
                        "X-Forwarded-For": LEE},
               body="passing this on")
    a = fw.analyse(msg)
    assert a["original_sender_email"] == "orig@company.com"


# ══ MULTIPLE ORIGINAL RECIPIENTS ═════════════════════════════════════════

def test_original_recipients_are_collected_and_exclude_our_own_addresses():
    msg = _msg(sender=f"Lee <{LEE}>", subject="Fwd: Application",
               to=f"{LEE}, ops@buildersinc.com",
               cc="hr@buildersinc.com",
               body=GMAIL_FORWARD_BODY)
    recipients = fw.analyse(msg)["original_recipients"]
    assert "ops@buildersinc.com" in recipients
    assert "hr@buildersinc.com" in recipients
    assert LEE not in recipients, "the forwarding hop is not a correspondent"


# ══ THE REFUSALS ═════════════════════════════════════════════════════════

def test_a_forward_with_no_recoverable_author_is_undetermined():
    msg = _msg(sender=f"Lee <{LEE}>", subject="Fwd: see below",
               body="Thoughts on this one?")
    a = fw.analyse(msg)
    assert a["is_forwarded"] is True
    assert a["determined"] is False
    assert a["original_sender_email"] == ""
    assert "no recoverable original sender" in a["reason"]


def test_an_undetermined_forward_never_falls_back_to_the_envelope():
    # This is THE bug. The envelope must not survive into `sender`.
    msg = _msg(sender=f"Lee <{LEE}>", subject="Fwd: see below", body="thoughts?")
    resolved = fw.resolve_sender(msg)
    assert resolved["sender"] == ""
    assert resolved["forwarder"].endswith(f"<{LEE}>")


def test_our_own_address_is_never_accepted_as_the_original_author():
    msg = _msg(sender=f"Lee <{LEE}>", subject="Fwd: x",
               headers={"X-Original-From": LEE2, "X-Forwarded-For": LEE})
    a = fw.analyse(msg)
    assert a["determined"] is False
    assert a["original_sender_email"] == ""


@pytest.mark.parametrize("address", [LEE, LEE2, "INFO@BuildProRecruiters.com"])
def test_lee_and_the_forwarding_addresses_are_recognised_as_ours(address):
    assert fw.is_own_address(address) is True


def test_a_third_party_address_is_not_ours():
    assert fw.is_own_address("john@example.com") is False


# ══ THE REPLY RECIPIENT ══════════════════════════════════════════════════

def test_a_reply_to_a_forwarded_candidate_goes_to_the_candidate():
    msg = _msg(sender=f"Lee <{LEE}>", subject="Fwd: Application",
               body=GMAIL_FORWARD_BODY)
    address, why = fw.safe_reply_address(msg)
    assert address == "john@example.com"
    assert why


def test_a_reply_to_a_forwarded_employer_goes_to_the_employer():
    msg = _msg(sender=f"Lee <{LEE}>", subject="FW: Hiring help",
               body=OUTLOOK_FORWARD_BODY)
    assert fw.safe_reply_address(msg)[0] == "dana@buildersinc.com"


def test_a_reply_is_never_addressed_to_lee_because_of_the_envelope():
    msg = _msg(sender=f"Lee <{LEE}>", subject="Fwd: see below", body="thoughts?")
    address, why = fw.safe_reply_address(msg)
    assert address == ""
    assert LEE not in address
    assert why


def test_a_direct_message_still_replies_to_its_sender():
    msg = _msg(sender="Jane <jane@example.com>", body="I am applying.")
    assert fw.safe_reply_address(msg)[0] == "jane@example.com"


# ══ CLASSIFICATION USES THE ORIGINAL SENDER ══════════════════════════════

def test_a_forwarded_candidate_classifies_on_the_candidate_not_on_lee():
    msg = _msg(sender=f"Lee <{LEE}>", subject="Fwd: Application",
               body=GMAIL_FORWARD_BODY)
    result = ev.classify(msg)
    assert result["category"] == ev.CANDIDATE
    assert result["sender_domain"] == "example.com", "classified on the forwarder"
    assert any("forwarded by" in e for e in result["evidence"])


def test_a_forwarded_employer_classifies_as_a_client():
    msg = _msg(sender=f"Lee <{LEE}>", subject="FW: Hiring help",
               body=OUTLOOK_FORWARD_BODY)
    assert ev.classify(msg)["category"] == ev.CLIENT_PROSPECT


def test_an_undetermined_forward_classifies_as_needs_review():
    msg = _msg(sender=f"Lee <{LEE}>", subject="Fwd: see below", body="thoughts?")
    result = ev.classify(msg)
    assert result["category"] == ev.UNKNOWN_NEEDS_REVIEW
    assert result["contradictory_evidence"]


def test_a_low_confidence_forwarded_email_creates_no_draft():
    msg = _msg(sender=f"Lee <{LEE}>", subject="Fwd: see below", body="thoughts?")
    allowed, reason = ev.can_act_autonomously(ev.classify(msg))
    assert allowed is False
    assert reason


# ══ INTAKE USES THE ORIGINAL SENDER ══════════════════════════════════════

def test_candidate_intake_refuses_an_undetermined_forward(monkeypatch):
    from actions import candidate_intake

    def _must_not_draft(*a, **k):
        raise AssertionError("drafted from an undetermined forward")
    monkeypatch.setattr(candidate_intake.gmail_integration, "create_draft", _must_not_draft)

    msg = _msg(sender=f"Lee <{LEE}>", subject="Fwd: see below", body="thoughts?")
    result = candidate_intake.process_candidate_email(msg)
    assert result["ok"] is False
    assert result["state"] == "UNKNOWN_NEEDS_REVIEW"
    assert result["needs_review"] is True


def test_client_intake_refuses_an_undetermined_forward(monkeypatch):
    from actions import buildpro_client_intake

    def _must_not_draft(*a, **k):
        raise AssertionError("drafted from an undetermined forward")
    monkeypatch.setattr(buildpro_client_intake.gmail_integration, "create_draft", _must_not_draft)

    msg = _msg(sender=f"Lee <{LEE}>", subject="Fwd: see below", body="thoughts?")
    result = buildpro_client_intake.process_client_email(msg)
    assert result["ok"] is False
    assert result["state"] == "UNKNOWN_NEEDS_REVIEW"


def test_intake_refuses_when_the_only_sender_is_our_own_address(monkeypatch):
    from actions import candidate_intake
    msg = _msg(sender=f"Lee <{LEE}>", subject="Fwd: x",
               headers={"X-Original-From": LEE2, "X-Forwarded-For": LEE})
    result = candidate_intake.process_candidate_email(msg)
    assert result["ok"] is False
    assert result["state"] == "UNKNOWN_NEEDS_REVIEW"


# ══ THE MONITOR'S DRAFT PATH ═════════════════════════════════════════════

def test_the_monitor_addresses_a_forwarded_candidate_draft_to_the_candidate(monkeypatch):
    from actions import buildpro_email_monitor as monitor
    from actions import gmail_integration as gmail
    from actions import business_intelligence as biz

    msg = _msg(sender=f"Lee <{LEE}>", subject="Fwd: Application",
               body=GMAIL_FORWARD_BODY)
    monkeypatch.setattr(gmail, "list_messages", lambda **k: {"ok": True, "messages": [msg]})
    monkeypatch.setattr(biz, "add_entry", lambda **k: None)
    monkeypatch.setattr(gmail, "classify_message", lambda m: "candidate_reply")

    sent_to = []
    monkeypatch.setattr(gmail, "create_draft",
                        lambda to, subj, body: sent_to.append(to) or {"ok": True, "draft_id": "d1"})

    out = monitor.scan_inbox(draft_replies=True)
    assert sent_to == ["john@example.com"]
    assert LEE not in sent_to
    assert len(out["drafts_created"]) == 1


def test_the_monitor_withholds_a_draft_when_the_author_is_unknown(monkeypatch):
    from actions import buildpro_email_monitor as monitor
    from actions import gmail_integration as gmail
    from actions import business_intelligence as biz

    msg = _msg(sender=f"Lee <{LEE}>", subject="Fwd: see below", body="thoughts?")
    monkeypatch.setattr(gmail, "list_messages", lambda **k: {"ok": True, "messages": [msg]})
    monkeypatch.setattr(biz, "add_entry", lambda **k: None)
    monkeypatch.setattr(gmail, "classify_message", lambda m: "candidate_reply")

    def _must_not_draft(*a, **k):
        raise AssertionError("drafted to the forwarding envelope")
    monkeypatch.setattr(gmail, "create_draft", _must_not_draft)

    out = monitor.scan_inbox(draft_replies=True)
    assert out["drafts_created"] == []
    assert len(out["drafts_blocked"]) == 1
