from actions.voice_navigation import (
    resolve_nucleus_target,
    resolve_nav_intent,
    parse_navigation_command,
)


# ── resolve_nucleus_target: natural-language variations from the spec ──

def test_open_buildpro_variations():
    for phrase in ("Open BuildPro", "Go to BuildPro", "Show BuildPro", "buildpro"):
        assert resolve_nucleus_target(phrase) == "buildpro", phrase


def test_open_daily_deal_finders_and_ddf_alias():
    assert resolve_nucleus_target("Open Daily Deal Finders") == "ddf"
    assert resolve_nucleus_target("Open DDF") == "ddf"


def test_open_careerrocket():
    assert resolve_nucleus_target("Open CareerRocket") == "careerrocket"
    assert resolve_nucleus_target("Open CareerRocket Pro") == "careerrocket"


def test_open_my_email():
    assert resolve_nucleus_target("Open my email") == "email"


def test_open_calendar_files_reports_communications():
    assert resolve_nucleus_target("Open calendar") == "calendar"
    assert resolve_nucleus_target("Open files") == "files"
    assert resolve_nucleus_target("Open reports") == "reports"
    assert resolve_nucleus_target("Open communications") == "communications"


def test_open_phone_maps_to_communications():
    assert resolve_nucleus_target("Open phone") == "communications"


def test_unrecognized_target_returns_none():
    assert resolve_nucleus_target("open the moon base") is None
    assert resolve_nucleus_target("") is None
    assert resolve_nucleus_target(None) is None


# ── resolve_nav_intent: back / home / status phrases ──

def test_go_back_intent():
    assert resolve_nav_intent("Go back") == "back"
    assert resolve_nav_intent("go back") == "back"


def test_go_home_and_show_everything_intent():
    assert resolve_nav_intent("Go home") == "home"
    assert resolve_nav_intent("Show me everything") == "home"


def test_what_am_i_looking_at_intent():
    assert resolve_nav_intent("What am I looking at?") == "status"


def test_non_intent_phrase_returns_none():
    assert resolve_nav_intent("Open BuildPro") is None


# ── parse_navigation_command: the full instruction the tool handler uses ──

def test_parse_clean_open_call():
    parsed = parse_navigation_command("open", "BuildPro")
    assert parsed == {"action": "open", "nucleus_id": "buildpro", "error": None}


def test_parse_clean_back_home_status_calls():
    assert parse_navigation_command("back", None)["action"] == "back"
    assert parse_navigation_command("home", None)["action"] == "home"
    assert parse_navigation_command("status", None)["action"] == "status"


def test_parse_phrase_in_target_slot_without_action():
    # Gemini sometimes won't cleanly split action vs target — the phrase can
    # land entirely in `target` with no `action` set.
    parsed = parse_navigation_command(None, "go back")
    assert parsed["action"] == "back"
    assert parsed["nucleus_id"] is None


def test_parse_phrase_in_action_slot():
    parsed = parse_navigation_command("go home", None)
    assert parsed["action"] == "home"


def test_parse_open_without_target_is_an_error():
    parsed = parse_navigation_command("open", None)
    assert parsed["error"] is not None
    assert parsed["nucleus_id"] is None


def test_parse_open_unrecognized_target_is_an_error():
    parsed = parse_navigation_command("open", "the moon base")
    assert parsed["error"] is not None


def test_parse_defaults_to_status_with_no_action_or_target():
    parsed = parse_navigation_command(None, None)
    assert parsed["action"] == "status"
    assert parsed["error"] is None
