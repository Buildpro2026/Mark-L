# Manual Gemini Live Verification Checklist

Automated tests (`tests/test_gemini_live_lifecycle.py`,
`tests/test_connection_error_reporting.py`, `tests/test_barge_in*.py`,
`tests/test_voice_provider_wiring.py`) cover the session lifecycle, tool-call
dispatch, error classification, backoff bounds, and interrupt/cancellation
logic against local fakes — never a live Gemini connection. Run this
checklist by hand after any change touching `main.py`'s `run()`/
`_receive_audio()`/`_execute_tool()`/`_build_config()`, using a real
`gemini_api_key`.

## Design note: no separate "normal LLM mode" exists

This checklist's source prompt asked to verify "graceful fallback to normal
LLM mode." **This repo has no such mode.** Gemini Live is the only LLM
backend JARVIS talks to — there is no secondary text-completion API it
falls back to when Live is unavailable (see `docs/OPERATIONS.md`'s "LLM
provider" section: `core/llm_client.py`, which would have been that
fallback, was confirmed dead code and removed). The actual graceful-
degradation behavior is: **retry the same Live connection with bounded,
escalating backoff**, and clearly surface *why* it's retrying:
- HUD shows `RECONNECTING` (not `SLEEPING`) specifically when the retry is
  error-driven — see `_post_session_state()`.
- The log panel shows a plain-English reason (`NET:`/`ERR:` prefixed) —
  see `_classify_connection_error()`.
- Backoff is capped at 60s and never spins unbounded.

There is nothing to fall back *to* beyond that. If a genuine secondary
LLM backend is ever wanted, it would need to be designed and built new —
not "restored," since the dead code that used to exist for this
(`core/llm_client.py`) targeted local Ollama/LM Studio servers, an
architecture this product doesn't use.

## 1. Authentication

- [ ] Start JARVIS with a valid `gemini_api_key` in `config/api_keys.json`
      — confirm it connects (`[JARVIS] Connected.` in console, `SYS: JARVIS
      online.` in the UI log, HUD shows `LISTENING`).
- [ ] Set an invalid/garbage `gemini_api_key`, start JARVIS — confirm the
      UI log shows `ERR: API key invalid — please re-enter your key.`, the
      HUD shows `SLEEPING`, and a reconfiguration prompt appears (not an
      infinite silent retry loop).
- [ ] Remove `gemini_api_key` entirely — confirm the same clear
      reconfiguration path, not a raw traceback shown to the user.

## 2. Live-session lifecycle

- [ ] Watch a full connect → converse → idle → reconnect cycle. HUD should
      move `THINKING` → `LISTENING` and back to `LISTENING` after each
      response, never getting stuck on `THINKING`.
- [ ] Force a network drop (disable Wi-Fi) mid-session — confirm the HUD
      switches to `RECONNECTING` (not `SLEEPING`) and the log shows an
      escalating-backoff `NET:` message in plain English.
- [ ] Restore the network — confirm it reconnects automatically without a
      restart, and HUD returns to `LISTENING`.
- [ ] Change voice provider in Settings mid-session — confirm it reconnects
      quietly (log: "Voice settings changed — reconnecting...") without
      showing an error, and the new voice takes effect on the next response.

## 3. Audio/text event handling

- [ ] Ask a question requiring a tool call (e.g. "what's the weather in
      Chicago"). Confirm the HUD briefly shows `THINKING` during tool
      execution, then `LISTENING`/`SPEAKING` once the response starts.
- [ ] Ask something requiring two tool calls in one turn (e.g. "check the
      weather and open Chrome") — confirm both actually execute (not just
      one), matching `test_multiple_function_calls_in_one_tool_call_all_get_responses`.
- [ ] Confirm the spoken/text response reflects real tool output, not a
      hallucinated answer (e.g. actually opens the app, actually reports
      real weather).

## 4. Cancellation

- [ ] Interrupt JARVIS mid-response by speaking — confirm playback stops
      immediately (not after finishing the sentence) and JARVIS starts
      listening to the new input. (Covered in depth by
      `docs/MIC_VERIFICATION.md` section 4 — this is the same mechanism.)
- [ ] Say the shutdown phrase — confirm the session closes cleanly (HUD:
      `SLEEPING`, not `RECONNECTING` — a deliberate shutdown is not an
      error) and the process can be restarted immediately after.

## 5. Status exposure in the UI

- [ ] Confirm every one of these states is visibly distinct on the HUD at
      some point during normal use: `THINKING`, `LISTENING`, `SPEAKING`,
      `SLEEPING`, `RECONNECTING`.
- [ ] Confirm the scrolling log panel (not just the HUD orb) shows a
      human-readable line for every state change that matters: connect,
      disconnect, error, voice-provider change.
