"""AudioWorker without a usable audio output: one ERROR, rate-limited retries, INFO on recovery."""
import logging
import queue
import threading
import time

import numpy as np

import logsync  # noqa: F401  (logging.verbose / trace)
import audioworker


class Stream:
    def __init__(self):
        self.written = 0

    def write(self, data): self.written += 1
    def stop_stream(self): pass
    def close(self): pass


class PortAudio:
    """pyaudio.PyAudio without a device until `available` is set."""
    available = False
    opens = 0
    stream = None

    def get_device_count(self): return 0
    def terminate(self): pass

    def open(self, **kw):
        PortAudio.opens += 1
        if not PortAudio.available:
            raise OSError(-9996, "Invalid output device (no default output device)")
        PortAudio.stream = Stream()
        return PortAudio.stream


def feed(q, seconds):
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:           # 20 ms buffers, like a camera's audio
        try:
            q.put((b"\0" * 640, 16000, 1), timeout=0.1)
        except queue.Full:
            pass
        time.sleep(0.02)


class StereoOnlyPortAudio(PortAudio):
    """A device that refuses mono and 8 kHz (WASAPI shared mode, some HDMI outputs)."""
    def open(self, **kw):
        PortAudio.opens += 1
        if kw.get("channels") != 2 or kw.get("rate") != 48000:
            raise OSError(-9997, "Invalid sample rate / channel count")
        PortAudio.stream = Stream()
        PortAudio.stream.kw = kw
        return PortAudio.stream


def test_mono_8k_camera_audio_plays_on_a_stereo_48k_only_device(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    PortAudio.opens, PortAudio.stream = 0, None
    monkeypatch.setattr(audioworker.pyaudio, "PyAudio", StereoOnlyPortAudio)
    monkeypatch.delenv("NOVA_AUDIO_DEVICE_INDEX", raising=False)
    q = queue.Queue(maxsize=10)
    w = audioworker.AudioWorker(q)
    t = threading.Thread(target=w.run)
    t.start()
    try:
        tone = (np.sin(np.arange(160) / 160 * 2 * np.pi * 5) * 8000).astype(np.int16).tobytes()   # 20 ms at 8 kHz
        for _ in range(10):
            q.put((tone, 8000, 1), timeout=1)
            time.sleep(0.02)
        time.sleep(0.3)
    finally:
        w.stop(); t.join(5)
    assert PortAudio.stream is not None and PortAudio.stream.kw["channels"] == 2 and PortAudio.stream.kw["rate"] == 48000
    assert PortAudio.stream.written >= 5
    assert "resampled and doubled to stereo" in caplog.text
    assert PortAudio.opens <= 3                                   # rate fallback, then stereo: no retry storm


def test_no_audio_device_logs_once_and_retries_slowly(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    PortAudio.available, PortAudio.opens, PortAudio.stream = False, 0, None
    monkeypatch.setattr(audioworker.pyaudio, "PyAudio", PortAudio)
    monkeypatch.setattr(audioworker, "OPEN_RETRY_S", 0.4)
    monkeypatch.delenv("NOVA_AUDIO_DEVICE_INDEX", raising=False)
    q = queue.Queue(maxsize=10)
    w = audioworker.AudioWorker(q)
    t = threading.Thread(target=w.run)
    t.start()
    try:
        feed(q, 1.0)
        attempts = PortAudio.opens
        assert 2 <= attempts <= 3 * 4, attempts             # ~3 attempts of 3 fallbacks, not 50 per second
        PortAudio.available = True
        feed(q, 1.0)
    finally:
        w.run_flag = False
        t.join()
    msgs = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert [m for lvl, m in msgs if lvl >= logging.ERROR] == [
        "[AudioWorker] cannot open an audio output: [Errno -9996] Invalid output device (no default output device); "
        "retrying every 0.4 s"]
    assert any(m == "[AudioWorker] audio output available again" for lvl, m in msgs if lvl == logging.INFO)
    assert PortAudio.stream.written > 0            # playback resumed (how many buffers depends on the runner's speed)
