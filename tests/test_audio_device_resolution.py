"""Tests for main.py's _resolve_input_device / _resolve_output_device —
the mic/speaker selection logic. These use a stub sounddevice Stream class
(dependency injection via monkeypatch) instead of real hardware, per the
"no real API keys / real devices required for automated tests" constraint.

sd.query_devices / sd.default / sd.InputStream / sd.RawOutputStream are all
patched via monkeypatch (auto-restored at teardown) rather than direct
assignment, since `main.sd` is the real, process-wide `sounddevice` module
object (import caching), not a private copy per loaded main.py instance.
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _main():
    return load_module("jarvis_main_audio_dev", "main.py")


class _StubStream:
    """Stands in for sd.InputStream / sd.RawOutputStream as a context
    manager. `opens=False` simulates a PortAudioError on open (e.g. a dead
    WDM-KS pin). `fires=True` simulates a real audio callback arriving
    (only relevant for InputStream probing, which waits on that)."""

    def __init__(self, *, device, callback=None, opens=True, fires=True, **kw):
        self.device = device
        self.callback = callback
        self.opens = opens
        self.fires = fires

    def __enter__(self):
        if not self.opens:
            raise RuntimeError("PortAudioError: [Errno -9996] Invalid device")
        if self.fires and self.callback:
            # A real callback buffer, not None — the output probe's
            # _silence_callback does `outdata[:] = ...; len(outdata)`, which
            # requires a real buffer-like object, not just any truthy value.
            self.callback(bytearray(64), 0, None, None)
        return self

    def __exit__(self, *exc):
        return False


# ── _resolve_input_device ──────────────────────────────────────────────

def test_resolve_input_device_prefers_named_microphone_over_unrelated_devices(monkeypatch):
    main = _main()
    devices = [
        {"name": "Stereo Mix", "max_input_channels": 2},
        {"name": "Microphone Array (Realtek)", "max_input_channels": 2},
        {"name": "Line In", "max_input_channels": 2},
    ]
    monkeypatch.setattr(main.sd, "query_devices", lambda: devices)
    monkeypatch.setattr(main.sd.default, "device", (-1, -1))  # stuck/invalid default, as seen in the wild
    monkeypatch.setattr(main.sd, "InputStream",
                         lambda **kw: _StubStream(device=kw["device"], callback=kw["callback"]))

    idx, name = main._resolve_input_device()
    assert name == "Microphone Array (Realtek)"
    assert idx == 1


def test_resolve_input_device_falls_back_when_priority_devices_dont_deliver_audio(monkeypatch):
    main = _main()
    devices = [
        {"name": "Microphone (dead pin)", "max_input_channels": 2},  # index 0: named mic, but never fires
        {"name": "USB Audio Device", "max_input_channels": 2},        # index 1: fallback, fires
    ]
    fires_map = {0: False, 1: True}
    monkeypatch.setattr(main.sd, "query_devices", lambda: devices)
    monkeypatch.setattr(main.sd.default, "device", (-1, -1))
    monkeypatch.setattr(main.sd, "InputStream",
                         lambda **kw: _StubStream(device=kw["device"], callback=kw["callback"],
                                                   fires=fires_map[kw["device"]]))

    idx, name = main._resolve_input_device()
    assert idx == 1
    assert name == "USB Audio Device"


def test_resolve_input_device_raises_when_no_input_capable_devices(monkeypatch):
    main = _main()
    monkeypatch.setattr(main.sd, "query_devices",
                         lambda: [{"name": "Speakers", "max_input_channels": 0}])
    with pytest.raises(RuntimeError, match="No usable microphone"):
        main._resolve_input_device()


def test_resolve_input_device_raises_when_every_device_fails_to_open(monkeypatch):
    main = _main()
    devices = [{"name": "Mic 1", "max_input_channels": 2}]
    monkeypatch.setattr(main.sd, "query_devices", lambda: devices)
    monkeypatch.setattr(main.sd.default, "device", (-1, -1))
    monkeypatch.setattr(main.sd, "InputStream",
                         lambda **kw: _StubStream(device=kw["device"], callback=kw["callback"], opens=False))

    with pytest.raises(RuntimeError, match="No microphone could actually be opened"):
        main._resolve_input_device()


# ── _resolve_output_device ─────────────────────────────────────────────

def test_resolve_output_device_prefers_named_speakers_over_tv_hdmi(monkeypatch):
    main = _main()
    devices = [
        {"name": "TV (HDMI)", "max_output_channels": 2},
        {"name": "Speakers (Realtek)", "max_output_channels": 2},
    ]
    monkeypatch.setattr(main.sd, "query_devices", lambda: devices)
    monkeypatch.setattr(main.sd.default, "device", (-1, -1))
    monkeypatch.setattr(main.sd, "RawOutputStream",
                         lambda **kw: _StubStream(device=kw["device"], callback=kw.get("callback")))

    idx, name = main._resolve_output_device()
    assert name == "Speakers (Realtek)"
    assert idx == 1


def test_resolve_output_device_uses_tv_hdmi_only_as_last_resort(monkeypatch):
    main = _main()
    devices = [{"name": "TV (HDMI)", "max_output_channels": 2}]
    monkeypatch.setattr(main.sd, "query_devices", lambda: devices)
    monkeypatch.setattr(main.sd.default, "device", (-1, -1))
    monkeypatch.setattr(main.sd, "RawOutputStream",
                         lambda **kw: _StubStream(device=kw["device"], callback=kw.get("callback")))

    idx, name = main._resolve_output_device()
    assert name == "TV (HDMI)"   # only option — better than nothing


def test_resolve_output_device_raises_when_no_output_capable_devices(monkeypatch):
    main = _main()
    monkeypatch.setattr(main.sd, "query_devices",
                         lambda: [{"name": "Mic", "max_output_channels": 0}])
    with pytest.raises(RuntimeError, match="No usable speaker/headphone"):
        main._resolve_output_device()


def test_resolve_output_device_raises_when_every_device_fails_to_open(monkeypatch):
    main = _main()
    devices = [{"name": "Speakers", "max_output_channels": 2}]
    monkeypatch.setattr(main.sd, "query_devices", lambda: devices)
    monkeypatch.setattr(main.sd.default, "device", (-1, -1))
    monkeypatch.setattr(main.sd, "RawOutputStream",
                         lambda **kw: _StubStream(device=kw["device"], opens=False))

    with pytest.raises(RuntimeError, match="No speaker/headphone output device could actually be opened"):
        main._resolve_output_device()
