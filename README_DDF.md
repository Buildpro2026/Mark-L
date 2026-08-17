# JARVIS 2.0 Daily Deal Finders

This repository now includes a lightweight local Daily Deal Finders pipeline that can:

- discover and score products locally,
- store them in SQLite,
- prepare social captions and affiliate URLs,
- create draft posts,
- and verify Buffer configuration status.

## Current status

- Local SQLite storage: available
- Product scoring: implemented
- Deduplication: implemented
- Draft post generation: implemented
- Buffer verification: implemented

## Notes

- Buffer publishing requires a real configured token and approval gate.
- No external publication is claimed without API confirmation.
