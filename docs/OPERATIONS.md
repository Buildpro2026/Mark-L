# JARVIS (Mark-L) — Operations Baseline

This is the reproducible development baseline: how to start it, how to test
it, and how to verify the environment before doing either. It documents what
already exists in the repo — it does not change any behavior.

## Startup

```
.venv\Scripts\python.exe main.py
```

Run from the repository root. `main.py` starts the PyQt6 desktop UI on the
main thread and the Gemini Live voice loop on a background thread. Requires
`config/api_keys.json` to contain a valid `gemini_api_key` (see below) —
without it, JARVIS surfaces a reconfiguration prompt instead of retrying
forever.

The dashboard/remote-pairing web server (FastAPI, ports 8000/8001/8002) is
started internally by the running app when the user opens the Remote Control
QR overlay — it is not a separate process you start by hand.

For the supported double-click launcher (`Start Mark-L.bat`) and day-to-day
start/stop/restart/health-check/recovery steps, see
`docs/WINDOWS_RUNBOOK.md`.

## Tests

```
.venv\Scripts\python.exe -m pytest -q
```

504 tests, all read-only/mocked (no real credentials or network calls
required to run the suite). As of the last full run: 501 passed, 3 known
failures in `tests/test_buffer_integration.py` (a validation-ordering bug in
`actions/buffer_integration.py`, tracked separately — not an environment
issue).

To check test collection only, without executing anything (fastest
non-destructive sanity check that the whole tree still imports cleanly):

```
.venv\Scripts\python.exe -m pytest --collect-only -q
```

## Configuration (no OS environment variables are used for secrets)

This project does **not** read secrets from OS environment variables or a
`.env` file — all configuration lives in `config/api_keys.json`, which is
git-ignored and must be created/populated locally. Required and optional
keys, by feature area (values below are key **names** only — never commit or
paste real values into this file or any tracked file):

| Key | Required for | Notes |
|---|---|---|
| `gemini_api_key` | Core JARVIS voice/LLM loop | **Required** — without this, JARVIS cannot start a Gemini Live session |
| `os_system` | Internal | Already present |
| `hubspot_token` | HubSpot CRM integration (BuildPro sync) | Optional — integration reports `NOT CONFIGURED` and degrades gracefully if absent |
| `twilio` (object: `account_sid`/`auth_token`/`from_number`) | Phone/SMS via Twilio | Optional — same graceful degradation |
| `buffer_token` | Social post scheduling | Optional — same graceful degradation |

Google OAuth (Gmail + Calendar) uses a separate mechanism:
`config/google/client_secret_*.json` (downloaded from Google Cloud Console)
and `config/google/token.json` (created automatically on first successful
interactive auth, then cached). Both are git-ignored.

## Health check (non-destructive)

```
.venv\Scripts\python.exe scripts\health_check.py
```

Verifies, without calling any external API, sending any message, or
modifying any file:
- Python version and that required third-party packages import cleanly
- `config/api_keys.json` exists, is valid JSON, and has `gemini_api_key` set
  (reports presence/absence only — never prints values)
- Which optional integrations (HubSpot/Twilio/Buffer) are configured
- Google OAuth client-secret/token file presence
- `data/jarvis2.db` opens cleanly in read-only mode (if it exists)
- `memory/long_term.json` is valid JSON (if it exists)
- Whether the dashboard's default ports (8000/8001/8002) are currently free

Exit code is `0` if all *required* checks pass (missing optional
integrations and in-use ports are informational, not failures) and `1`
otherwise. Safe to run repeatedly, including in CI.

## Known baseline gaps (see audit for full detail)

