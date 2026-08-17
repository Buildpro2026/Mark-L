# End-to-End Test Plan

## Why this exists

Every prompt in this series (3–16) added focused unit/integration tests
at each subsystem's own boundary — a tool dispatcher test mocks the
backend it calls; a backend test mocks the network. That's the right
default (fast, precise failure localization), but it means the *seams*
between subsystems are individually well-tested while the **full chain**
— e.g. a voice command actually changing persisted memory that a later
system prompt actually reads back — is only proven by inference, not by
a test that exercises the real chain start to finish.

This prompt adds `tests/test_end_to_end.py`: a small number of
deliberately chosen, high-value tests that each exercise a real multi-
subsystem chain, using mocks/fakes only at the true external boundary
(network calls, Gemini Live session, OS clock) — never mocking the
internal seams between JARVIS's own modules the way the per-subsystem
tests do. All are deterministic (no real network, no real credentials,
no sleeping on wall-clock time — `asyncio.sleep` is patched where a
background loop's poll interval would otherwise make a test take minutes).

## Coverage matrix (the six areas this prompt asks for)

| Area | Test | What it actually chains together |
|---|---|---|
| Voice → command → UI status | `test_save_memory_command_updates_ui_state_and_persists_to_future_prompts` | `_execute_tool()` dispatch → real `memory_manager` write → UI state transition → a *second*, independent `format_memory_for_prompt()` call proving the saved fact would actually reach a future system prompt |
| Memory/config | (same test, second half) | tool call → disk persistence → prompt-formatting read path |
| Provider/integration failure → user-visible message | `test_gmail_oauth_failure_propagates_to_a_spoken_message_without_crashing` | OAuth layer raises (`google_auth.get_credentials`) → `gmail_integration.send_email()` → voice tool dispatcher → final spoken result, proving a real low-level failure surfaces correctly through every layer above it, not just that each layer's own mock returns the right shape |
| Integration confirmation gates | `test_buffer_preview_then_confirmed_publish_two_turn_workflow` | A realistic two-call sequence: `preview` (never publishes) then `publish` (actually posts) against the same post content, proving the gate's *workflow*, not just each half in isolation |
| Proactive suggestions | `test_proactive_mode_loop_iteration_sends_a_message_and_records_the_trail` | Runs the *actual* `_run_proactive_mode()` coroutine for one real iteration (not the individual `ProactiveEngine` methods) → confirms it reaches the fake Gemini session → confirms the persistent activity trail (Prompt 15) actually has the entry |
| Scheduler state | `test_agent_scheduler_loop_iteration_runs_a_due_agent_and_persists_it` | Runs the *actual* `_run_agent_scheduler()` coroutine for one real iteration against an isolated orchestrator → confirms the task executed, the UI logged it, and a *fresh* orchestrator instance against the same DB (Prompt 16's restart recovery) sees the update |

## What's deliberately NOT re-tested here

Schedule-calculation math, individual tool argument validation, platform
character limits, duplicate-detection logic, OAuth scope details, and
every other per-subsystem behavior already has dedicated tests from its
own prompt — duplicating that here would just be slower, redundant
coverage of the same logic. These six tests only exist to prove the
*wiring between* those already-tested pieces is correct.
