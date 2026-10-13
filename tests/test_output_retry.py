"""Recorder and streamer restart after a failure: backoff, state(), the gray icon, stale exits."""
import queue

import pytest
from PySide6.QtGui import QGuiApplication

import logsync  # noqa: F401  (logging.verbose)
import recorder
import streamer


@pytest.fixture(scope="module")
def app():
    return QGuiApplication.instance() or QGuiApplication([])


class VideoWorker:
    def __init__(self):
        self.rec_on = self.stream_on = False

    def width(self): return 320
    def height(self): return 180
    def set_rec_on(self, v): self.rec_on = v
    def set_stream_on(self, v): self.stream_on = v


class Worker:
    def __init__(self):
        self.failure, self.ran = None, 0.0

    def stop(self): pass
    def wait(self): return True


def with_fake_workers(monkeypatch, owner, flag):
    """The owner's real _start_worker, but with a worker that only records its fate."""
    started = []

    def start():
        owner.worker = Worker()
        started.append(owner.worker)
        setattr(owner.vid_worker, flag, True)

    monkeypatch.setattr(owner, "_start_worker", start)
    return started


def fire(owner):
    """The single-shot retry timer runs out."""
    assert owner._retry_timer.isActive()
    owner._retry_timer.stop()
    owner._retry_timer.timeout.emit()


def fail(owner, handler, ran=0.0):
    owner.worker.failure, owner.worker.ran = "cannot open output: No space left on device", ran
    handler(owner.worker.failure)
    return owner._retry_timer.interval() // 1000


@pytest.fixture
def rec(app, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                       # AVRecorder creates ./videos
    r = recorder.AVRecorder("cam", queue.Queue(), VideoWorker(), None, None)
    r.set_dir(str(tmp_path / "videos"))
    with_fake_workers(monkeypatch, r, "rec_on")
    yield r
    r.off()


def test_recorder_restarts_with_backoff_while_switched_on(rec):
    assert rec.state() == "off"
    rec.on()
    assert rec.state() == "on" and rec.vid_worker.rec_on and rec.get_icon() is rec.icon
    delays = []
    for _ in range(6):
        delays.append(fail(rec, rec._on_worker_exit))
        assert rec.state() == "waiting" and rec.is_on() and rec.get_icon() is rec.icon_gray
        assert not rec.vid_worker.rec_on                 # no frames queued while waiting
        fire(rec)
        assert rec.state() == "on" and rec.vid_worker.rec_on
    assert delays == [5, 10, 20, 40, 60, 60]
    assert fail(rec, rec._on_worker_exit, ran=61) == 5   # it had recorded for a minute: series again
    fire(rec)
    rec.off()
    assert rec.state() == "off" and not rec._retry_timer.isActive()


def test_recorder_ignores_the_exit_of_a_worker_it_no_longer_runs(rec):
    rec.on()
    rec._on_worker_exit("from a worker that was replaced")   # the current one has not failed
    assert rec.state() == "on"
    rec.off()
    rec._on_worker_exit("after off")
    assert rec.state() == "off"


def test_streamer_backoff_counts_from_the_first_packet_not_the_start(app, monkeypatch):
    s = streamer.AVStreamer("rtmp://127.0.0.1:19361/live/x", queue.Queue(), VideoWorker(), None, None)
    with_fake_workers(monkeypatch, s, "stream_on")
    s.on()
    assert s.state() == "on"
    assert fail(s, s._handle_worker_error) == 5
    assert s.state() == "waiting" and s.get_icon() is s.icon_gray
    fire(s)
    # a worker that spent 60 s in a refused open never sent a packet: no reset of the series
    assert fail(s, s._handle_worker_error, ran=0.0) == 10
    fire(s)
    assert fail(s, s._handle_worker_error, ran=59.0) == 20
    fire(s)
    assert fail(s, s._handle_worker_error, ran=60.0) == 5
    s.off()
    assert s.state() == "off"


def test_a_failed_file_stream_is_switched_off(app, monkeypatch, tmp_path):
    s = streamer.AVStreamer(str(tmp_path / "out.flv"), queue.Queue(), VideoWorker(), None, None)
    with_fake_workers(monkeypatch, s, "stream_on")
    s.on()
    fail(s, s._handle_worker_error)
    assert s.state() == "off" and not s.vid_worker.stream_on
