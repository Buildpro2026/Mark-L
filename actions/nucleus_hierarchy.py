from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config" / "nucleus_config.json"

DEFAULT_HIERARCHY = {
    "id": "jarvis",
    "name": "Jarvis",
    "kind": "central",
    "children": [
        {
            # 2026-09-03 (Lee's autonomous-CEO spec, Section 18): the
            # Company Core Planet — every real infrastructure platform
            # JARVIS runs on, as its own top-level Nucleus. Star status is
            # computed live by dashboard/server.py's _module_company_core()
            # from the same _integration_health() check the System module
            # already uses — a disconnected star is labeled honestly, never
            # hidden or shown as healthy.
            "id": "company_core",
            "name": "Company Core",
            "kind": "domain",
            "children": [
                {"id": "star-render", "name": "Render (hosting)", "kind": "category"},
                {"id": "star-database", "name": "Database", "kind": "category"},
                {"id": "star-memory", "name": "Memory / Brain", "kind": "category"},
                {"id": "star-knowledge", "name": "JARVIS Brain (Obsidian)", "kind": "category"},
                {"id": "star-ollama", "name": "Ollama Cloud", "kind": "category"},
                {"id": "star-groq", "name": "Groq", "kind": "category"},
                {"id": "star-gemini", "name": "Gemini", "kind": "category"},
                {"id": "star-cartesia", "name": "Cartesia (voice)", "kind": "category"},
                {"id": "star-twilio", "name": "Twilio (SMS/calls)", "kind": "category"},
                {"id": "star-hubspot", "name": "HubSpot (CRM)", "kind": "category"},
                {"id": "star-gmail", "name": "Gmail", "kind": "category"},
                {"id": "star-calendar", "name": "Google Calendar", "kind": "category"},
                {"id": "star-buffer", "name": "Buffer (social)", "kind": "category"},
            ],
        },
        {
            "id": "buildpro",
            "name": "BuildPro",
            "kind": "domain",
            "children": [
                {"id": "clients", "name": "Clients", "kind": "category"},
                {"id": "candidates", "name": "Candidates", "kind": "category"},
                {"id": "jobs", "name": "Jobs", "kind": "category"},
                {"id": "prospects", "name": "Prospects", "kind": "category"},
                {"id": "matches", "name": "Matches", "kind": "category"},
            ],
        },
        {
            "id": "ddf",
            "name": "Daily Deal Finders",
            "kind": "domain",
            "children": [
                {"id": "products", "name": "Products", "kind": "category"},
                {"id": "ddf-drafts", "name": "Deal Drafts", "kind": "category"},
            ],
        },
        {
            "id": "careerrocket",
            "name": "CareerRocket Pro",
            "kind": "domain",
            "children": [
                {"id": "pipeline", "name": "Pipeline", "kind": "category"},
            ],
        },
        {
            "id": "email",
            "name": "Email",
            "kind": "domain",
            "children": [
                {"id": "inbox", "name": "Inbox", "kind": "category"},
                {"id": "drafts", "name": "Drafts", "kind": "category"},
            ],
        },
        {
            "id": "calendar",
            "name": "Calendar",
            "kind": "domain",
            "children": [
                {"id": "upcoming", "name": "Upcoming", "kind": "category"},
                {"id": "events", "name": "Events", "kind": "category"},
            ],
        },
        {
            # 2026-09-03 (Lee's autonomous-CEO spec, Section 9): a real
            # Personal Planet — every child here is either live data
            # (Email pulls real Gmail messages classified PERSONAL by
            # actions/email_classification.py, with real deep links) or an
            # honest "not connected yet" placeholder (Section 20's "real
            # data only, NO DATA if none" rule) — never a fabricated
            # personal task/document/alert. Calendar/Files/Communications
            # reuse the exact same live modules the top-level Calendar/
            # Files/Communications domains already use — same functions,
            # a second navigable path to them, not a second implementation.
            "id": "personal",
            "name": "Personal",
            "kind": "domain",
            # 2026-09-03: display names deliberately distinct from the
            # top-level Calendar/Files/Communications domains (never bare
            # "Calendar"/"Files"/"Communications") — find_node_by_name()
            # is a global, first-match lookup used by voice command
            # resolution (Section 15), so a duplicate display name would
            # silently make "open calendar" ambiguous. See
            # test_hierarchy_has_no_duplicate_ids_or_names.
            "children": [
                {"id": "personal-email", "name": "Personal Email", "kind": "category"},
                {"id": "personal-contacts", "name": "Personal Contacts", "kind": "category"},
                {"id": "personal-calendar", "name": "Personal Calendar", "kind": "category"},
                {"id": "personal-documents", "name": "Personal Documents", "kind": "category"},
                {"id": "personal-tasks", "name": "Personal Tasks", "kind": "category"},
                {"id": "personal-communications", "name": "Personal Communications", "kind": "category"},
                {"id": "personal-files", "name": "Personal Files", "kind": "category"},
                {"id": "personal-alerts", "name": "Personal Alerts", "kind": "category"},
            ],
        },
        {
            # Distinct from "files" below: this is specifically the Obsidian
            # knowledge vault (default knowledge/JARVIS Brain/, see
            # core/headless/obsidian.py), not a general filesystem search.
            # No static category children — dashboard/server.py's
            # _module_knowledge() lists the vault's real notes at request
            # time rather than hardcoding folder names here that could
            # drift from what the vault actually contains.
            "id": "knowledge",
            "name": "JARVIS Brain",
            "kind": "domain",
            "children": [],
        },
        {
            "id": "files",
            "name": "Files",
            "kind": "domain",
            "children": [
                {"id": "files-recent", "name": "Recent", "kind": "category"},
                {"id": "files-documents", "name": "Documents", "kind": "category"},
                {"id": "files-reports", "name": "Report Files", "kind": "category"},
                {"id": "files-projects", "name": "Projects", "kind": "category"},
            ],
        },
        {
            "id": "reports",
            "name": "Reports",
            "kind": "domain",
            "children": [
                {"id": "reports-morning-brief", "name": "Morning Brief", "kind": "category"},
                {"id": "reports-system-health", "name": "System Health", "kind": "category"},
                {"id": "reports-history", "name": "History", "kind": "category"},
            ],
        },
        {
            "id": "communications",
            "name": "Communications",
            "kind": "domain",
            "children": [
                # Twilio (actions/twilio_integration.py) is real, working code —
                # whether these render as live or as placeholders in the 3D UI is
                # computed dynamically at request time from actual configured
                # state (dashboard/server.py's _overlay_communications_placeholder),
                # not hardcoded here, so the UI stays accurate once credentials
                # are added without needing a code change.
                {"id": "comm-phone", "name": "Phone", "kind": "category"},
                {"id": "comm-sms", "name": "SMS", "kind": "category"},
                {"id": "comm-calls", "name": "Calls", "kind": "category"},
                {"id": "comm-contacts", "name": "Contacts", "kind": "category"},
                {"id": "comm-notifications", "name": "Notifications", "kind": "category"},
            ],
        },
        {
            "id": "system",
            "name": "System",
            "kind": "domain",
            "children": [
                {"id": "health", "name": "Health", "kind": "category"},
                {"id": "system-files", "name": "System Files", "kind": "category"},
            ],
        },
        {
            "id": "hubspot",
            "name": "HubSpot",
            "kind": "domain",
            "children": [
                {"id": "hubspot-contacts", "name": "HubSpot Contacts", "kind": "category"},
                {"id": "hubspot-companies", "name": "HubSpot Companies", "kind": "category"},
            ],
        },
        {
            "id": "social",
            "name": "Buffer / Social",
            "kind": "domain",
            "children": [
                {"id": "social-channels", "name": "Channels", "kind": "category"},
            ],
        },
    ],
}


