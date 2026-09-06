# BuildPro Matching — Input Schema, Scoring, and Review Requirements

## What this is (and isn't)

`actions/buildpro_matching.py` produces a **transparent, rule-based
comparison score** between a candidate and a job — not a machine-learning
prediction, not an objective ranking, and never an automated hiring
decision. Every score comes with a plain-English rationale explaining
exactly which fields were compared and why. A human must review a match
before acting on it (submitting a candidate, contacting a client, or
rejecting anyone) — nothing in this pipeline contacts a candidate,
client, or anyone else. See `main.py`'s `buildpro_matching` voice tool
description for the exact disclaiming language surfaced to the model.

## Candidate input schema

| Field | Type | Used for matching? | Notes |
|---|---|---|---|
| `name` | text | no | required |
| `email` | text | no | **the dedup key** — see below |
| `phone` | text | no | |
| `source` | text | no | e.g. `"hubspot"`, `"manual"` |
| `status` | enum | no | `new / screening / submitted / interviewing / placed / rejected / withdrawn` |
| `title` | text | yes (20%) | exact or word-overlap match against the job's title |
| `specialty` | text | yes (20%) | exact or substring match |
| `years_experience` | integer | yes (20%) | vs. the job's `min_years_experience` |
| `skills` | comma-separated text | yes (20%) | overlap ratio against the job's `required_skills` |
| `location` | text | yes (10%) | exact/substring match, or either side says "remote" |
| `desired_compensation` | text | yes (5%) | numeric range comparison, best-effort |
| `availability` | enum | yes (5%) | `available / employed / not_looking / unknown` |
| `hubspot_contact_id` | text | no | sync-only, links to a HubSpot contact |

Every matching field is **nullable and never guessed at** — a candidate
missing a field simply has that factor skipped (not scored zero) when
matched against a job. See `docs/HUBSPOT_VERIFICATION.md` for how HubSpot
sync populates these fields honestly (only real HubSpot property values,
never fabricated).

## Job input schema

| Field | Type | Used for matching? |
|---|---|---|
| `title` | text | yes (20%) |
| `client_id` | integer | no |
| `description` | text | no |
| `location` | text | yes (10%) |
| `status` | enum | no | `open / on_hold / filled / closed` |
| `specialty` | text | yes (20%) |
| `min_years_experience` | integer | yes (20%) |
| `required_skills` | comma-separated text | yes (20%) |
| `compensation` | text | yes (5%) |
| `employment_type` | text | no |

## Scoring

Score = (earned points / possible points across only the factors that
had data on **both** sides) × 100, so it's always relative to what was
actually comparable — never diluted by fields nobody filled in. `score`
is `None` (not a fabricated number) when zero factors were comparable at
all. The rationale string always names which factors were used and which
were skipped for missing data.

`buildpro_data.QUALIFIED_MATCH_SCORE = 70.0` is the documented default
threshold for "qualified" in summary views — a reasonable default, not
derived from any external hiring standard, and callers can pass their own
`min_score` to `generate_matches_for_job()`/`generate_matches_for_candidate()`
to only store/surface matches above whatever bar they choose.

## Duplicate handling

- **Matches**: `find_match(candidate_id, job_id)` — re-scoring the same
  pair always updates the existing row, never creates a second one.
- **Candidates/clients**: `upsert_candidate()`/`upsert_client()`
  (added this prompt) deduplicate by email — adding the same person
  twice updates their existing record instead of creating a duplicate.
  Records without an email can't be deduplicated this way; each
  `add_candidate()`/`add_client()` call for those creates a new row, same
  as before this prompt.
- **Jobs**: no dedup key — jobs are created deliberately, once per
  opening; this wasn't identified as a real duplicate-entry risk the way
  repeated candidate/client entry is.

## Privacy

Candidate/client PII (name, email, phone) lives only in the local SQLite
database (`data/jarvis2.db`), which is git-ignored (`.gitignore`) and
never transmitted anywhere by this module — `buildpro_matching.py` does
no logging, no printing, and no network calls at all; it's pure
in-memory comparison plus local SQL reads/writes via `buildpro_data.py`.
Broader dashboard-level access control (who can view this data through
the 3D command center) is a separate, already-flagged concern — see
`docs/OPERATIONS.md`'s dashboard security notes.
