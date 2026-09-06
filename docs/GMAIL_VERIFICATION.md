# Manual Gmail Authorization & Verification Checklist

Automated tests (`tests/test_gmail_tool.py`, `tests/test_gmail_integration.py`,
`tests/test_google_auth.py`) mock every Gmail/OAuth call — none of them
touch a real inbox. Run this checklist by hand with a real (ideally
non-critical/test) Google account before relying on this in daily use.

## Background: what changed

`actions/gmail_integration.py` was fully built, tested, and OAuth-capable,
but wired into **nothing** — no voice tool, no dashboard action, no agent
could actually trigger it. This prompt wired it into a new `gmail` voice
tool (`main.py`) with three actions: `status`/`list` (read-only, always
safe), `draft` (creates a real Gmail draft, never sends), and `send` (a
real, irreversible send — see the gating below). The "BuildPro Email
Monitor" autonomous agent (`actions/agent_orchestrator.py`) is a separate,
still-unwired consumer — deliberately left alone here, since an autonomous
background agent acting on email is a different risk profile than a
voice command with an explicit user instruction behind it, and wasn't
part of the scope decided for this prompt.

## 1. OAuth setup validation

- [ ] Confirm `config/google/client_secret_*.json` exists (downloaded from
      Google Cloud Console — Desktop App credential type).
- [ ] Run the one-time interactive auth if `config/google/token.json`
      doesn't exist yet:
      `python -c "from actions.google_auth import authorize_interactively as a; a()"`
      — confirm it opens a real browser consent screen and completes.
- [ ] Say "is my email connected" (or similar) — confirm JARVIS calls the
      `gmail` tool with `action=status` and reports "connected and
      authorized," not a generic/wrong answer.

## 2. Least-privilege scopes (verify, don't just trust the code comment)

- [ ] During the consent screen in step 1, confirm Google lists **only**:
      "Read your email messages and settings" (gmail.readonly), "Manage
      drafts and send emails" (gmail.compose), and "View and edit events
      on your calendars" (calendar.events) — not full Gmail access, not
      full Calendar access, not any other scope.

## 3. Read boundary

- [ ] Say "read my last few emails" — confirm real sender/subject data
      comes back, not fabricated content.
- [ ] Say "any unread emails from [someone]" — confirm the Gmail search
      query (`from:`, `is:unread`) actually filters correctly.
- [ ] Disconnect from the internet, ask again — confirm an honest "couldn't
      read Gmail" style error, not a hang or a fabricated answer.

## 4. Draft boundary (should always be safe)

- [ ] Say "draft an email to test@example.com saying hello" — confirm a
      real draft appears in the Gmail account's Drafts folder, and
      **nothing is sent**.
- [ ] Confirm JARVIS's spoken response says "draft," not "sent."

## 5. Send boundary — the critical one

- [ ] Say something vague like "check my email" — confirm JARVIS does
      **not** send anything (should route to `status`/`list`, never `send`).
- [ ] Say something with a recipient but no content, e.g. "email John" —
      confirm JARVIS asks for content or drafts, never sends with guessed
      content.
- [ ] Say a fully explicit send instruction: "email jane@example.com and
      tell her the report is ready" — confirm:
      - [ ] JARVIS calls `gmail` with `action=send`, the exact recipient,
            and content matching what was said (not paraphrased into
            something different).
      - [ ] The email actually arrives at the recipient (check a real test
            inbox you control).
      - [ ] JARVIS's spoken confirmation says "sent," matching reality.
- [ ] Confirm there's no way to trigger `send` through an ambiguous or
      partial instruction — this is enforced twice: once in the tool
      description (Gemini's own judgment) and once in code
      (`main.py`'s `gmail` dispatch branch refuses without both `to` and
      `body` non-empty, regardless of what Gemini sends) — the code-level
      gate is the one to actually trust; the description-level one is
      just what makes the model *want* to provide both before calling it.

## 6. Failure honesty

- [ ] Revoke the app's access from your Google Account's security settings,
      then try `list`/`draft`/`send` — confirm each reports a clear
      NOT_AUTHORIZED-style failure, never a fabricated success.
