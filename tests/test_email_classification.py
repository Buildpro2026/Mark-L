from actions import email_classification as ec


def test_buildpro_candidate_via_legacy_rule():
    msg = {"subject": "Re: Application for Electrician role", "sender": "jane@example.com"}
    r = ec.classify_email(msg)
    assert r["category"] == ec.CATEGORY_BUILDPRO
    assert r["legacy_label"] == "candidate_reply"
    assert r["company_id"] == "buildpro"
    assert 0 < r["confidence"] <= 1


def test_buildpro_client_via_legacy_rule():
    msg = {"subject": "Quote request for new project", "sender": "client@construction.com"}
    r = ec.classify_email(msg)
    assert r["category"] == ec.CATEGORY_BUILDPRO
    assert r["legacy_label"] == "client_inquiry"


def test_buildpro_broadened_title_signal_senior_interior_designer():
    # Lee's explicit must-catch example: a non-executive, non-"construction"
    # title, with the real signal in the body under a generic subject.
    msg = {
        "subject": "Hi there",
        "sender": "pat@example.com",
        "body": "I'm currently a Senior Interior Designer looking for my next opportunity.",
    }
    r = ec.classify_email(msg)
    assert r["category"] == ec.CATEGORY_BUILDPRO
    assert "senior interior designer" in r["reason"]


def test_jarvis_own_infra_notification():
    msg = {
        "subject": "Your deploy failed",
        "sender": "notifications@render.com",
        "sender_domain": "render.com",
        "body": "Build failed for jarvis-headless-core.",
    }
    r = ec.classify_email(msg)
    assert r["category"] == ec.CATEGORY_JARVIS
    assert r["company_id"] == "jarvis"


def test_ddf_relevant_content_from_tiktok_is_not_irrelevant():
    # Section 5's explicit correction: platform alone must never drive the
    # decision, but real DDF-relevant content from a TikTok-domain sender
    # must be recognized as DDF, not dismissed as noise.
    msg = {
        "subject": "You're invited: Creator Fund opportunity",
        "sender": "creators@tiktok.com",
        "sender_domain": "tiktok.com",
        "body": "Join our Creator Fund and start earning commission on your content.",
    }
    r = ec.classify_email(msg)
    assert r["category"] == ec.CATEGORY_DDF


def test_ddf_platform_name_alone_is_not_a_positive_signal():
    # An ordinary personal notification from Instagram, with no deal/
    # creator/monetization content, must not be forced into DDF.
    msg = {
        "subject": "Someone liked your photo",
        "sender": "notify@instagram.com",
        "sender_domain": "instagram.com",
        "body": "yourfriend liked your photo.",
    }
    r = ec.classify_email(msg)
    assert r["category"] != ec.CATEGORY_DDF


def test_careerrocket_signal():
    msg = {
        "subject": "Interested in career coaching",
        "sender": "someone@example.com",
        "body": "I'd like to book a resume review and career coaching session.",
    }
    r = ec.classify_email(msg)
    assert r["category"] == ec.CATEGORY_CAREERROCKET
    assert r["company_id"] == "careerrocket"


def test_irrelevant_marketing_noise():
    msg = {
        "subject": "Our new feature is here!",
        "sender": "hello@render.com",
        "body": "Welcome! Render gives you a fast, reliable cloud application platform. Unsubscribe here.",
    }
    r = ec.classify_email(msg)
    assert r["category"] == ec.CATEGORY_IRRELEVANT
    assert r["company_id"] is None


def test_irrelevant_never_gets_a_company_id():
    msg = {"subject": "Weekly digest", "sender": "no-reply@service.com", "body": "unsubscribe anytime"}
    r = ec.classify_email(msg)
    assert r["category"] == ec.CATEGORY_IRRELEVANT
    assert r["company_id"] is None


def test_personal_real_sender_no_business_signal():
    msg = {"subject": "Dinner Friday?", "sender": "mom@example.com", "body": "Want to grab dinner this Friday?"}
    r = ec.classify_email(msg)
    assert r["category"] == ec.CATEGORY_PERSONAL
    assert r["company_id"] == "personal"


def test_review_required_ambiguous_automated_sender():
    msg = {"subject": "Hello", "sender": "no-reply@mystery.example", "body": ""}
    r = ec.classify_email(msg)
    assert r["category"] in (ec.CATEGORY_REVIEW, ec.CATEGORY_IRRELEVANT)


def test_never_forces_buildpro_when_legacy_label_is_notification():
    msg = {"subject": "Your weekly digest", "sender": "no-reply@service.com"}
    r = ec.classify_email(msg)
    assert r["category"] != ec.CATEGORY_BUILDPRO


def test_result_always_has_all_five_keys():
    msg = {"subject": "x", "sender": "a@b.com"}
    r = ec.classify_email(msg)
    assert set(r.keys()) == {"category", "confidence", "reason", "company_id", "legacy_label"}
    assert r["category"] in ec.ALL_CATEGORIES
