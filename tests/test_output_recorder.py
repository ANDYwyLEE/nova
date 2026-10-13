"""AVRecorder on its worker-thread side, driven directly: crash leftovers, the folder lock, segment
naming and rollover (with a fake wall clock), a rename that fails."""
import logging
import os
import queue
import subprocess
import sys
import textwrap
import time
from fractions import Fraction
from types import SimpleNamespace

import av
import numpy as np
import pytest
from PySide6.QtGui import QGuiApplication

import logsync  # noqa: F401  (logging.verbose)
import recorder

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W, H, FPS = 160, 96, 25


@pytest.fixture(scope="module")
def app():
    return QGuiApplication.instance() or QGuiApplication([])


class VideoWorker:
    def __init__(self):
        self.rec_on = False

    def width(self): return W
    def height(self): return H
    def set_rec_on(self, v): self.rec_on = v
    def get_fps(self): return FPS
    def get_stream_codec(self): return "h264"
    def get_sample_rate(self): return 0
    def get_channels(self): return 0
    def get_gpu_idx(self): return 0

    def get_ffopts(self):
        return SimpleNamespace(enc_v={"preset": "ultrafast"}, enc_a={}, mux={}, v_encoder="libx264",
                               no_audio=True, v_tag=None, a_encoder=None, vf=None, metadata=None)


class Clock:
    """recorder.time with a settable wall clock."""
    def __init__(self, t):
        self.t = t

    def time(self):
        return self.t

    def __getattr__(self, name):
        return getattr(time, name)


def local(y, mo, d, h, mi, s=0):
    return time.mktime((y, mo, d, h, mi, s, 0, 0, -1))


def frame(i):
    f = av.VideoFrame.from_ndarray(np.full((H, W, 3), (i * 3) % 256, np.uint8), format="rgb24")
    f.pts, f.time_base = i, Fraction(1, FPS)
    return f


def make_recorder(tmp_path, monkeypatch, name="cam"):
    monkeypatch.chdir(tmp_path)                       # AVRecorder creates ./videos
    r = recorder.AVRecorder(name, queue.Queue(), VideoWorker(), None, None)
    r.set_dir(str(tmp_path / "videos"))
    r.flag = True                                     # switched on; the worker side is driven by hand
    return r


def feed(r, clock, t_from, t_to, i0=0):
    """Video frames at 25 fps of the fake wall clock from t_from to t_to."""
    i = i0
    clock.t = t_from
    while clock.t < t_to:
        r.write(1, frame(i))
        i += 1
        clock.t += 1.0 / FPS
    return i


def segments(folder):
    return sorted(f for f in os.listdir(folder) if f.endswith(".mp4"))


def count_frames(path):
    """Decodable video frames; the cut-off tail of a killed writer's file is skipped like a player would."""
    n = 0
    with av.open(str(path)) as c:
        for packet in c.demux(video=0):
            try:
                n += len(packet.decode())
            except av.error.InvalidDataError:
                pass
    return n


CRASH = textwrap.dedent("""
    import os, sys, numpy as np, av, logsync, avwriter
    from fractions import Fraction
    path, n = sys.argv[1], int(sys.argv[2])
    w = avwriter.AVWriter(None, path=path, width=160, height=96, fps=25, encoder="libx264",
                          enc_v_opts={"preset": "ultrafast", "g": "25"}, audio_enable=False)
    rng = np.random.default_rng(1)          # noise: real-size packets that leave PyAV's 32 KB write buffer
    for i in range(n):
        f = av.VideoFrame.from_ndarray(rng.integers(0, 255, (96, 160, 3), np.uint8), format="rgb24")
        f.pts, f.time_base = i, Fraction(1, 25)
        w.write_video(f)
    os._exit(0)                       # killed: no trailer, no close
""")


def crashed_segment(path, frames):
    env = dict(os.environ, PYTHONPATH=ROOT)
    subprocess.run([sys.executable, "-c", CRASH, str(path), str(frames)], cwd=ROOT, env=env, check=True, timeout=120)


