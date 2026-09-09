"""One resolver for every way of moving around the Command Center.

WHY THIS EXISTS
Navigation had two halves that could disagree. Voice went
main.py -> navigate_command_center -> DashboardServer.apply_navigation();
clicks in the 3D scene went straight to apply_navigation over the
websocket. Both reached the same mutator, which was right, but neither
produced a STRUCTURED destination — so "open BuildPro" and clicking the
BuildPro planet could only ever mean "change nucleus_id", and there was
nowhere to express "open this candidate record" or "open this webpage".
Anything that was not a nucleus id fell out of the system entirely, which
is why external pages ended up as a URL read aloud for Lee to click.

resolve() is the single place a request becomes a destination, and it
returns the same shape whatever asked for it:

    destination_type   nucleus | record | external | control
    destination_id     the nucleus/record id, when there is one
    destination_route  the internal route to open, when there is one
    external_url       the external page, when there is one
    action             what the client should DO with it
    embeddable         whether that page can legally be framed

apply_navigation() stays exactly where it is and stays the only thing
that mutates navigation state. This resolves; it does not navigate.

ON EMBEDDING, HONESTLY
"Open it in the workspace" is only possible for pages that permit
framing. Google, LinkedIn, HubSpot's app, GitHub and the rest send
X-Frame-Options or a frame-ancestors CSP and will render an empty box
inside an iframe — which looks exactly like a broken JARVIS. So known
refusers are marked embeddable=False and get action=open_external_tab:
the client still EXECUTES the navigation (window.open), which is real
navigation and not a URL handed back to the user, but nothing claims the
page was embedded. A host we have no knowledge of is attempted in the
workspace with a load probe behind it; the client falls back to a tab if
the frame stays blank. Neither path ever reports success it did not have.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional
from urllib.parse import urlparse

from actions import notification_destinations as dest

logger = logging.getLogger("jarvis.workspace_navigation")

# ── destination types ────────────────────────────────────────────────────
TYPE_NUCLEUS = "nucleus"      # a node in the Command Center hierarchy
TYPE_RECORD = "record"        # a specific business record (contact, email, ...)
TYPE_EXTERNAL = "external"    # a page outside JARVIS
TYPE_CONTROL = "control"      # back / home / close — no destination of its own

# ── actions the client performs ──────────────────────────────────────────
ACTION_OPEN_NUCLEUS = "open_nucleus"
ACTION_OPEN_RECORD = "open_record"
ACTION_OPEN_WORKSPACE = "open_workspace"        # embed in the workspace frame
ACTION_OPEN_EXTERNAL_TAB = "open_external_tab"  # execute, but in a real tab
ACTION_BACK = "back"
ACTION_HOME = "home"
ACTION_CLOSE = "close"

CONTROL_ACTIONS = frozenset({ACTION_BACK, ACTION_HOME, ACTION_CLOSE})

# Hosts that refuse framing. Marked so the workspace never shows an empty
# box and calls it success. Not a security control — an honesty one.
_FRAME_REFUSERS: tuple[str, ...] = (
    "google.com", "mail.google.com", "calendar.google.com", "accounts.google.com",
    "docs.google.com", "drive.google.com",
    "linkedin.com", "www.linkedin.com",
    "app.hubspot.com", "hubspot.com",
    "github.com", "www.github.com",
    "facebook.com", "www.facebook.com", "instagram.com", "www.instagram.com",
    "x.com", "twitter.com", "www.tiktok.com", "tiktok.com",
    "amazon.com", "www.amazon.com",
    "publish.buffer.com", "buffer.com",
    "render.com", "dashboard.render.com",
)

# Spoken phrasings that mean a control rather than a place.
_CONTROL_PHRASES: tuple[tuple[str, str], ...] = (
    (r"\b(go\s+back|back|previous)\b", ACTION_BACK),
    (r"\b(home|main|start|centre|center)\b", ACTION_HOME),
    (r"\b(close|dismiss|exit|shut\s+this)\b", ACTION_CLOSE),
)

# Words that carry no destination of their own. After a control phrase is
# removed, a remainder made only of these means the user named a control
# and nothing else: "take me home" -> "take me" -> HOME, while "go back to
# BuildPro" -> "go to BuildPro" -> a place, not a control.
_FILLER = frozenset({
    "this", "it", "me", "us", "please", "now", "jarvis", "to", "the", "that",
    "up", "go", "take", "bring", "get", "head", "jump", "navigate", "return",
    "send", "lets", "let's", "screen", "page", "window", "one",
})

# Command verbs stripped before a name is looked up. Longest first, so
# "show me jobs" loses "show me" rather than just "show" and leaving "me".
_VERB_PREFIX = re.compile(
    r"^\s*(?:please\s+)?(?:"
    r"take\s+me\s+to|navigate\s+to|bring\s+up|switch\s+to|jump\s+to|"
    r"go\s+back\s+to|back\s+to|"
    r"show\s+me|go\s+to|open\s+up|open|show|display|view"
    r")\s+", re.I)

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
_BARE_DOMAIN_RE = re.compile(r"\b((?:[\w-]+\.)+[a-z]{2,})(/[^\s]*)?\b", re.I)

# Well-known sites spoken/typed by brand name, without a domain suffix —
# "open Google", "open YouTube". _BARE_DOMAIN_RE above requires a literal
# dot, so a bare brand name never matched it and fell all the way through
# to resolve_nucleus(), which of course has no such area either — "open
# Google" resolved to nothing at all, the actual reported bug. Deliberately
# a small, explicit allowlist rather than "assume any single word is a
# website": an unlisted bare word still falls through to resolve_nucleus()
# and gets an honest "I couldn't find that" rather than a guessed domain.
_KNOWN_SITE_NAMES: dict[str, str] = {
    "google": "https://google.com",
    "youtube": "https://youtube.com",
    "gmail": "https://mail.google.com",
    "github": "https://github.com",
    "linkedin": "https://linkedin.com",
    "hubspot": "https://app.hubspot.com",
    "amazon": "https://amazon.com",
    "facebook": "https://facebook.com",
    "instagram": "https://instagram.com",
    "twitter": "https://x.com",
    "x": "https://x.com",
    "tiktok": "https://www.tiktok.com",
    "buffer": "https://publish.buffer.com",
}


def is_embeddable(url: str) -> bool:
    """False for hosts known to send X-Frame-Options / frame-ancestors.

    Unknown hosts return True: the workspace attempts the embed and the
    client falls back to a tab if the frame never loads. Guessing False
    for everything would send every page to a tab and make the workspace
    pointless; guessing True for known refusers is what produces an empty
    box that looks like a broken JARVIS."""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    return not any(host == refuser or host.endswith("." + refuser)
                   for refuser in _FRAME_REFUSERS)


def normalize_url(text: str) -> Optional[str]:
    """A real URL out of what someone said or typed, or None.

    None is a real answer — "open the thing" is not a URL, and inventing
    one would send JARVIS somewhere nobody asked for."""
    if not text:
        return None
    match = _URL_RE.search(text)
    if match:
        return match.group(0).rstrip(".,;)")
    bare = _BARE_DOMAIN_RE.search(text.strip())
    if bare and "." in bare.group(1):
        return "https://" + bare.group(0).rstrip(".,;)")
    known = _KNOWN_SITE_NAMES.get(text.strip().lower())
    if known:
        return known
    return None


def _destination(destination_type: str, action: str, *, destination_id: str = "",
                 destination_route: str = "", external_url: str = "",
                 label: str = "", embeddable: Optional[bool] = None,
                 detail: str = "") -> dict[str, Any]:
    return {
        "destination_type": destination_type,
        "destination_id": destination_id,
        "destination_route": destination_route,
        "external_url": external_url,
        "action": action,
        "label": label,
        "embeddable": embeddable,
        "detail": detail,
    }


def resolve_control(action: str) -> Optional[dict[str, Any]]:
    if action not in CONTROL_ACTIONS:
        return None
    return _destination(TYPE_CONTROL, action, label=action)


def resolve_record(source: str, data: dict[str, Any]) -> Optional[dict[str, Any]]:
    """A business record's destination, via the existing
    notification_destinations builders — the allowlist and the
    no-id-no-link rule already live there and are not reimplemented."""
    built = dest.build(source, data or {})
    if built is None:
        return None
    if built.get("kind") == dest.EXTERNAL:
        url = built.get("url") or ""
        embeddable = is_embeddable(url)
        return _destination(
            TYPE_RECORD,
            ACTION_OPEN_WORKSPACE if embeddable else ACTION_OPEN_EXTERNAL_TAB,
            destination_id=str((data or {}).get("id") or ""),
            external_url=url, label=built.get("label") or source,
            embeddable=embeddable,
            detail=("" if embeddable else
                    f"{urlparse(url).hostname} refuses to be embedded — "
                    f"opening it in its own tab instead."))
    return _destination(TYPE_RECORD, ACTION_OPEN_RECORD,
                        destination_id=str((data or {}).get("id") or ""),
                        destination_route=built.get("path") or "",
                        label=built.get("label") or source)


def resolve_nucleus(target: str) -> Optional[dict[str, Any]]:
    """A Command Center area by spoken or clicked name. None when no such
    area exists — the caller says so rather than opening something near it."""
    from actions import nucleus_hierarchy

    if not target:
        return None
    node = nucleus_hierarchy.find_node_by_name(target)
    if node is None:
        node = nucleus_hierarchy.get_hierarchy_node(target)
    if node is None:
        return None
    return _destination(TYPE_NUCLEUS, ACTION_OPEN_NUCLEUS,
                        destination_id=node["id"],
                        destination_route=f"/3d#{node['id']}",
                        label=node.get("name") or node["id"])


def resolve(request: str = "", *, action: str = "", target: str = "",
            record_source: str = "", record_data: Optional[dict] = None
            ) -> Optional[dict[str, Any]]:
    """The one entry point. Voice passes what was said; a click passes an
    explicit action/target/record. Both get the same structured
    destination, so the two can never drift apart.

    Returns None when nothing resolves — which the caller must report as
    "I could not find that", never as a successful navigation."""
    if record_source:
        return resolve_record(record_source, record_data or {})

    explicit = (action or "").strip().lower()
    if explicit in CONTROL_ACTIONS:
        return resolve_control(explicit)

    text = (request or target or "").strip()

    # An explicit "open" with a target skips control-phrase matching, so
    # "open Home Improvement" cannot be mistaken for "go home".
    if explicit not in ("open", "open_nucleus"):
        for pattern, control in _CONTROL_PHRASES:
            if re.search(pattern, text, re.I) and not _URL_RE.search(text):
                # "go back to BuildPro" names a place; plain "go back" does not.
                remainder = re.sub(pattern, " ", text, flags=re.I)
                words = [w for w in re.findall(r"[\w']+", remainder.lower()) if w]
                if all(w in _FILLER for w in words):
                    return resolve_control(control)

    url = normalize_url(text)
    if url:
        embeddable = is_embeddable(url)
        host = urlparse(url).hostname or url
        return _destination(
            TYPE_EXTERNAL,
            ACTION_OPEN_WORKSPACE if embeddable else ACTION_OPEN_EXTERNAL_TAB,
            external_url=url, label=host, embeddable=embeddable,
            detail=("" if embeddable else
                    f"{host} refuses to be embedded — opening it in its own tab instead."))

    cleaned = _VERB_PREFIX.sub("", text).strip()
    cleaned = re.sub(r"\s+(please|now|for me)\s*$", "", cleaned, flags=re.I).strip()
    return resolve_nucleus(cleaned or text)


def describe(destination: Optional[dict[str, Any]], delivered: int) -> str:
    """What JARVIS should SAY about a navigation that has already been
    attempted. `delivered` is the number of Command Center windows that
    ACTUALLY received and processed it — the one and only signal this
    function is allowed to treat as success. Zero means no live view
    confirmed anything, and that is reported as a real failure, not
    smoothed into an "opening now"/"I set it to..." claim that assumes
    a later, unconfirmed client-side action will land. A tool result
    generated at this exact moment cannot know what a browser tab does
    after the response is sent — so it must never promise that it does.

    Never tells the user to go open a Command Center window themselves:
    JARVIS is the one that navigates, not the user."""
    if destination is None:
        return ("I couldn't find that destination — try a business or module name "
                "like BuildPro, Candidates or Daily Deal Finders, or give me a full web address.")

    label = destination.get("label") or "that"
    if destination["action"] == ACTION_OPEN_EXTERNAL_TAB:
        if not delivered:
            return f"I couldn't open {label} — no active Command Center view is connected right now."
        return (f"Opened {label} in a new tab — {destination.get('detail') or ''}".strip()
                or f"Opened {label} in a new tab.")
    if destination["destination_type"] == TYPE_CONTROL:
        verb = {ACTION_BACK: "Went back", ACTION_HOME: "Back at the command center home",
                ACTION_CLOSE: "Closed the workspace"}[destination["action"]]
        if not delivered:
            return f"There's no active Command Center view connected, so there was nothing to {destination['action']}."
        return f"{verb}."
    if not delivered:
        return f"I couldn't open {label} — no active Command Center view is connected right now."
    return f"Opened {label} in the command center."
