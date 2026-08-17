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
            "id": "personal",
            "name": "Personal",
            "kind": "domain",
            "children": [
                {"id": "notes", "name": "Notes", "kind": "category"},
                {"id": "reminders", "name": "Reminders", "kind": "category"},
            ],
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
