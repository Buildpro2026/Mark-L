import threading

from core import tts as core_tts


class _FakeEngine:
    def __init__(self):
        self.calls = []

    def speak(self, text, stop_event=None):
        self.calls.append((text, stop_event))


def test_ttsplayer_speak_passes_a_cleared_stop_event_to_the_engine():
    engine = _FakeEngine()
    player = core_tts.TTSPlayer(engine)

    player.speak("hello")

    assert len(engine.calls) == 1
    text, stop_event = engine.calls[0]
    assert text == "hello"
    assert isinstance(stop_event, threading.Event)
    assert stop_event.is_set() is False


def test_ttsplayer_stop_sets_the_stop_event():
    player = core_tts.TTSPlayer(_FakeEngine())
    assert player._stop_event.is_set() is False
    player.stop()
    assert player._stop_event.is_set() is True


def test_ttsplayer_stop_aborts_the_live_stream_when_one_exists(monkeypatch):
    # This is the actual latency fix: stop() must abort() (discard in-flight
    # audio) rather than sd.stop(), which waits for buffered audio to drain.
    calls = []
    fake_stream = type("StreamStub", (), {"abort": lambda self: calls.append("abort")})()
    monkeypatch.setattr(core_tts.sd, "get_stream", lambda: fake_stream)
    spy_stop = []
    monkeypatch.setattr(core_tts.sd, "stop", lambda: spy_stop.append(1))

    player = core_tts.TTSPlayer(_FakeEngine())
    player.stop()

    assert calls == ["abort"]
    assert spy_stop == []   # the draining sd.stop() must NOT be used when a stream exists


def test_ttsplayer_stop_falls_back_safely_when_nothing_has_played():
    # sd.get_stream() raises RuntimeError when play()/rec()/playrec() was
    # never called — stop() must not crash in that case (e.g. interrupt
    # pressed before any local TTS has spoken yet).
    player = core_tts.TTSPlayer(_FakeEngine())
    player.stop()   # must not raise
    assert player._playing is False


def test_stopped_helper():
    assert core_tts._stopped(None) is False
    ev = threading.Event()
    assert core_tts._stopped(ev) is False
    ev.set()
    assert core_tts._stopped(ev) is True


def test_elevenlabs_engine_skips_playback_when_stopped_during_network_wait(monkeypatch):
    played = []
    monkeypatch.setattr(core_tts, "_play_audio_bytes", lambda audio_bytes: played.append(audio_bytes))

    class _FakeResp:
        content = b"fake-mp3-bytes"
        def raise_for_status(self):
            pass

    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResp())

    engine = core_tts.ElevenLabsTTSEngine(api_key="test-key")

    stop_event = threading.Event()
    stop_event.set()   # interrupted while the (fake) network request was in flight
    engine.speak("hello", stop_event=stop_event)
    assert played == []

    engine.speak("hello", stop_event=threading.Event())
    assert played == [b"fake-mp3-bytes"]
