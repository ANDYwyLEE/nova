"""AudioWorker without a usable audio output: one ERROR, rate-limited retries, INFO on recovery."""
import logging
import queue
import threading
import time

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
        assert 2 <= attempts <= 2 * 4, attempts             # ~3 attempts of 2 fallbacks, not 50 per second
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
    assert PortAudio.stream.written > 10
