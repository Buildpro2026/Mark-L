# Manual Airtable Setup & Verification Checklist

Automated tests (`tests/test_airtable_integration.py`, `tests/test_airtable_tool.py`)
mock every Airtable API call — none of them touch a real base. Run this
checklist by hand with your real account (ideally against a test/scratch
base first) before relying on this in daily use.

## Background: what this is

Unlike Gmail/Calendar (pre-existing, fixed schemas — email, calendar
events), Airtable bases are entirely user-defined: any base, any table,
any field names. `actions/airtable_integration.py` makes **no** schema
assumptions — `base_id`/`table_name`/`fields` are always explicit
parameters, on every call, never hardcoded or inferred. This is a genuine
design constraint, not a shortcut: the module and the `airtable` voice
tool were both built this way deliberately.

## 1. Create a properly-scoped token (never a full-account token)

- [ ] Go to [airtable.com/create/tokens](https://airtable.com/create/tokens).
- [ ] Create a new token scoped to **only** the specific base(s) you want
      JARVIS to touch — not "all current and future bases."
- [ ] Grant only the scopes you actually need:
      - `data.records:read` — always needed for `list`/`get`.
      - `data.records:write` — only if you want `create`/`update` to work;
        omit this scope entirely if you only ever want read access, as a
        second layer of protection beyond the code's own approval gate.
- [ ] Copy the token value.

## 2. Add it to config (never paste it into a chat/conversation)

- [ ] Open `config/api_keys.json` directly in a text editor.
- [ ] Set `"airtable_token"` to the token from step 1.
- [ ] Confirm `config/api_keys.json` is git-ignored (it is, by default —
      see `.gitignore`) so the token never gets committed.
- [ ] Run `python scripts/health_check.py` — confirm it reports
      `optional:airtable_token: configured` (informational line, not a
      hard failure either way).

## 3. Connection status

- [ ] Say "is Airtable connected" — confirm JARVIS calls the `airtable`
      tool with `action=status` and reports it's connected.

## 4. Read boundary (always safe)

- [ ] Ask JARVIS to list records from a real base/table you specify by
      name — confirm real data comes back, not fabricated rows.
- [ ] Ask about a table/base that doesn't exist — confirm an honest
      "couldn't read Airtable" error naming the real problem (e.g. "Table
      not found"), not a hang or invented data.
- [ ] Try a filter formula (e.g. "records where Status is New") — confirm
      it's actually forwarded to Airtable's `filterByFormula`, not
      silently ignored.

## 5. Create/update boundary — explicit confirmation

- [ ] Say something vague like "check my leads table" — confirm JARVIS
      does **not** create anything (routes to `list`, never `create`).
- [ ] Give a fully explicit instruction naming the base, table, and
      exact fields to set — confirm:
      - [ ] The record actually appears in the real Airtable base.
      - [ ] Field values match exactly what was said, not paraphrased or
            guessed.
      - [ ] If you use a field name that doesn't exist in the table,
            confirm JARVIS reports Airtable's own error (e.g. "Unknown
            field name") rather than silently inventing/renaming a field.
- [ ] Ask JARVIS to update a specific record — confirm only the field(s)
      you named change; nothing else on the record is touched.

## 6. What's deliberately NOT built

- No `delete_record` — deleting rows wasn't part of what was wired in;
  adding it would be a separate, explicit decision (same reasoning as
  Calendar's missing `delete_event` — see `docs/CALENDAR_VERIFICATION.md`).
- No default/remembered base or table — every call names them explicitly,
  by design, so nothing is ever silently applied to the wrong base.
