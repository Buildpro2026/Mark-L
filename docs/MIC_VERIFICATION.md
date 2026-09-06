# Manual Voice Pipeline Verification Checklist

Automated tests mock the audio hardware (see `tests/test_audio_device_resolution.py`,
`tests/test_connection_error_reporting.py`, `tests/test_tts_playback_error_reporting.py`,
`tests/test_barge_in*.py`, `tests/test_voice_provider_wiring.py`) — they verify
the *selection logic* and *error-reporting logic*, not real hardware. Run
this checklist by hand after any change touching `main.py`'s audio code,
`core/tts.py`, or `actions/voice_manager.py`, and whenever you set up JARVIS
on a new machine.

## 1. Startup / device selection

- [ ] Start JARVIS (`.venv\Scripts\python.exe main.py`) with your normal
      microphone and speakers/headphones connected. Console should print
      `[JARVIS] 🎤 Selected input device: [...]` and
      `[JARVIS] 🔊 Selected output device: [...]` naming your actual
      hardware, not a virtual/loopback device.
- [ ] Unplug/disable your microphone entirely (or select "no microphone" in
      Windows Sound settings), then start JARVIS. Expect a clear, visible
      error in the UI log (`ERR: Audio device problem — ...`) — **not**
      silent retries with no explanation, and not a crash.
- [ ] If you have a Voicemeeter or other virtual-audio-cable setup
      installed, confirm JARVIS still picks your real physical
      microphone/speakers, not a virtual routing device.

## 2. Speech recognition (STT)

- [ ] Speak a full sentence. Confirm the transcript appears in the UI
      (input transcription) and matches what you said.
- [ ] Speak in a quiet room, then with background noise/music playing.
      Confirm JARVIS doesn't get stuck or crash under either condition.

## 3. Speech output (TTS) — test EVERY provider you plan to use

- [ ] **Gemini voice** (default): ask a question, confirm audio plays back
      through your speakers with no delay/garbling.
- [ ] **ElevenLabs** (if you have an API key): open Voice Settings, switch
      provider to ElevenLabs, save, ask a question. Confirm real speech
      plays back (this path was broken until `miniaudio` was added —
      confirm it works before relying on it).
- [ ] **Local (Kokoro)**: switch provider to Local. If `kokoro`/`torch`
      aren't installed, confirm the UI log shows a clear fallback message
      ("... voice unavailable — using Gemini voice") and JARVIS keeps
      working on Gemini voice instead of hanging or crashing. If you do
      have Kokoro installed, confirm real offline speech plays back.
- [ ] Trigger a TTS failure deliberately (e.g. temporarily disable your
      network while using ElevenLabs) and confirm the failure appears in
      the UI log, not just the console.

## 4. Barge-in / interruption

- [ ] While JARVIS is speaking a long response, start talking. Confirm
      playback stops promptly and JARVIS starts listening to the new input
      (works for both the Gemini-voice path and, separately, the
      Local/ElevenLabs text-then-TTS path).
- [ ] Confirm JARVIS does **not** self-interrupt from hearing its own voice
      through speaker bleed into the mic (acoustic echo) during normal use.

## 5. Clean shutdown

- [ ] Say a phrase that should trigger `shutdown_jarvis` (or close the app
      normally). Confirm no orphaned audio stream keeps the process alive,
      and no unhandled exception is printed on exit.
- [ ] Restart JARVIS immediately after shutdown — confirm it reconnects
      cleanly and re-selects audio devices without needing a machine reboot.

## 6. Network loss recovery

- [ ] Disconnect the network entirely while JARVIS is running. Confirm the
      UI log shows an English, human-readable message (`NET: Could not
      connect — retrying in Ns...`), with the retry delay visibly
      increasing (backoff), not a wall of raw tracebacks or a hung UI.
- [ ] Reconnect the network — confirm JARVIS reconnects automatically
      without a restart.
