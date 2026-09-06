# Manual Calendar Authorization & Verification Checklist

Automated tests (`tests/test_calendar_tool.py`, `tests/test_calendar_integration.py`,
`tests/test_google_auth.py`) mock every Calendar/OAuth call — none of them
touch a real calendar. Run this checklist by hand with a real (ideally
non-critical/test) Google account before relying on this in daily use.

## Background: what changed

`actions/calendar_integration.py`, like Gmail before it (see
`docs/GMAIL_VERIFICATION.md`), was fully built and OAuth-capable but wired
into nothing. This prompt wired it into a new `calendar` voice tool with
`status`/`list` (read-only), `create` (gated, now also conflict-checked),
and `update` (gated) actions. Two real backend additions were made, not
just wiring:
- **Conflict detection**: `create_event()` now checks for overlapping
  existing events first and refuses (`state=CONFLICT`, with the
  conflicting events listed) unless `ignore_conflicts=True` is explicitly
  passed — it no longer silently double-books.
- **Timezone safety**: a datetime with no UTC offset (which an LLM could
  plausibly produce) now gets the system's local timezone attached rather
  than being sent to Google's API ambiguous or naive.

`delete_event` was deliberately **not** added — it doesn't exist in
`calendar_integration.py` today, and adding untested delete-capable code
against a real calendar is a bigger, separate decision than wiring up
already-built read/create/update code was.

## 1. OAuth setup validation

- [ ] Confirm `config/google/client_secret_*.json` and
      `config/google/token.json` exist (same shared credential as Gmail —
      see `docs/GMAIL_VERIFICATION.md` section 1 if not).
- [ ] Say "is my calendar connected" — confirm JARVIS calls `calendar`
      with `action=status` and reports "connected and authorized."

## 2. Timezone-safe reads

- [ ] Say "what's on my calendar" — confirm returned event times match
      what's actually in Google Calendar, in the correct timezone (not
      shifted by your UTC offset).
- [ ] If you're near a DST transition, double-check event times right
      around the transition specifically.

## 3. Read boundary

- [ ] Confirm `list` never fabricates events — disconnect from the
      internet and ask again; confirm an honest "couldn't read the
      calendar" error, not a hang or invented events.

## 4. Conflict detection

- [ ] Create a test event manually in Google Calendar for a specific
      time slot.
- [ ] Ask JARVIS to schedule something else at the same (or overlapping)
      time — confirm it refuses and tells you what's already there,
      rather than double-booking silently.
- [ ] Confirm no event was actually created in Google Calendar after the
      conflict refusal.
- [ ] Explicitly tell JARVIS to schedule it anyway despite the conflict —
      confirm this time it actually creates the event (exercises
      `ignore_conflicts`).

## 5. Create/update boundary — explicit confirmation

- [ ] Say something vague like "check my schedule" — confirm JARVIS does
      **not** create anything (routes to `list`, never `create`).
- [ ] Say a fully explicit instruction: "schedule a call with Jane
      tomorrow at 2pm for 30 minutes" — confirm:
      - [ ] JARVIS calls `calendar` with `action=create`, a real
            title/start/end matching what was said.
      - [ ] The event actually appears in Google Calendar.
      - [ ] The spoken confirmation matches reality ("created," not
            something implying it merely planned to).
- [ ] Ask JARVIS to reschedule that same event — confirm `update` is
      used, only the changed field(s) are touched, and the original
      title/location aren't accidentally cleared.

## 6. Failure honesty

- [ ] Revoke the app's access from your Google Account's security
      settings, then try `list`/`create`/`update` — confirm each reports
      a clear NOT_AUTHORIZED-style failure, never a fabricated success.