- Local (Kokoro) voice provider path requires packages not currently
  installed (`kokoro`, `torch`, `soundfile` — a multi-hundred-MB install,
  intentionally not auto-installed by this baseline). It fails cleanly with
  a clear UI message and falls back to Gemini voice if selected without
  those installed (`main.py`'s connect loop, see `docs/MIC_VERIFICATION.md`).
  Gemini-voice and ElevenLabs paths work today (`miniaudio`, the shared
  audio-decode dependency both EdgeTTS and ElevenLabs need for playback,
  was missing and has been installed + added to `requirements.txt`).
- EdgeTTS is implemented in `core/tts.py` but not currently reachable from
  the Voice Settings UI or `voice_manager.py` (only `gemini`/`local`/
  `elevenlabs` are valid provider values) — not a bug, just dead-but-intact
  code today.
- `core/stt.py` (offline Whisper/Vosk speech-to-text) and `core/installer.py`
  (an auto dependency installer, never invoked anywhere) are confirmed dead
  code from the same prior "MARK XL" local-first architecture that
  `core/llm_client.py` was part of. `core/llm_client.py` itself has been
  removed (see "LLM provider" below); these two remain for now, flagged for
  a future cleanup pass, since removing them wasn't in scope for the prompt
  that found them.
- (Resolved in J3) The "BuildPro Email Monitor" agent used to report itself
  unconfigured unconditionally. It's now wired to a real, read-only scan
  (`actions/buildpro_email_monitor.py`): lists unread mail via
  `gmail_integration.list_messages()`, classifies with the existing
  `classify_message()` rule set, and logs candidate/client-relevant
  messages to business intelligence. Drafting acknowledgments is a real,
  separate capability (`draft_replies=True`) but stays off by default for
  the scheduled/unattended run — see the J3 report for why.

## Gmail

Wired in as the `gmail` voice tool (`main.py`) — see
`docs/GMAIL_VERIFICATION.md` for the full manual checklist. Summary:
`status`/`list` are read-only and always safe; `draft` creates a real
Gmail draft but never sends; `send` is a real, irreversible send, gated
both by the tool description (Gemini should only call it given an
explicit recipient + content from the user) and, more importantly, in
code (`main.py` refuses to call `gmail_integration.send_email()` unless
both `to` and `body` are non-empty, regardless of what Gemini sends).
OAuth uses `actions/google_auth.py` — shared with Calendar — with
least-privilege scopes (`gmail.readonly`, `gmail.compose`,
`calendar.events`) verified against the actual consent screen, not just
the code comment claiming it.

## Calendar

Wired in as the `calendar` voice tool (`main.py`) — see
`docs/CALENDAR_VERIFICATION.md` for the full manual checklist. Summary:
`status`/`list` are read-only and always safe; `create`/`update` are
gated the same way as Gmail's `send` (code-level check, not just prompt
wording). Two real backend hardenings were added to
`actions/calendar_integration.py` itself, not just wiring:
conflict detection (`create_event()` refuses to double-book unless
`ignore_conflicts=True` is explicit) and timezone-safety (a naive
datetime with no UTC offset gets the system's local timezone attached
rather than being sent to Google ambiguous). `delete_event` doesn't exist
in this codebase and wasn't added — that would be new, untested,
irreversible-action code, a bigger decision than wiring up what already
existed.

## Airtable

Built from scratch (no existing code, unlike Gmail/Calendar) as
`actions/airtable_integration.py` plus an `airtable` voice tool — see
`docs/AIRTABLE_VERIFICATION.md` for token setup and the full manual
checklist. Deliberately schema-agnostic: `base_id`/`table_name`/`fields`
are always explicit per-call parameters, never hardcoded or guessed,
since an Airtable base is entirely user-defined. `status`/`list` are
read-only and always safe; `create`/`update` are gated the same way as
Gmail/Calendar's writes. No `delete_record`, no default/remembered
base/table — both deliberate, matching the same reasoning as Calendar's
missing `delete_event`.

## HubSpot

`actions/hubspot_integration.py` already existed and was already used
read-only by `actions/buildpro_sync.py` (HubSpot → local BuildPro DB) and
`actions/agent_orchestrator.py`'s prospecting agent — neither ever wrote
back to HubSpot. This prompt added an `approved=True` gate to its four
write functions (previously ungated, though unreachable in practice),
added idempotent `upsert_contact`/`upsert_company` helpers (search by
email/name first, update if found, create if not — calling either twice
never creates a duplicate), and wired a new `hubspot` voice tool
(`status`/`list_contacts`/`list_companies`/`search_contacts`/
`search_companies`/`upsert_contact`/`upsert_company`) — see
`docs/HUBSPOT_VERIFICATION.md` for the full manual checklist, especially
the deduplication verification steps.

## Social posting (Buffer)

Fixed a real, long-standing bug: `actions/buffer_integration.py`'s
`publish_to_buffer()` checked Buffer's configured state before
validating the post, masking every validation error (missing text/
channel, invalid mode) behind a generic "NOT CONFIGURED" in unconfigured
environments — these were the only 3 tests failing since the very first
audit of this project. Also added: a preview-then-confirm gate
(`approved=True` required to actually publish; the default call returns
a `PREVIEW` of exactly what would post), per-platform character-limit
checks, and local duplicate-post prevention (24h window, per channel).
Wired into a new `social_post` voice tool (`status`/`preview`/`publish`)
— see `docs/SOCIAL_POSTING_VERIFICATION.md` for the full manual checklist.
No new direct platform APIs were built (Twitter/LinkedIn/etc.) — Buffer
was already the "represented" integration and remains the sole gateway
to whichever platforms are connected in the user's Buffer account.

