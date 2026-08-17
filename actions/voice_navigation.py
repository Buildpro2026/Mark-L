"""Voice → Nucleus command mapping for the 3D spatial command center.

Resolves free-text phrases (as extracted by Gemini's function calling in
main.py's `navigate_command_center` tool) to a canonical Nucleus id from the
existing hierarchy in actions/nucleus_hierarchy.py — that hierarchy is the
source of truth; nothing here invents Nuclei of its own.
"""
from __future__ import annotations

import re
from typing import Any

from actions.nucleus_hierarchy import get_hierarchy_root

# Shorthand/alias phrases that don't literally match a Nucleus name in the
# hierarchy (e.g. "DDF", "my email", "phone" for Communications).
_EXTRA_ALIASES: dict[str, str] = {
    "ddf": "ddf",
    "daily deals": "ddf",
    "deal finder": "ddf",
    "deal finders": "ddf",
    "careerrocket": "careerrocket",
    "career rocket": "careerrocket",
    "career rocket pro": "careerrocket",
    "my email": "email",
    "mail": "email",
    "my calendar": "calendar",
    "my files": "files",
    "comms": "communications",
    "phone": "communications",
    "sms": "communications",
    "texts": "communications",
    "messages": "communications",
    "notifications": "communications",
    "everything": "jarvis",
    "home": "jarvis",
    "center": "jarvis",
    "command center": "jarvis",
    "main menu": "jarvis",
    "core": "jarvis",
    "jarvis": "jarvis",
}

_STRIP_PREFIX_RE = re.compile(
    r"^(please\s+)?(jarvis[,]?\s+)?"
    r"(open|go to|go|show me|show|navigate to|pull up|switch to|take me to|open up)\s+",
    re.IGNORECASE,
)

_NAV_INTENT_PATTERNS: dict[str, "re.Pattern[str]"] = {
    "back": re.compile(r"\b(go\s+back|previous)\b", re.IGNORECASE),
    "home": re.compile(
        r"\b(go\s+home|show me everything|show everything|overview|main menu)\b",
        re.IGNORECASE,
    ),
    "status": re.compile(
        r"\b(what am i looking at|what am i seeing|where am i|what is this)\b",
        re.IGNORECASE,
    ),
}


def _normalize(text: str) -> str:
    text = (text or "").strip()
    text = _STRIP_PREFIX_RE.sub("", text)
    return text.strip(" ?.!").lower()


def resolve_nav_intent(text: str) -> str | None:
    """Detect a back/home/status intent in free text. Returns None if the
    text doesn't look like one of those (e.g. it's a Nucleus name instead)."""
    norm = (text or "").strip().lower()
    if not norm:
        return None
    # Bare single-word phrasing ("back", "home") — full-match only, since
    # these words are common substrings elsewhere ("home" is also an alias
    # for "jarvis" as an open target, disambiguated by the caller).
    if norm in ("back",):
        return "back"
    for action, pattern in _NAV_INTENT_PATTERNS.items():
        if pattern.search(norm):
            return action
    return None


def _iter_hierarchy_nodes(node: dict[str, Any]):
    yield node
    for child in node.get("children", []) or []:
        yield from _iter_hierarchy_nodes(child)


def resolve_nucleus_target(text: str) -> str | None:
    """Map free-text like 'BuildPro' / 'open my email' / 'DDF' to a
    canonical Nucleus id from the live hierarchy. Returns None if nothing
    recognizable was found."""
    norm = _normalize(text)
    if not norm:
        return None

    if norm in _EXTRA_ALIASES:
        return _EXTRA_ALIASES[norm]

    root = get_hierarchy_root()
    candidates: list[tuple[str, str]] = []
    if root:
        for node in _iter_hierarchy_nodes(root):
            node_id = node.get("id")
            name = (node.get("name") or "").strip().lower()
            if node_id and name:
                candidates.append((node_id, name))

    for node_id, name in candidates:
        if norm == name or norm == node_id:
            return node_id

    # Substring match, longest name first — avoids "email" matching before
    # a more specific multi-word name would.
    for node_id, name in sorted(candidates, key=lambda c: -len(c[1])):
        if name and (name in norm or norm in name):
            return node_id

    for alias, node_id in _EXTRA_ALIASES.items():
        if alias in norm:
            return node_id

    return None


_VALID_ACTIONS = {"open", "back", "home", "status"}


def parse_navigation_command(action: str | None, target: str | None) -> dict[str, Any]:
    """Normalize a (action, target) pair — as extracted by Gemini's
    function-calling, or a raw phrase in either slot — into a clean
    {"action", "nucleus_id", "error"} instruction.

    Handles two calling shapes because an LLM tool call isn't guaranteed to
    split intent this cleanly:
      1. action="open", target="BuildPro"          (clean)
      2. action="", target="go back"                (phrase in target)
      3. action="go back", target=""                (phrase in action slot)
    """
    action_norm = (action or "").strip().lower()
    target = (target or "").strip()

    if action_norm not in _VALID_ACTIONS:
        intent = resolve_nav_intent(target) or resolve_nav_intent(action_norm)
        action_norm = intent or ("open" if target else "status")

    if action_norm == "open":
        nucleus_id = resolve_nucleus_target(target)
        if not nucleus_id:
            return {
                "action": "open",
                "nucleus_id": None,
                "error": (
                    f"I don't recognize a Nucleus called '{target}'."
                    if target else "Which Nucleus should I open?"
                ),
            }
        return {"action": "open", "nucleus_id": nucleus_id, "error": None}

    return {"action": action_norm, "nucleus_id": None, "error": None}