def test_a_crash_leftover_is_kept_under_its_last_write_time(app, tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    r = make_recorder(tmp_path, monkeypatch)
    sub = tmp_path / "videos" / "cam"
    sub.mkdir(parents=True)
    crashed_segment(sub / "cam_output.mp4", 120)            # several 1 s fragments made it to disk
    crashed_segment(sub / "cam_output_1.mp4", 3)            # killed before its first fragment: header only
    (sub / "cam_output_2.mp4").write_bytes(b"")
    (sub / "cam_output_x.mp4").write_bytes(b"not ours")      # another name: untouched
    t = local(2026, 10, 13, 22, 15, 30)
    os.utime(sub / "cam_output.mp4", (t, t))
    (sub / "20261013_2215_recovered.mp4").write_bytes(b"an earlier one")

    r._open_writer()
    try:
        assert segments(sub) == ["20261013_2215_recovered.mp4", "20261013_2215_recovered_1.mp4", "cam_output_x.mp4"]
        assert count_frames(sub / "20261013_2215_recovered_1.mp4") >= 75
        msgs = [m.getMessage() for m in caplog.records]
        assert sum("left by a crash kept" in m for m in msgs) == 1
        assert sum("left by a crash removed" in m for m in msgs) == 2
    finally:
        r._close_writer()


def test_one_process_per_camera_folder(app, tmp_path, monkeypatch):
    a = make_recorder(tmp_path, monkeypatch)
    b = make_recorder(tmp_path, monkeypatch)
    a._open_writer()
    with pytest.raises(recorder.WorkerRefused, match=f"another Nova process \\(pid {os.getpid()}\\)"):
        b._open_writer()
    assert b._locked_out
    a._close_writer()                                       # released when a's worker ends
    b._open_writer()
    assert not b._locked_out
    b._close_writer()


HOLD = textwrap.dedent("""
    import sys, time, recorder
    lock = recorder.DirLock(sys.argv[1])
    print("held" if lock.acquire() else "busy", flush=True)
    time.sleep(60)
""")


def test_the_folder_lock_works_across_processes(app, tmp_path):
    folder = str(tmp_path)
    env = dict(os.environ, PYTHONPATH=ROOT)
    p = subprocess.Popen([sys.executable, "-c", HOLD, folder], cwd=ROOT, env=env, stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "held"
        mine = recorder.DirLock(folder)
        assert not mine.acquire() and mine.holder() == str(p.pid)
    finally:
        p.kill()
        p.wait()
    deadline = time.monotonic() + 5                         # (Windows releases a dead process's locks lazily)
    while not mine.acquire() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert mine.fd is not None                              # the OS released it with the process
    mine.release()


class Worker:
    def __init__(self):
        self.failure, self.ran = None, 0.0

    def stop(self): pass
    def wait(self): return True


def test_a_locked_out_recorder_waits_quietly(app, tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    r = make_recorder(tmp_path, monkeypatch)
    r.flag = False

    def start():
        r.worker = Worker()
    monkeypatch.setattr(r, "_start_worker", start)
    r.on()
    for _ in range(3):
        r._locked_out = True                                # what the worker's _open_writer found
        r.worker.failure = "videos/cam is being recorded by another Nova process (pid 4242)"
        r._on_worker_exit(r.worker.failure)
        assert r.state() == "waiting" and r._retry_timer.interval() == 60_000
        r._retry_timer.stop()
        r._restart()
    errors = [m.getMessage() for m in caplog.records if m.levelno >= logging.ERROR]
    assert len(errors) == 1 and "trying again every 60 s" in errors[0], errors
    r.off()


def test_a_failed_rename_never_lets_the_next_segment_truncate_the_file(app, tmp_path, monkeypatch):
    r = make_recorder(tmp_path, monkeypatch)
    r.set_interval(60)
    clock = Clock(0)
    monkeypatch.setattr(recorder, "time", clock)
    sub = tmp_path / "videos" / "cam"
    real_replace = os.replace
    failures = []

    def replace(src, dst):
        if not failures:                                    # the first rename fails (file in use, ...)
            failures.append(dst)
            raise PermissionError(13, "Permission denied")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    r._open_writer()
    i = feed(r, clock, local(2026, 10, 14, 10, 29, 50), local(2026, 10, 14, 10, 30, 5))
    assert failures and segments(sub) == ["cam_output.mp4", "cam_output_1.mp4"]
    kept = (sub / "cam_output.mp4").stat().st_size
    i = feed(r, clock, clock.t, local(2026, 10, 14, 10, 31, 2), i)
    assert segments(sub) == ["20261014_1029_1030.mp4", "20261014_1030_1031.mp4", "cam_output.mp4"]
    feed(r, clock, clock.t, local(2026, 10, 14, 10, 31, 4), i)
    r._close_writer()
    names = segments(sub)
    assert names == ["20261014_1029_1030.mp4", "20261014_1030_1031.mp4", "20261014_1031_1031.mp4"], names
    assert (sub / "20261014_1029_1030.mp4").stat().st_size == kept
    assert abs(count_frames(sub / "20261014_1029_1030.mp4") - 10 * FPS) <= 1     # (float clock steps)
