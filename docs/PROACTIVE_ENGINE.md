# Proactive Engine — Controls and Activity Trail

`actions/proactive.py`'s `ProactiveEngine` was already real and
well-designed — genuine silence/cooldown gating, a rotating context focus
so it doesn't repeat the same opener, and no hardcoded canned responses
(Gemini decides what to actually say). This doc covers the controls and
activity trail added on top of that.

## What it can never do

`_run_proactive_mode()` (`main.py`) only ever sends a text prompt into
the existing Gemini Live session via `session.send_client_content()` —
it never calls an email/SMS/social-posting function directly, never
touches an external system, and the prompt itself instructs Gemini not
to call any tools during a proactive turn. That instruction is a
prompt-level constraint, not a hard per-turn block — Gemini Live's tool
configuration is set once for the whole session, not toggled per
message, so there's no clean way to hard-disable tool-calling for just
one turn without a larger architectural change. The real backstop is
that every consequential tool in this codebase (Gmail send, Calendar
create, HubSpot/Airtable writes, Buffer publish, browser purchases)
already requires its own independent explicit-instruction/`approved`/
`confirmed` gate — so even a hypothetical tool call during a proactive
turn would still hit those gates and be refused without genuine user
authorization already present in the conversation.

## Controls

| Control | Where | Default |
|---|---|---|
| Enable/disable | `config/api_keys.json` → `proactive_enabled`, or say "turn off proactive check-ins" (voice tool `proactive_settings`, action `disable`/`enable`) | enabled (matches this product's existing behavior/identity — see note below) |
| Quiet hours | `config/api_keys.json` → `proactive_quiet_hours: [start_hour, end_hour]` (24h, wraps midnight), or `null` to disable | `[22, 8]` (10pm–8am) |
| Snooze | Say "stop checking in on me" / "give me some quiet time" (voice tool `proactive_settings`, action `snooze`, default 60 min) | none (session-only, resets on restart) |
| Rate limit | `ProactiveEngine(min_silence_secs=900, check_cooldown=1200)` — 15 min silence required, 20 min minimum gap between check-ins | unchanged, pre-existing |

**Why "enabled by default" rather than requiring opt-in**: this product's
whole identity (per `readme.md`, `core/prompt.txt`'s "act like Jarvis
from Iron Man") is built around a present, proactive assistant — flipping
proactive check-ins off by default would be a real behavior regression
for existing users, not a neutral privacy default. A real, working
enable/disable control now exists (satisfying the actual requirement —
the *ability* to opt in or out); the default preserves current behavior
rather than silently changing it.

Both `enabled` and `quiet_hours` are read fresh from config on every
60-second check in `_run_proactive_mode()` — a voice command to
disable/snooze takes effect on the very next check, not just after a
restart.

## Deduplication

Beyond the existing 3-way rotating focus, `build_prompt()` now
explicitly instructs Gemini: "Do NOT repeat a topic/theme already covered
in the recent conversation above" — the recent-conversation context
already fed into the prompt (`recent_turns`) includes JARVIS's own prior
proactive remarks, so this makes the existing soft mechanism explicit
rather than relying on Gemini incidentally noticing the repetition.

## Activity trail

Every `mark_triggered()` call now also records a row to
`data/jarvis2.db`'s `proactive_log` table (timestamp + which focus area
was used) via `actions/proactive.py`'s `_record_trigger()`/
`get_recent_triggers()`. Query it via the `proactive_settings` voice tool
(action `history`), or directly: `actions.proactive.get_recent_triggers(limit=20)`.
A logging failure never blocks a real check-in — `_record_trigger()`
degrades silently on its own error, same pattern as every other
best-effort local log in this codebase (e.g. `buffer_integration.py`'s
duplicate-post history).
