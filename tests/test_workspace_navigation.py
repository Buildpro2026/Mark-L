"""One resolver for voice and clicks, and a workspace that never claims a
page opened when it did not.

Before this, voice went through navigate_command_center and clicks went
straight to apply_navigation, and neither produced a structured
destination — so anything that was not a nucleus id (an external page, a
specific record) fell out of the system and got read aloud as a URL for
Lee to click himself.
"""
import asyncio

import pytest

from actions import workspace_navigation as wn


# ══ SPOKEN COMMANDS RESOLVE ══════════════════════════════════════════════

@pytest.mark.parametrize("phrase,expect_id", [
    ("Open BuildPro", "buildpro"),
    ("open buildpro", "buildpro"),
    ("Take me to candidates", "candidates"),
    ("show me jobs", "jobs"),
])
def test_a_spoken_area_resolves_to_its_nucleus(phrase, expect_id):
    d = wn.resolve(phrase)
    assert d is not None, phrase
    assert d["destination_type"] == wn.TYPE_NUCLEUS
    assert d["destination_id"] == expect_id
    assert d["action"] == wn.ACTION_OPEN_NUCLEUS


def test_the_controls_resolve_to_controls_not_places():
    assert wn.resolve("go back")["action"] == wn.ACTION_BACK
    assert wn.resolve("take me home")["action"] == wn.ACTION_HOME
    assert wn.resolve("close this")["action"] == wn.ACTION_CLOSE
    for phrase in ("go back", "take me home", "close this"):
        assert wn.resolve(phrase)["destination_type"] == wn.TYPE_CONTROL


def test_a_place_whose_name_contains_a_control_word_is_still_a_place():
    # "Home Improvement" must not be swallowed by the "home" control.
    d = wn.resolve("Open Home Improvement", action="open", target="Home Improvement")
    assert d is None or d["destination_type"] != wn.TYPE_CONTROL


def test_an_unknown_area_resolves_to_nothing_rather_than_something_near():
    assert wn.resolve("open the flurgle department") is None


def test_an_unresolvable_request_is_reported_as_not_found():
    said = wn.describe(None, delivered=1)
    assert "couldn't find" in said.lower()


# ══ EXTERNAL PAGES ═══════════════════════════════════════════════════════

def test_a_spoken_url_resolves_to_an_external_destination():
    d = wn.resolve("open https://example.com/pricing")
    assert d["destination_type"] == wn.TYPE_EXTERNAL
    assert d["external_url"] == "https://example.com/pricing"


def test_a_bare_domain_is_understood_as_a_url():
    d = wn.resolve("open example.com")
    assert d["external_url"] == "https://example.com"


def test_an_embeddable_site_opens_in_the_workspace():
    d = wn.resolve("open https://example.com")
    assert d["embeddable"] is True
    assert d["action"] == wn.ACTION_OPEN_WORKSPACE


@pytest.mark.parametrize("url", [
    "https://mail.google.com/mail/u/0/#all/abc",
    "https://www.linkedin.com/in/someone",
    "https://app.hubspot.com/contacts/1/contact/2",
    "https://github.com/Buildpro2026/Mark-L",
    "https://www.amazon.com/dp/B0ABCD1234",
])
def test_a_site_that_refuses_framing_is_not_pretended_to_be_embedded(url):
    d = wn.resolve(f"open {url}")
    assert d["embeddable"] is False
    assert d["action"] == wn.ACTION_OPEN_EXTERNAL_TAB
    assert "refuses to be embedded" in d["detail"]


def test_a_refusing_site_still_gets_executed_not_handed_back_as_a_url():
    d = wn.resolve("open https://www.linkedin.com/feed")
    said = wn.describe(d, delivered=1)
    # JARVIS performed the navigation; it did not read a URL out.
    assert "opened" in said.lower()
    assert "https://" not in said


def test_subdomains_of_a_refusing_host_are_also_refused():
    assert wn.is_embeddable("https://foo.linkedin.com/x") is False
    assert wn.is_embeddable("https://notlinkedin.com/x") is True


def test_a_url_with_no_host_is_never_embedded():
    assert wn.is_embeddable("not a url") is False


# ══ RECORDS ══════════════════════════════════════════════════════════════

def test_a_record_resolves_through_the_existing_destination_builders():
    d = wn.resolve(record_source="hubspot_contact",
                   record_data={"contact_id": "42", "portal_id": "999"})
    assert d["destination_type"] == wn.TYPE_RECORD
    assert "app.hubspot.com" in d["external_url"]
    assert d["embeddable"] is False          # HubSpot refuses framing


def test_an_internal_record_keeps_its_internal_route():
    d = wn.resolve(record_source="approval", record_data={"task_id": "t1"})
    assert d["destination_route"].startswith("/ui#approvals/")
    assert d["action"] == wn.ACTION_OPEN_RECORD


def test_a_record_without_an_id_produces_no_destination():
    assert wn.resolve(record_source="hubspot_contact", record_data={}) is None


