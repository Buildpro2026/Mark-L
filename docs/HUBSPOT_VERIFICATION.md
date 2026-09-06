# Manual HubSpot Verification Checklist

Automated tests (`tests/test_hubspot_tool.py`, `tests/test_hubspot_integration.py`,
`tests/test_buildpro_sync.py`) mock every HubSpot call — none of them touch
a real portal. Run this checklist by hand with a real (ideally sandbox)
HubSpot account before relying on this in daily use.

## Background: what changed

`actions/hubspot_integration.py` already had real, tested read/write
functions and was already used for two read-only purposes: syncing
HubSpot contacts/companies into the local BuildPro database
(`actions/buildpro_sync.py`) and identifying prospects for the BuildPro
Prospecting agent (`actions/agent_orchestrator.py`). Neither of those ever
wrote back to HubSpot. This prompt:

1. Added an `approved: bool = False` gate to `create_contact`,
   `update_contact`, `create_company`, `update_company` — previously
   these had **no** confirmation gate at all (unlike Gmail/Calendar/
   Airtable's writes), even though nothing in the codebase called them in
   production. They're safe now regardless of what calls them next.
2. Added `upsert_contact(email, properties, approved)` and
   `upsert_company(name, properties, approved)` — idempotent create-or-
   update: searches for an existing match first (by email/name), updates
   it if found, creates a new one if not. Calling either twice with the
   same key never creates a duplicate.
3. Wired a new `hubspot` voice tool (`main.py`): `status`, read-only
   `list_contacts`/`list_companies`/`search_contacts`/`search_companies`,
   and gated `upsert_contact`/`upsert_company`.

## 1. Credential setup

- [ ] In HubSpot: Settings → Integrations → Private Apps → create one
      scoped to only the CRM scopes you need (`crm.objects.contacts.read`/
      `.write`, `crm.objects.companies.read`/`.write`) — not every scope
      HubSpot offers.
- [ ] Paste the token directly into `config/api_keys.json` under
      `"hubspot_token"` — never into a chat/conversation.
- [ ] Run `python scripts/health_check.py` — confirm
      `optional:hubspot_token: configured`.
- [ ] Say "is HubSpot connected" — confirm JARVIS reports connected.

## 2. Read boundary

- [ ] Ask "list my HubSpot contacts" / "search HubSpot for [name]" —
      confirm real data comes back.
- [ ] Confirm a search/list failure (e.g. revoke the token temporarily)
      reports an honest error, not fabricated results.

## 3. Write boundary — explicit confirmation

- [ ] Say something vague like "check my contacts" — confirm JARVIS does
      **not** write anything (routes to list/search, never upsert).
- [ ] Give a fully explicit instruction: "add a HubSpot contact for
      jane@example.com, first name Jane" — confirm a real contact appears
      in HubSpot with exactly those field values, nothing invented.

## 4. Deduplication — the critical property to verify live

- [ ] Run the same "add a contact for jane@example.com" instruction
      **twice** (different details the second time, e.g. a different
      phone number) — confirm HubSpot ends up with **one** contact for
      that email, updated with the second call's details, not two
      separate contacts.
- [ ] Do the same for a company by name.
- [ ] Confirm `upsert_contact`/`upsert_company` never silently overwrite
      fields you didn't mention — only the properties you actually
      specify should change (existing HubSpot fields not named stay as
      they were).

## 5. Sync path (pre-existing, unaffected by this prompt)

- [ ] Confirm `actions/buildpro_sync.py`'s pull-only sync
      (HubSpot → local BuildPro DB) still behaves as before — this
      prompt didn't touch its logic, only added new capabilities
      alongside it.
