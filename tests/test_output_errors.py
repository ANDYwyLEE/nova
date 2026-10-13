"""AVRecWorker error policy: consecutive failures counted per stream type, one traceback per kind."""
import logging
import queue
import threading

import pytest

import logsync  # noqa: F401  (logging.verbose)
import recworker


class Owner:
    name = "AVRecorder"

    def __init__(self, video_error=None, audio_error=None):
        self.errlog = recworker.ErrorLog(self.name)
        self.video_error, self.audio_error = video_error, audio_error
        self.writes = {1: 0, 2: 0}
        self.closed = False

    def _open_writer(self): pass
    def _close_writer(self): self.closed = True
    def _output_live(self): return False

    def write(self, typ, frame):
        self.writes[typ] += 1
        err = self.video_error if typ == 1 else self.audio_error
        if err: raise err


def run(owner, items, limit=5.0):
    q = queue.Queue()
    for it in items:
        q.put(it)
    w = recworker.AVRecWorker(owner, q)
    stopper = threading.Timer(limit, w.stop)           # the old policy never gave up here
    stopper.start()
    try:
        w.run()                                         # on this thread: no event loop needed
    finally:
        stopper.cancel()
    return w


def test_video_failing_while_audio_works_is_a_writer_failure(caplog):
    caplog.set_level(logging.ERROR)
    owner = Owner(video_error=RuntimeError("-vf: filter 'drawtext' is not available in this FFmpeg build"))
    w = run(owner, [(t, None) for _ in range(200) for t in (1, 2)])
    assert w.failure and w.failure.startswith("video:"), w.failure
    assert owner.writes[1] == recworker.AVRecWorker.MAX_CONSECUTIVE_ERRORS and owner.closed
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert sum("Traceback" in m for m in errors) == 1, errors
    assert len(errors) <= 3, errors


def test_one_traceback_per_kind_then_rate_limited_summaries(caplog, monkeypatch):
    caplog.set_level(logging.ERROR)
    log = recworker.ErrorLog("AVStreamer")
    now = [1000.0]
    monkeypatch.setattr(recworker.time, "monotonic", lambda: now[0])

    def fail(e):
        try:
            raise e
        except Exception as err:
            log.report("audio write", err)

    for i in range(500):                                # 10 s of failures at 50 per second
        fail(ValueError("Frame does not match AudioResampler setup."))
        now[0] += 0.02
    fail(OSError(28, "No space left on device"))       # another kind: its own traceback at once
    now[0] += recworker.ErrorLog.SUMMARY_S
    fail(ValueError("Frame does not match AudioResampler setup."))
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("Traceback" in m for m in msgs) == 2, msgs
    assert len(msgs) == 3 and "still failing (1x since the last message)" in msgs[-1], msgs


def test_open_errors_end_the_worker_at_once():
    from avwriter import OutputOpenError
    owner = Owner(video_error=OutputOpenError("cannot open output: Connection refused"))
    w = run(owner, [(1, None)] * 5)
    assert w.failure == "cannot open output: Connection refused" and owner.writes[1] == 1
