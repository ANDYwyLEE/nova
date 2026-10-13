"""Hand-off to the recorder/streamer queues: drops are reported, NOVA_TIMEOUT parsed once."""
import logging
import queue

import videoworker
from videoworker import VideoWorker


def test_a_full_queue_drops_the_oldest_and_says_so(caplog, monkeypatch):
    vw = VideoWorker.__new__(VideoWorker)               # only what _put needs
    vw._drops = {}
    q = queue.Queue(maxsize=2)
    clock = iter([100.0, 100.5, 101.0, 111.0])
    monkeypatch.setattr(videoworker.time, "monotonic", lambda: next(clock))
    with caplog.at_level(logging.WARNING):
        for i in range(6):                              # 2 fit, 4 drop the oldest
            vw._put(q, i, "recorder")
    assert [q.get_nowait(), q.get_nowait()] == [4, 5]
    warnings = [r.getMessage() for r in caplog.records if "cannot keep up" in r.getMessage()]
    assert warnings == ["[VideoWorker] recorder cannot keep up: 1 frame(s) dropped",
                        "[VideoWorker] recorder cannot keep up: 3 frame(s) dropped"]


def test_nova_timeout_is_parsed_once_with_a_fallback(monkeypatch, caplog):
    monkeypatch.setenv("NOVA_TIMEOUT", "7000000")
    assert videoworker._timeout_us() == 7_000_000 and videoworker._timeout_seconds() == 7.0
    monkeypatch.setenv("NOVA_TIMEOUT", "10s")
    monkeypatch.setattr(videoworker, "_bad_timeout_warned", False)
    with caplog.at_level(logging.WARNING):
        assert videoworker._timeout_us() == 5_000_000
        assert videoworker._build_conn_options("rtsp://h/x")["timeout"] == "5000000"
    assert sum("NOVA_TIMEOUT" in r.getMessage() for r in caplog.records) == 1
