# Manual Social Posting (Buffer) Verification Checklist

Automated tests (`tests/test_social_post_tool.py`, `tests/test_buffer_integration.py`)
mock every Buffer call — none of them post to a real account. Run this
checklist by hand with a real (ideally a scratch/test channel) Buffer
account before relying on this in daily use.

## Background: what changed

`actions/buffer_integration.py` had a real, long-standing bug (flagged in
the very first audit of this project): `publish_to_buffer()` checked
whether Buffer was configured **before** validating the post itself, so
in an unconfigured environment every validation error (missing text,
missing channel, invalid mode) was masked behind a generic "NOT
CONFIGURED" — the 3 tests that caught this had been failing since before
any of this work started. Fixed by reordering: input validation now runs
first, so a caller always learns what's wrong with their post regardless
of Buffer's config state.

Three more things were added, all explicitly requested for this prompt:

1. **Preview + confirmation gate**: `publish_to_buffer(post, approved=False)`
   (the default) validates everything and returns a `PREVIEW` — exactly
   what would be posted, to which channel, in what mode — without
   publishing anything. Nothing publishes without `approved=True`. Wired
   into a new `social_post` voice tool with separate `preview` and
   `publish` actions.
2. **Platform character limits**: when a `service` name is given (e.g.
   `"twitter"`), text exceeding that platform's known limit (280 for
   Twitter/X, 3000 LinkedIn, 2200 Instagram, etc.) is refused before
   ever reaching Buffer's API, instead of risking silent truncation.
3. **Duplicate-post prevention**: a local SQLite table
   (`buffer_posts` in `data/jarvis2.db`) tracks what's been published
   through this function; re-publishing identical text to the same
   channel within 24 hours is refused unless `allow_duplicate=True`.

Buffer itself was already the "already represented" social integration in
this codebase (confirmed real, GraphQL-based, live-tested) — per this
prompt's explicit "implement only integrations already represented,"
no new direct platform APIs (Twitter/LinkedIn/etc.) were built. Buffer is
the gateway to whichever platforms are connected in the user's Buffer
account.

## 1. Credential setup

- [ ] Paste your real Buffer Personal Access Token into
      `config/api_keys.json` under `"buffer_token"` — never into a
      chat/conversation.
- [ ] Say "is Buffer connected" — confirm JARVIS reports it's verified.

## 2. Preview — must never post anything

- [ ] Ask JARVIS to preview a post to a specific connected channel —
      confirm it shows the exact text/channel/mode back to you, and
      confirm **nothing appears in Buffer's queue** afterward.
- [ ] Preview a post with text over a platform's limit (e.g. 300+
      characters targeting `service: "twitter"`) — confirm it's refused
      with a clear character-count message, not silently truncated or
      passed through.

## 3. Publish — explicit confirmation required

- [ ] Say something vague like "check my Buffer queue" — confirm JARVIS
      does **not** publish anything (routes to `status`, never `publish`).
- [ ] Preview a post, explicitly confirm you want it posted, then have
      JARVIS call `publish` — confirm it actually appears in Buffer's
      queue for that channel with the exact text.

## 4. Duplicate prevention

- [ ] Publish the same text to the same channel twice in a row — confirm
      the second attempt is refused as a duplicate, and Buffer's queue
      only shows it once.
- [ ] Confirm posting that same text to a **different** channel is not
      blocked (duplicates are tracked per-channel, not globally).
- [ ] Confirm explicitly allowing a duplicate (`allow_duplicate`) lets a
      genuine repeat through when you actually want that.

## 5. Failure honesty

- [ ] Temporarily use an invalid token, try to preview/publish — confirm
      an honest connection error, never a fabricated "posted" result.
