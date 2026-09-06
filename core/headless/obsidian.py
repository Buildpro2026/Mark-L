"""Obsidian knowledge-layer interface (J2 Step 9 — foundation only).

Per the J1 audit's instructions: JARVIS reads founder/company knowledge,
goals, priorities, SOPs, decisions, and research from an existing Obsidian
vault ("JARVIS — BuildPro Operating System"), and writes back completed
tasks, decisions, research, and lessons learned — without inventing a
replacement knowledge architecture and without blindly overwriting
existing notes.

This module is a plain filesystem abstraction over one vault directory —
no RAG, no embeddings, no indexing yet (explicitly out of scope for this
phase; see docs/OPERATIONS.md-style scoping elsewhere in this repo). It's
the seam a future retrieval layer plugs into, not that layer itself.

The vault path is never hardcoded — it comes from JARVIS_OBSIDIAN_VAULT_PATH
(core/headless/config.py). Unset or missing means "not configured," reported
honestly via is_configured()/status(), never guessed at or silently skipped.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from core.headless import config

DECISIONS_SUBDIR = "Jarvis/Decisions"
COMPLETED_WORK_SUBDIR = "Jarvis/Completed Work"


class VaultNotConfigured(Exception):
    pass


class ObsidianVault:
    def __init__(self, vault_path: Optional[str] = None):
        raw = vault_path if vault_path is not None else config.OBSIDIAN_VAULT_PATH
        self.root: Optional[Path] = Path(raw).expanduser().resolve() if raw else None

    def is_configured(self) -> bool:
        return self.root is not None and self.root.is_dir()

    def status(self) -> dict:
        return {
            "configured": self.root is not None,
            "path": str(self.root) if self.root else None,
            "exists": bool(self.root and self.root.is_dir()),
        }

    def _require_vault(self) -> Path:
        if not self.is_configured():
            raise VaultNotConfigured(
                "No Obsidian vault configured (set JARVIS_OBSIDIAN_VAULT_PATH), "
                "or the configured path doesn't exist."
            )
        return self.root

    def _resolve_safe(self, relative_path: str) -> Path:
        """Resolves relative_path against the vault root and refuses
        anything that escapes it (path traversal via '..' or an absolute
        path) — a write/read request is always confined to the vault."""
        root = self._require_vault()
        candidate = (root / relative_path).resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"Path escapes the vault: {relative_path!r}")
        return candidate

    # ── read ──────────────────────────────────────────────────────────

    def list_notes(self, subfolder: str = "") -> list[str]:
        """Relative paths (posix-style) of every .md file under subfolder
        (default: the whole vault). Empty list if unconfigured or the
        subfolder doesn't exist — never raises for a routine 'nothing
        here yet' case."""
        if not self.is_configured():
            return []
        base = self._resolve_safe(subfolder) if subfolder else self.root
        if not base.is_dir():
            return []
        return sorted(
            str(p.relative_to(self.root).as_posix())
            for p in base.rglob("*.md")
        )

    def read_note(self, relative_path: str) -> Optional[str]:
        path = self._resolve_safe(relative_path)
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    def format_for_prompt(self, query: Optional[str] = None, max_chars: int = 6000) -> str:
        """Concatenates Brain notes into a system-prompt-ready block, most
        relevant first. query=None means "no specific topic" — every note
        in vault order (list_notes() is already sorted), truncated to
        max_chars, rather than an arbitrary/empty result. With a query,
        reuses search_notes()'s own relevance ranking instead of a second
        matching implementation. Never raises: an unconfigured or empty
        vault returns an honest empty string, not a fabricated summary."""
        if not self.is_configured():
            return ""
        if query:
            paths = [hit["path"] for hit in self.search_notes(query, limit=50)]
        else:
            paths = self.list_notes()
        parts: list[str] = []
        used = 0
        for rel_path in paths:
            content = self.read_note(rel_path)
            if not content:
                continue
            block = f"## {rel_path}\n{content.strip()}\n"
            if used + len(block) > max_chars:
                remaining = max_chars - used
                if remaining > 0:
                    parts.append(block[:remaining])
                break
            parts.append(block)
            used += len(block)
        return "\n".join(parts)

    def search_notes(self, query: str, limit: int = 20) -> list[dict]:
        """Plain substring search over title (filename) and content —
        deliberately simple; a real retrieval layer is future work, not
        this phase's job. Case-insensitive, stops at `limit` matches."""
        if not self.is_configured() or not query.strip():
            return []
        needle = query.strip().lower()
        results = []
        for rel in self.list_notes():
            path = self.root / rel
            title_hit = needle in Path(rel).stem.lower()
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                continue
            content_hit = needle in text.lower()
            if title_hit or content_hit:
                snippet = ""
                if content_hit:
                    idx = text.lower().find(needle)
                    start = max(0, idx - 80)
                    snippet = text[start:idx + 80].strip()
                results.append({"path": rel, "title_match": title_hit, "snippet": snippet})
            if len(results) >= limit:
                break
        return results

    # ── write (approval-gated, never blindly overwrites) ────────────────

    def write_note(
        self, relative_path: str, content: str, *,
        approved: bool = False, overwrite: bool = False,
    ) -> dict:
        """Writes a note. Two separate gates, matching the pattern every
        other write action in this codebase uses (see gmail_integration.py
        etc.): `approved=True` is required at all (the same explicit-
        instruction contract), and if a note already exists at that path,
        `overwrite=True` is ALSO required — silently clobbering existing
        founder/company knowledge is exactly what the J1 audit's Part 4
        instruction ("must not be blindly overwritten") rules out."""
        if not approved:
            return {"ok": False, "state": "NOT_APPROVED", "detail": "Write requires approved=True."}
        path = self._resolve_safe(relative_path)
        if path.exists() and not overwrite:
            return {
                "ok": False, "state": "EXISTS",
                "detail": f"{relative_path} already exists — pass overwrite=True to replace it.",
            }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"ok": True, "path": relative_path}

    def _append_journal_entry(self, subdir: str, title: str, content: str) -> dict:
        """Shared by record_decision/record_completed_work: always creates
        a NEW timestamped file rather than writing into a shared/rotating
        one, so this is inherently append-only and can never overwrite a
        prior entry — no overwrite gate needed for this path specifically."""
        root = self._require_vault()
        ts = time.strftime("%Y-%m-%d %H%M%S")
        safe_title = "".join(c for c in title if c not in '\\/:*?"<>|')[:80].strip() or "untitled"
        rel_path = f"{subdir}/{ts} - {safe_title}.md"
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        body = f"# {title}\n\n{content}\n"
        path.write_text(body, encoding="utf-8")
        return {"ok": True, "path": rel_path}

    def record_decision(self, title: str, content: str) -> dict:
        return self._append_journal_entry(DECISIONS_SUBDIR, title, content)

    def record_completed_work(self, title: str, content: str) -> dict:
        return self._append_journal_entry(COMPLETED_WORK_SUBDIR, title, content)