def _ensure_config() -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_HIERARCHY, indent=2), encoding="utf-8")


def load_hierarchy() -> dict[str, Any]:
    _ensure_config()
    try:
        loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return loaded or DEFAULT_HIERARCHY
    except Exception:
        return DEFAULT_HIERARCHY


def _find_node(nodes: list[dict[str, Any]], target_id: str) -> dict[str, Any] | None:
    for node in nodes:
        if node.get("id") == target_id:
            return node
        child = _find_node(node.get("children", []), target_id)
        if child is not None:
            return child
    return None


def get_hierarchy_node(node_id: str) -> dict[str, Any] | None:
    if not node_id:
        return None
    root = load_hierarchy()
    return _find_node([root], node_id)


def get_hierarchy_children(node_id: str) -> list[dict[str, Any]]:
    node = get_hierarchy_node(node_id)
    return list(node.get("children", []) if node else [])


def get_hierarchy_path(node_id: str) -> list[dict[str, Any]]:
    if not node_id:
        return []
    root = load_hierarchy()
    stack: list[tuple[dict[str, Any], list[dict[str, Any]]]] = [(root, [root])]
    while stack:
        current, path = stack.pop()
        if current.get("id") == node_id:
            return path
        for child in reversed(current.get("children", [])):
            stack.append((child, path + [child]))
    return []


def get_hierarchy_root() -> dict[str, Any]:
    return load_hierarchy()


def find_node_by_name(target: str) -> dict[str, Any] | None:
    """Case-insensitive lookup by display name OR id — e.g. 'BuildPro'
    matches name='BuildPro', and 'DDF' matches id='ddf' even though that
    node's display name is 'Daily Deal Finders'. Used by the
    navigate_command_center voice tool so a person can say either the
    friendly name or the short id and reach the same Nucleus."""
    target_norm = (target or "").strip().lower()
    if not target_norm:
        return None
    stack = [load_hierarchy()]
    while stack:
        node = stack.pop()
        if node.get("name", "").strip().lower() == target_norm or node.get("id", "").strip().lower() == target_norm:
            return node
        stack.extend(node.get("children", []))
    return None
