from actions.integrations import (
    build_provider_status_payload,
    normalize_email_message,
    normalize_calendar_event,
)
from actions.nucleus_hierarchy import (
    get_hierarchy_node,
    get_hierarchy_path,
    get_hierarchy_children,
    get_hierarchy_root,
)


def test_hierarchy_exposes_central_nucleus_and_domain_children():
    root = get_hierarchy_node("jarvis")
    assert root is not None
    assert root["name"] == "Jarvis"
    children = get_hierarchy_children("jarvis")
    assert any(child["id"] == "buildpro" for child in children)
    assert any(child["id"] == "email" for child in children)


def test_hierarchy_navigation_supports_parent_and_child_paths():
    path = get_hierarchy_path("buildpro")
    assert path[0]["id"] == "jarvis"
    assert any(node["id"] == "buildpro" for node in path)

    children = get_hierarchy_children("buildpro")
    assert children
    assert any(child["id"] == "clients" for child in children)


def test_hierarchy_includes_the_full_spatial_command_center_nucleus_set():
    # Phase 2 requires these 9 top-level Nuclei around the central Jarvis core.
    root = get_hierarchy_root()
    child_ids = {child["id"] for child in root.get("children", [])}
    expected = {
        "buildpro", "ddf", "careerrocket", "email", "calendar",
        "files", "reports", "communications", "system",
    }
    assert expected <= child_ids


def test_hierarchy_has_no_duplicate_ids_or_names():
    # A duplicate id or display name breaks voice-command resolution — the
    # first match wins silently, so a collision is easy to introduce by
    # accident when adding new Nuclei/children (regression test for the
    # files-vs-system-files / files-reports-vs-reports bugs found while
    # building Phase 1).
    root = get_hierarchy_root()
    ids: list[str] = []
    names: list[str] = []

    def _walk(node):
        ids.append(node["id"])
        names.append(node["name"].strip().lower())
        for child in node.get("children", []) or []:
            _walk(child)

    _walk(root)
    assert len(ids) == len(set(ids)), "duplicate nucleus id in hierarchy"
    assert len(names) == len(set(names)), "duplicate nucleus name in hierarchy"


def test_communications_nucleus_children_are_not_statically_flagged_placeholder():
    # actions/twilio_integration.py is real, working code — the static
    # hierarchy (this file's source, config/nucleus_config.json) no longer
    # hardcodes "placeholder": True here. Whether these render as live or as
    # placeholders in the 3D UI is now computed dynamically at request time
    # from actual Twilio-configured state — see
    # dashboard/server.py's _overlay_communications_placeholder and
    # tests/test_communications_placeholder_overlay.py for that behavior.
    children = get_hierarchy_children("communications")
    assert children
    assert all("placeholder" not in child for child in children)


def test_provider_status_payload_reports_not_connected_when_google_unauthorized(monkeypatch):
    import actions.integrations as integrations

    monkeypatch.setattr(
        integrations, "get_credential_status",
        lambda: {"credential_file": "missing", "credential_type": None, "token_cached": False, "authorized": False},
    )
    payload = build_provider_status_payload()
    assert payload["email"]["status"] == "not_connected"
    assert payload["calendar"]["status"] == "not_connected"
    assert payload["email"]["providers"]["google"]["status"] == "not_connected"


def test_provider_status_payload_reports_connected_when_google_authorized(monkeypatch):
    # Regression test for the real bug this replaced: the old check read a
    # "google_credentials" config/api_keys.json key that the actual Google
    # OAuth flow (actions/google_auth.py) never writes to — it caches a
    # token at config/google/token.json instead — so email/calendar always
    # reported not_connected even when genuinely authorized.
    import actions.integrations as integrations

    monkeypatch.setattr(
        integrations, "get_credential_status",
        lambda: {"credential_file": "present", "credential_type": "installed", "token_cached": True, "authorized": True},
    )
    payload = build_provider_status_payload()
    assert payload["email"]["status"] == "connected"
    assert payload["calendar"]["status"] == "connected"
    assert payload["email"]["providers"]["google"]["status"] == "connected"
    assert payload["email"]["providers"]["google"]["account_count"] == 1


def test_provider_status_payload_reflects_this_repos_real_cached_google_token():
    # This repo's config/google/token.json is a real cached OAuth token
    # (confirmed live-authenticated by docs/OPERATIONS.md's health check) —
    # the payload must reflect that real state, not a hardcoded key that's
    # never actually populated.
    payload = build_provider_status_payload()
    assert payload["email"]["providers"]["google"]["status"] == "connected"


def test_provider_normalizers_return_provider_agnostic_models_without_live_data():
    normalized_message = normalize_email_message(
        source="gmail",
        subject="Quarterly update",
        sender="person@example.com",
        body="Hello",
    )
    assert normalized_message["provider"] == "gmail"
    assert normalized_message["subject"] == "Quarterly update"

    normalized_event = normalize_calendar_event(
        provider="google",
        title="Planning",
        start="2026-08-09T10:00:00Z",
        end="2026-08-09T11:00:00Z",
    )
    assert normalized_event["provider"] == "google"
    assert normalized_event["title"] == "Planning"
