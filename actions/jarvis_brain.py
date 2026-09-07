"""One way in to the JARVIS Brain, for every surface that needs it.

The knowledge base under knowledge/JARVIS Brain/ was already readable —
core/headless/obsidian.py's ObsidianVault handles paths, safety and
targeted retrieval well, and config.OBSIDIAN_VAULT_PATH already defaults to
the in-repo copy, so headless deployments resolve it correctly with no
environment variable set. What was missing is that nothing on the
AUTONOMOUS path ever asked. The vault was wired to the Gemini tool registry
(a human in a conversation) and to nothing else, so the CEO cycle, the
agents and the business pipeline all made decisions without ever consulting
the operating mandate, the approval matrix, or the company profile sitting
in the repository beside them.

This is a thin accessor, deliberately:

  * It does NOT copy or re-parse the knowledge base, and there is no
    separate brain for voice, headless or desktop — one ObsidianVault
    instance backs every caller, so the Brain cannot drift between them.
  * It does NOT load everything on every request. ObsidianVault already has
    query-scoped retrieval (search_notes/format_for_prompt with a char
    budget); this uses it rather than dumping the whole vault into context.
  * A missing or malformed vault is a normal, expected state — every
    function degrades to "no knowledge available" and the caller keeps
    working. Knowledge is an input to a decision, never a prerequisite for
    running at all.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("jarvis.brain")

# Retrieval budget for an autonomous caller. Smaller than an interactive
# prompt's: a background cycle wants the few relevant paragraphs, not a
# briefing document, and every extra character is cost on a real LLM call.
AUTONOMOUS_CONTEXT_CHARS = 2500

_vault = None


def _get_vault():
    """One shared vault instance. Built lazily so importing this module
    never touches the filesystem, and cached so a per-agent lookup does not
    re-walk the vault directory each time."""
    global _vault
    if _vault is None:
        from core.headless.obsidian import ObsidianVault
        _vault = ObsidianVault()
    return _vault


def reset_cache() -> None:
    """Drops the cached vault. For tests, and for the case where the vault
    path changes at runtime."""
    global _vault
    _vault = None


def status() -> dict[str, Any]:
    """Whether the Brain is actually readable — structure only, no content."""
    try:
        vault = _get_vault()
        st = vault.status()
        return {
            "configured": bool(st.get("configured")),
            "available": bool(st.get("configured") and st.get("exists", True)),
            "detail": st.get("detail"),
        }
    except Exception as exc:
        logger.debug("brain status check failed", exc_info=True)
        return {"configured": False, "available": False, "detail": str(exc)}


def is_available() -> bool:
    return bool(status().get("available"))


def recall(query: str, max_chars: int = AUTONOMOUS_CONTEXT_CHARS) -> str:
    """Knowledge relevant to `query`, as text, or "" when there is none.

    Returns a plain string rather than raising so a caller can always do
    `context = recall(...)` and carry on — a missing Brain must never be the
    reason an autonomous cycle stops."""
    if not query or not query.strip():
        return ""
    try:
        vault = _get_vault()
        if not vault.is_configured():
            return ""
        hits = find_notes(query, limit=5)
        if not hits:
            return ""
        parts, used = [], 0
        for hit in hits:
            content = vault.read_note(hit["path"])
            if not content:
                continue
            block = f"## {hit['path']}\n{content.strip()}\n"
            if used + len(block) > max_chars:
                remaining = max_chars - used
                if remaining > 0:
                    parts.append(block[:remaining])
                break
            parts.append(block)
            used += len(block)
        return "\n".join(parts)
    except Exception:
        logger.debug("brain recall failed for %r", query, exc_info=True)
        return ""


# Words too common in this vault to narrow anything down. Dropping them
# keeps a multi-word query from matching on "jarvis" alone.
_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "what", "when",
    "how", "why", "are", "our", "his", "her", "its", "jarvis", "a", "an",
    "of", "to", "in", "on", "by", "is", "be",
}


def _terms(query: str) -> list[str]:
    import re
    return [w for w in re.findall(r"[a-z0-9]+", query.lower())
            if len(w) > 2 and w not in _STOPWORDS]


def find_notes(query: str, limit: int = 5) -> list[dict]:
    """Matching notes as structured hits (path + excerpt), most relevant
    first.

    ObsidianVault.search_notes is a whole-string substring match, which is
    right for the interactive tool path (a human types a phrase they expect
    to appear) but finds nothing for a descriptive query like "CEO operating
    mandate priorities" — no note contains that exact string. Rather than
    change matching for the existing callers, this tries the phrase first
    and then falls back to scoring notes by how many significant terms they
    match, so an autonomous caller can ask by topic instead of having to
    know the exact wording of a filename."""
    if not query or not query.strip():
        return []
    try:
        vault = _get_vault()
        if not vault.is_configured():
            return []

        exact = vault.search_notes(query, limit=limit) or []
        if exact:
            return exact

        terms = _terms(query)
        if not terms:
            return []
        scored: dict[str, dict] = {}
        for term in terms:
            for hit in (vault.search_notes(term, limit=50) or []):
                entry = scored.setdefault(hit["path"], {**hit, "score": 0})
                # A term in the filename is a much stronger signal than one
                # buried in the body of a long note.
                entry["score"] += 3 if hit.get("title_match") else 1
        ranked = sorted(scored.values(), key=lambda h: h["score"], reverse=True)
        return ranked[:limit]
    except Exception:
        logger.debug("brain search failed for %r", query, exc_info=True)
        return []


def read(relative_path: str) -> Optional[str]:
    """One note by path, or None. Path safety (no escaping the vault) is
    enforced by ObsidianVault._resolve_safe, not re-implemented here."""
    try:
        vault = _get_vault()
        if not vault.is_configured():
            return None
        return vault.read_note(relative_path)
    except Exception:
        logger.debug("brain read failed for %r", relative_path, exc_info=True)
        return None


# Topics the autonomous cycle consults by name. Keeping them here rather
# than as string literals scattered through the cycle means the Brain's
# vocabulary is reviewable in one place, and a renamed note is one edit.
OPERATING_TOPICS = {
    "mandate": "CEO operating mandate priorities",
    "approval": "decision authority approval matrix",
    "rhythm": "daily operating rhythm",
    "recruiting": "BuildPro recruiting operating system",
    "revenue": "revenue intelligence engine",
}


def operating_context(topics: Optional[list[str]] = None,
                      max_chars: int = AUTONOMOUS_CONTEXT_CHARS) -> dict[str, str]:
    """The Brain excerpts the CEO cycle wants, keyed by topic.

    Only topics that actually returned something appear in the result, so a
    caller can tell "the Brain had nothing on this" from "the Brain had
    this" without parsing empty strings."""
    wanted = topics if topics is not None else list(OPERATING_TOPICS)
    budget = max(200, max_chars // max(1, len(wanted)))
    out: dict[str, str] = {}
    for topic in wanted:
        query = OPERATING_TOPICS.get(topic, topic)
        text = recall(query, max_chars=budget)
        if text.strip():
            out[topic] = text
    return out
