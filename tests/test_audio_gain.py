"""Phase 4 voice volume control — main.py's _AudioSink applies a real
gain multiplier to the PCM stream, wired from the persisted
voice_volume preference. This is the one voice setting genuinely new
in Phase 4 (provider/voice/speed already existed); volume never did
anything before this.
"""
import importlib.util
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _main():
    return load_module("jarvis_main_audio_gain", "main.py")


def _pcm(samples):
    return struct.pack(f"<{len(samples)}h", *samples)


def _unpack(data):
    return list(struct.unpack(f"<{len(data)//2}h", data))


def test_default_gain_leaves_audio_unchanged():
    main = _main()
    sink = main._AudioSink()
    original = [1000, -1000, 5000, -5000]
    sink.write(_pcm(original))
    out = _unpack(sink.read(len(original) * 2))
    assert out == original


def test_zero_gain_mutes_completely():
    main = _main()
    sink = main._AudioSink(gain=0.0)
    sink.write(_pcm([1000, -1000, 5000]))
    out = _unpack(sink.read(6))
    assert out == [0, 0, 0]


def test_half_gain_halves_amplitude():
    main = _main()
    sink = main._AudioSink(gain=0.5)
    sink.write(_pcm([1000, -1000, 2000]))
    out = _unpack(sink.read(6))
    assert out == [500, -500, 1000]


def test_gain_above_one_boosts_but_clips_at_int16_range():
    main = _main()
    sink = main._AudioSink(gain=2.0)
    sink.write(_pcm([20000, -20000]))
    out = _unpack(sink.read(4))
    assert out == [32767, -32768]


def test_gain_is_clamped_to_a_sane_range_at_construction():
    main = _main()
    sink = main._AudioSink(gain=99.0)
    assert sink._gain == 2.0
    sink2 = main._AudioSink(gain=-5.0)
    assert sink2._gain == 0.0


def test_set_gain_changes_behavior_for_subsequent_writes():
    main = _main()
    sink = main._AudioSink()
    sink.set_gain(0.0)
    sink.write(_pcm([1000]))
    out = _unpack(sink.read(2))
    assert out == [0]