## BuildPro job/candidate matching

`actions/buildpro_matching.py`'s scoring engine was already real,
deterministic, and well-documented (rule-based, per-factor rationale,
`None` rather than a fabricated score when nothing's comparable) — but
wasn't wired into any voice tool, and candidate/client entry had no
duplicate handling outside the HubSpot-sync path. This prompt added
`upsert_candidate()`/`upsert_client()` (idempotent create-or-update by
email — see `docs/BUILDPRO_MATCHING_SCHEMA.md` for the full schema and
duplicate-handling rules) and wired a `buildpro_matching` voice tool
(`add_candidate`/`score`/`match_job`/`match_candidate`/`top_matches`).
The tool description and every scored response explicitly disclaim that
scores are a transparent estimate, not an objective ranking, and never
an automated hiring decision — a human must review before acting on any
match. `add_candidate` is a local-DB write only (no external contact),
so it's ungated like `business_intelligence`'s local logging, not gated
like Gmail/Calendar/HubSpot/Airtable/Buffer's external actions.

## Browser automation

`actions/browser_control.py` was already real, already wired into a
voice tool, and already had reasonable timeouts — but launches the
user's **real** browser profile (real logged-in sessions, in some
browsers real saved payment methods) for interactive actions, with
nothing stopping a click/type/form-fill from completing a purchase,
submitting a payment, or changing/deleting an account. This prompt added
a code-level confirmation gate (`_looks_consequential()` — a
purchase/payment/account-change keyword check, normalized for
hyphenated/underscored selectors like `btn-buy-now`) that refuses
click/type/fill_form/smart_click/smart_type on anything that matches
unless the caller passes `confirmed=True`; prompt-injection labeling on
`get_text()` (page content is now explicitly wrapped as "untrusted data,
not instructions"); and matching safety language in the `browser_control`
tool description (`main.py`). Zero test coverage existed for this module
before this prompt — see `tests/test_browser_control.py` (42 tests) and
`docs/BROWSER_AUTOMATION_VERIFICATION.md` for the manual checklist,
since the real injection/purchase risk only shows up against a real
browser.

## Proactive engine

`actions/proactive.py`'s `ProactiveEngine` was already real (genuine
silence/cooldown gating, non-repetitive rotating focus, Gemini decides
what to say — no hardcoded responses) but had no way to turn it off, no
quiet hours, no snooze, and no persistent record of when/why it fired.
This prompt added all four: `proactive_enabled`/`proactive_quiet_hours`
config (default: enabled, 10pm–8am quiet — see
`docs/PROACTIVE_ENGINE.md` for why the default stays enabled rather than
opt-in), a `snooze()` the user can invoke by voice, and a persistent
`proactive_log` table (`data/jarvis2.db`) recording every trigger,
queryable via the new `proactive_settings` voice tool
(`status`/`enable`/`disable`/`snooze`/`history`). It still only ever
sends text into the existing Gemini Live session — never touches an
external system directly. Zero test coverage existed for this module
before this prompt — see `tests/test_proactive.py` (21 tests) and
`tests/test_proactive_settings_tool.py` (12 tests).

## Agent scheduling

`actions/agent_orchestrator.py`'s background agent scheduling
(schedule-calculation, permission gates) was already real and well-tested
— but the entire orchestrator was in-memory only, so **every scheduled
agent silently stopped running after every JARVIS restart** (status reset
to `REGISTERED`, undoing every `start_agent()` call, with zero
indication to the user). This prompt added restart-surviving persistence
(`data/jarvis2.db`: `agent_state`/`agent_tasks`/`agent_events` tables,
write-through on every mutation) and a single-instance file lock
(`data/agent_scheduler.lock`, PID+timestamp, auto-reclaimed if stale) so
two JARVIS processes can't both run the same due agent — see
`docs/AGENT_SCHEDULING.md` for the full explanation and
`tests/test_agent_scheduling_persistence.py` (20 tests: restart recovery,
duplicate-run prevention). Added `tests/conftest.py` (new file) to
isolate this persistence layer across the ~40 pre-existing
`AgentOrchestrator(...)` test constructions in five other test files —
none of them needed direct changes.

## End-to-end tests