def test_an_unsafe_external_record_is_refused_by_the_existing_allowlist():
    assert wn.resolve(record_source="linkedin",
                      record_data={"thread_url": "https://evil.example.com/x"}) is None


# ══ EVERY DESTINATION CARRIES THE STRUCTURED FIELDS ══════════════════════

@pytest.mark.parametrize("kwargs", [
    {"request": "open BuildPro"},
    {"request": "open https://example.com"},
    {"request": "go back"},
    {"record_source": "approval", "record_data": {"task_id": "t1"}},
])
def test_every_destination_has_the_full_structured_shape(kwargs):
    d = wn.resolve(**kwargs)
    assert d is not None
    for field in ("destination_type", "destination_id", "destination_route",
                  "external_url", "action", "embeddable", "label"):
        assert field in d, f"{field} missing from {kwargs}"


# ══ THE SERVER EXECUTES IT, AND REPORTS WHAT REALLY HAPPENED ═════════════

class _FakeWS:
    def __init__(self): self.sent = []
    async def send_json(self, payload): self.sent.append(payload)


def _server(viewers=1):
    from dashboard.server import DashboardServer
    s = object.__new__(DashboardServer)
    s._3d_ws_clients = {_FakeWS() for _ in range(viewers)}
    s._nucleus_id = "jarvis"
    s._nucleus_back_stack = []
    return s


def test_executing_a_nucleus_destination_changes_state_and_broadcasts():
    server = _server()
    d = wn.resolve("open BuildPro")
    delivered = asyncio.run(server.execute_destination(d))
    assert delivered == 1
    assert server._nucleus_id == "buildpro"
    payload = next(iter(server._3d_ws_clients)).sent[0]
    assert payload["nav_action"] == wn.ACTION_OPEN_NUCLEUS
    assert payload["destination_id"] == "buildpro"


def test_executing_an_external_destination_carries_the_url_and_embeddability():
    server = _server()
    d = wn.resolve("open https://www.linkedin.com/feed")
    asyncio.run(server.execute_destination(d))
    payload = next(iter(server._3d_ws_clients)).sent[0]
    assert payload["external_url"] == "https://www.linkedin.com/feed"
    assert payload["embeddable"] is False
    assert payload["nav_action"] == wn.ACTION_OPEN_EXTERNAL_TAB


def test_back_and_home_execute_through_the_same_mutator():
    server = _server()
    asyncio.run(server.execute_destination(wn.resolve("open BuildPro")))
    assert server._nucleus_id == "buildpro"

    asyncio.run(server.execute_destination(wn.resolve("go back")))
    assert server._nucleus_id == "jarvis"

    asyncio.run(server.execute_destination(wn.resolve("open BuildPro")))
    asyncio.run(server.execute_destination(wn.resolve("take me home")))
    assert server._nucleus_id == "jarvis"
    assert server._nucleus_back_stack == []


def test_with_no_window_open_nothing_is_reported_as_opened():
    server = _server(viewers=0)
    d = wn.resolve("open BuildPro")
    delivered = asyncio.run(server.execute_destination(d))
    assert delivered == 0
    said = wn.describe(d, delivered)
    assert "no Command Center window is open" in said


def test_a_successful_navigation_is_reported_as_success():
    server = _server()
    d = wn.resolve("open BuildPro")
    delivered = asyncio.run(server.execute_destination(d))
    assert wn.describe(d, delivered) == "Opened BuildPro in the command center."


def test_executing_nothing_delivers_nothing():
    server = _server()
    assert asyncio.run(server.execute_destination(None)) == 0


def test_voice_and_click_share_one_executor():
    import inspect
    from dashboard import server as mod
    src = inspect.getsource(mod)
    assert src.count("def execute_destination") == 1
    assert src.count("def apply_navigation") == 1


# ── auto_opens: /ui IS the Command Center, no separate window required ────

def test_auto_opens_reports_the_destination_will_open_itself():
    # /ui's own browser tab executes navigation client-side when nothing
    # else picked it up (see index.html's actOnNavigation) — so it must
    # never be told to go open some other Command Center window first.
    d = wn.resolve("open BuildPro")
    said = wn.describe(d, 0, auto_opens=True)
    assert "open the command center" not in said.lower()
    assert "BuildPro" in said


def test_without_auto_opens_the_desktop_message_is_unchanged():
    # main.py's desktop path never passes auto_opens — there a Command
    # Center is a genuinely separate paired device and nothing here can
    # open a window on it, so the honest "go open it" message must stay.
    d = wn.resolve("open BuildPro")
    said = wn.describe(d, 0)
    assert "no Command Center window is open" in said.lower() or "no command center window" in said.lower()


def test_auto_opens_a_control_action_with_nothing_delivered_stays_honest():
    d = wn.resolve_control(wn.ACTION_HOME)
    said = wn.describe(d, 0, auto_opens=True)
    assert "nothing to" in said.lower()
