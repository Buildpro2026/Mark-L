"""Tests for main.py's _classify_connection_error — the pure function that
turns a raw reconnect-loop exception into a user-visible log line + backoff.

Covers the two proven bugs this replaces:
  1. Audio-device errors (no mic/speaker found) used to retry silently with
     zero ui.write_log call at all.
  2. Network errors used a hardcoded Turkish string regardless of the
     user's configured language.
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _main():
    return load_module("jarvis_main_conn_err", "main.py")


def test_microphone_error_gets_specific_actionable_message():
    main = _main()
    err = RuntimeError("No usable microphone input device found (0 input-capable device(s) enumerated).")
    msg, backoff = main._classify_connection_error(err, prev_backoff=3)
    assert msg.startswith("ERR:")
    assert "microphone" in msg.lower() or "audio device" in msg.lower()
    assert "Windows Sound settings" in msg
    assert backoff == 3


def test_speaker_error_gets_specific_actionable_message():
    main = _main()
    err = RuntimeError("No speaker/headphone output device could actually be opened (2 output-capable device(s)).")
    msg, backoff = main._classify_connection_error(err, prev_backoff=3)
    assert msg.startswith("ERR:")
    assert "reconnect your microphone/speakers" in msg
    assert backoff == 3


def test_network_error_message_is_english_not_hardcoded_turkish():
    main = _main()
    err = ConnectionRefusedError("getaddrinfo failed")
    msg, backoff = main._classify_connection_error(err, prev_backoff=3)
    assert "Bağlantı" not in msg          # the old hardcoded Turkish string
    assert "VPN gerekiyor" not in msg
    assert msg.startswith("NET:")
    assert "internet connection" in msg.lower() or "vpn" in msg.lower()


def test_network_error_backoff_escalates_and_caps_at_60():
    main = _main()
    err = TimeoutError("timed out")
    _, backoff1 = main._classify_connection_error(err, prev_backoff=3)
    assert backoff1 == 6
    _, backoff2 = main._classify_connection_error(err, prev_backoff=40)
    assert backoff2 == 60   # min(80, 60)
    _, backoff3 = main._classify_connection_error(err, prev_backoff=60)
    assert backoff3 == 60   # stays capped


def test_generic_unrecognized_error_still_produces_a_visible_message():
    # This is the core silent-failure fix: previously, anything that wasn't
    # the API-key case or a recognized network substring produced NO
    # ui.write_log call whatsoever — the user saw nothing.
    main = _main()
    err = ValueError("something unexpected broke")
    msg, backoff = main._classify_connection_error(err, prev_backoff=10)
    assert msg.startswith("ERR:")
    assert "ValueError" in msg
    assert backoff == 3   # resets, matching prior non-escalating behavior


def test_microphone_check_takes_priority_over_network_substring_match():
    # A mic failure message could in principle also contain a substring the
    # network-error check matches on (e.g. "OSError" appears literally in
    # some PortAudio error text) — the audio-device branch must win so the
    # message stays specific/actionable instead of falling back to generic
    # network-retry text.
    main = _main()
    err = RuntimeError(
        "No usable microphone input device found — underlying OSError: Cannot connect to PortAudio."
    )
    msg, _ = main._classify_connection_error(err, prev_backoff=3)
    assert "Windows Sound settings" in msg
    assert not msg.startswith("NET:")