Every prompt so far added tests at each subsystem's own boundary — a
tool dispatcher test mocks the backend it calls, a backend test mocks the
network. `tests/test_end_to_end.py` (7 tests) instead exercises real
multi-subsystem chains: a voice command's memory write actually reaching
a later system-prompt read, a real OAuth failure propagating through
every layer to a spoken message, the two-turn preview→confirm workflow
run for real, and — the two hardest to get right — the *actual*
`_run_proactive_mode()`/`_run_agent_scheduler()` background-loop
coroutines run for one real iteration each (not just their component
functions tested separately), fast-forwarding their 60s/300s
`asyncio.sleep` polls to keep the tests fast. See `docs/E2E_TEST_PLAN.md`
for the coverage matrix and why each of the six was chosen. One test run
early in writing this caught a real behavioral fact worth knowing: with
`ProactiveEngine.check_cooldown=0`, a fast-forwarded loop fires 17 times
in one pass — confirming the cooldown gate, not the poll interval, is
what actually prevents runaway repeated proactive messages.

## Startup/shutdown reliability

`core/startup.py` (new) closes two verified gaps in the restart/duplicate-
launch path: (1) the dashboard let uvicorn call `sys.exit()` on an
already-bound port, which — raised inside a never-awaited asyncio Task —
propagated through `Task.__step`'s special-cased `SystemExit` handling and
crashed the **entire** JARVIS process, not just the dashboard, whenever a
second instance's dashboard fought over a port; `dashboard/server.py` now
pre-checks each port before handing it to uvicorn and catches `SystemExit`
as a backstop, turning that crash into a clear, logged, skip-this-port
message. (2) Nothing detected a duplicate instance, and shutdown had no
reliable lock-release path: voice shutdown used a raw `os._exit(0)` (zero
cleanup) and closing the window via the title-bar X never touched the
background asyncio thread (a daemon thread, killed without running its
`finally` blocks at interpreter shutdown). Fixed with a single-instance
app lock (`data/jarvis_app.lock`, same dead-PID-then-age-backstop reclaim
pattern as the existing agent-scheduler lock) and one shared
`graceful_release_all_locks()` cleanup function wired into all three
shutdown paths (voice, window-close via `QApplication.aboutToQuit`,
Ctrl+C). A startup banner reports required/optional config presence
(never values). Lifecycle events (not conversation content) are logged to
`data/logs/jarvis.log`. See `docs/WINDOWS_RUNBOOK.md` for the operator-facing
recovery steps and `tests/test_startup.py` for coverage (lock acquire/
stale-reclaim/release, port-conflict detection, config summary — all
mocked/isolated, no real server or process started).

## LLM provider

This repo has exactly **one** real LLM provider: Gemini Live
(`google.genai`), used directly in `main.py`. There is no multi-provider
abstraction/failover layer for the reasoning/tool-calling engine — that
would be scope creep for an assistant that only ever talks to one backend.
(Voice *output* is a separate concern with its own multi-provider system —
Gemini voice / ElevenLabs / Local — see `docs/MIC_VERIFICATION.md`.)

`core/llm_client.py` (a client for Ollama/LM Studio/other local
OpenAI-compatible servers) was removed — it was confirmed unreferenced
anywhere in the codebase, and represented a different, never-adopted
architecture rather than a real fallback path for the actual Gemini
provider.

**API key safety** (verified, not just assumed):
- `main.py`'s `_get_api_key()` raises a fixed error message on a
  missing/empty key — it never interpolates the key value itself, so
  there's nothing to leak even in that message.
- The installed `google-genai` SDK sends the API key via the
  `x-goog-api-key` HTTP header, never as a URL query parameter — confirmed
  by reading `_api_client.py` directly. This matters because the
  reconnect loop's error messages sometimes echo connection failure text;
  a URL-embedded key would have been a real leak vector.
- `main.py`'s `_classify_connection_error()` (see
  `tests/test_connection_error_reporting.py`,
  `tests/test_llm_provider_key_safety.py`) never echoes the raw exception
  message body in its network-error or generic-fallback branches — only a
  fixed template plus the exception's *type name*. Only the audio-device
  branch echoes the original message, which is safe since those errors
  come from local mic/speaker probing, never from an API response.
- Backoff on connection failure is bounded (`min(backoff * 2, 60)`,
  see `test_network_error_backoff_escalates_and_caps_at_60`) — retries
  never spin unbounded or hammer the API faster over time.
- `win10toast` (one rung of the reminder-notification fallback chain) is
  broken in this environment (`pkg_resources` removed from modern
  `setuptools`); reminders still work via the `plyer` fallback.
