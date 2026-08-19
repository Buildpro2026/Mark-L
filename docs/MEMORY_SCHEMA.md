# Memory & Configuration Schema

## Where each kind of state lives (read this before adding a new store)

JARVIS has five separate layers of state on purpose — the risk with a
system like this is quietly growing a second place that answers the same
question a different way. Before adding a new file, table, or store, check
whether one of these five already owns that kind of fact:

| Layer | What it holds | Where |
|---|---|---|
| Runtime/session memory | The current conversation only — never written to disk | In-process (`JarvisLive._session_log`, chat `history` lists) |
| Long-term personal memory | Facts Lee has told JARVIS directly (identity, preferences, projects, relationships, wishes, notes) | `memory/long_term.json` via `memory/memory_manager.py` |
| Operational databases | Structured business data JARVIS generates or syncs itself: agent tasks/events, BuildPro candidates/clients/jobs/matches, DDF products/posts, business-intelligence entries, opportunities, audit log | `data/jarvis2.db` (SQLite) |
| External business systems | The source of truth for anything that also lives in a real external tool — Gmail, Google Calendar, HubSpot, Airtable, Buffer, Twilio | Those services themselves; JARVIS reads/writes through their APIs and never re-stores a shadow copy as its own truth |
| JARVIS personalization | How JARVIS presents itself and behaves — business identity, theme, animation, voice volume, interface layout, alert sensitivity. Not a fact about Lee (that's long-term memory) and not a credential (that's api_keys.json) | `config/preferences.json` via `memory/preferences_manager.py`, merged with the pre-existing voice/briefing settings (still in `api_keys.json`) by `core/headless/personalization.py` — see that module's docstring for exactly which file owns which field |
| Founder/company knowledge (future) | Goals, priorities, SOPs, prior decisions, research notes — anything that belongs in Lee's own written knowledge base | An Obsidian vault, via `core/headless/obsidian.py` |

The Obsidian layer (`core/headless/obsidian.py`) is a working, tested-by-hand
filesystem interface — path-traversal-safe reads, and approval-gated writes
that refuse to silently overwrite an existing note — but it is **not yet
wired to any LLM tool**. That's deliberate: it's the seam a future
retrieval/knowledge layer plugs into, not a replacement for
`memory/long_term.json` (which stays the place for facts Lee states
directly in conversation) or `data/jarvis2.db` (which stays the place for
structured records JARVIS itself generates). Wiring it up as a tool is a
product decision — whether Lee wants JARVIS reading/writing his real vault
— not a technical one; the groundwork is ready when that decision is made.

## Personal memory vs. credentials

Two separate persistent JSON files, deliberately never mixed:

| File | Holds | Git status |
|---|---|---|
| `memory/long_term.json` | Personal facts the user shared (name, preferences, projects, ...) | **Should never be committed** — real personal data, not source code (see below) |
| `config/api_keys.json` | API keys / credentials / app settings | Git-ignored (`.gitignore`) |
| `config/google/token.json`, `config/google/client_secret_*.json` | Google OAuth (Gmail/Calendar) | Git-ignored |

Neither file should ever contain data that belongs in the other — no
credentials in `long_term.json`, no personal facts in `api_keys.json`.

## `memory/long_term.json`

```jsonc
{
  "identity":      { "<key>": { "value": "...", "updated": "YYYY-MM-DD" } },
  "preferences":   { "<key>": { "value": "...", "updated": "YYYY-MM-DD" } },
  "projects":      { "<key>": { "value": "...", "updated": "YYYY-MM-DD" } },
  "relationships": { "<key>": { "value": "...", "updated": "YYYY-MM-DD" } },
  "wishes":        { "<key>": { "value": "...", "updated": "YYYY-MM-DD" } },
  "notes":         { "<key>": { "value": "...", "updated": "YYYY-MM-DD" } },
  "sessions": [ { "date": "YYYY-MM-DD", "summary": "...", "language": "en" } ]
}
```

- The six top-level categories always exist (`memory_manager._empty_memory()`
  is the canonical schema / safe default); a file missing one gets it
  backfilled to `{}` on load, never dropped.
- Every entry value is truncated to 380 chars (`MAX_VALUE_LENGTH`) on write.
- The whole file is capped at ~2200 chars (`MEMORY_MAX_CHARS`) — oldest-updated
  entries are silently trimmed first if a save would exceed it.
- `sessions` is optional, capped at the 3 most recent entries
  (`_SESSION_MAX`), consumed one at a time via `pop_last_session()` for the
  morning-briefing flow.

## `config/api_keys.json`

```jsonc
{
  "gemini_api_key": "",
  "hubspot_token": "",
  "buffer_token": "",
  "twilio": { "account_sid": "", "auth_token": "", "from_number": "" },
  "assistant_name": "J.A.R.V.I.S.",
  "user_name": "Mr. Chandler",
  "ui_color": "#00beff",
  "morning_brief_enabled": true
}
```

This is `memory/config_manager.DEFAULT_CONFIG` — the only keys any real
integration in this codebase actually reads. It previously also included
`github_token`, `vercel_token`, `make_api_token`, `airtable_token`,
`google_credentials`, `microsoft_credentials` — none of those are read
anywhere in the codebase (confirmed by a repo-wide search); they were dead
scaffolding that misleadingly implied those integrations existed, and have
been removed. Gmail/Calendar auth is a **separate** mechanism
(`config/google/`, see `actions/google_auth.py`) and intentionally has no
key here.

Extra keys not in this schema (e.g. `os_system`) are preserved as-is by
`save_config()` — the merge is additive, never destructive.

## Corruption recovery

Both `memory_manager.py` and `config_manager.py` follow the same pattern on
load:

1. If the file doesn't parse as JSON, or doesn't parse to a `dict`, the
   **original file is renamed** to `<name>.corrupt-<unix-timestamp>.json`
   in the same directory (never deleted) and a fresh default is used in
   memory for that call.
2. This means a corrupted file is never silently discarded — a human can
   always recover it from the `.corrupt-*.json` backup. This matters most
   for `api_keys.json`: without it, a corrupted read followed by any save
   would have silently overwritten real API keys with blanks.

## Atomic writes

Both files are written via a temp file in the same directory
(`<name>.tmp-<pid>`) followed by `os.replace()` onto the real path.
`os.replace` is atomic on both Windows and POSIX for a same-filesystem
rename, so a crash or power loss mid-write leaves the previous, intact file
in place rather than a truncated/corrupted one. No `.tmp-*` file should
ever be left behind after a normal write completes.

## Concurrency

Both modules serialize every read-modify-write cycle (load → mutate → save)
under a single `threading.Lock` acquisition, so two callers in the same
process can't interleave and silently lose one write (this was a real bug:
`update_memory()` and `save_config()` previously acquired and released the
lock separately for the read vs. the write, leaving a window in between).
This protects against races **within one running process** only — it is
not a cross-process file lock. Two separate JARVIS processes writing to the
same file concurrently is not a supported configuration.

## Settings changes reaching the UI

`ui.py` never reads `config/api_keys.json` directly for values that change
at runtime (voice settings, assistant name, UI color) — every settings
overlay writes through `config_manager.save_*()` / `voice_manager.py`, and
`main.py`'s reconnect loop re-reads the config fresh on every session
(re-loading voice provider, assistant name, and memory each time
`_build_config()` runs), so a settings change takes effect on the next
reconnect without requiring a full app restart. Voice-provider changes
additionally flag `_voice_reload_pending`, which forces an immediate
reconnect (`_watch_voice_reload` in `main.py`) rather than waiting for the
next natural one.
