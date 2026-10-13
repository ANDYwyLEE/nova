"""VideoWorker without a camera: size across reconnects, the frame-rate clamp, open-error logging."""
import logging
from fractions import Fraction
from types import SimpleNamespace

import av
import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication

from videoworker import VideoWorker


@pytest.fixture(scope="module")
def qapp():
    return QGuiApplication.instance() or QGuiApplication([])


def _worker(url="file.mp4", accel=None, gpu=None, headless=True):
    w = VideoWorker(url, 30, False, accel, headless, None, gpu=gpu)
    w.clock.sleep = lambda ms: None          # no 10 s retry pauses in tests
    return w


def _write_clip(path, *, frames=75, audio_s=None, codec="libx264", pix_fmt="yuv420p", size=(320, 180), fmt=None,
                corrupt_video=(), corrupt_audio=()):
    """H.264 (or another codec) + optional AAC, written with PyAV.  corrupt_*: 1-based packet numbers
    whose payload is replaced by garbage after encoding (a damaged packet as a camera might send).
    Use .nut: the mkv and mp4 muxers drop an H.264 packet they cannot parse."""
    w, h = size
    with av.open(str(path), "w", format=fmt) as out:
        v = out.add_stream(codec, rate=25)
        v.width, v.height, v.pix_fmt = w, h, pix_fmt
        if codec == "libx264":
            v.codec_context.options = {"preset": "ultrafast", "g": "25"}
        a = None
        if audio_s:
            a = out.add_stream("aac", rate=48000)
            a.layout = "mono"
        nv = na = 0
        rng = np.random.default_rng(1)

        def mux(p):
            nonlocal nv, na
            if p.stream.type == "video":
                nv += 1
                bad = nv in corrupt_video
            else:
                na += 1
                bad = na in corrupt_audio
            if bad:
                q = av.Packet(bytes(rng.integers(0, 256, 120, dtype=np.uint8)))
                q.pts, q.dts, q.time_base, q.stream = p.pts, p.dts, p.time_base, p.stream
                p = q
            out.mux(p)

        apts = 0
        a_total = int((audio_s or 0) * 48000)
        for i in range(max(frames, int((audio_s or 0) * 25))):
            if i < frames:
                img = np.zeros((h, w, 3), dtype=np.uint8)
                img[:, :, 0] = np.linspace(0, 255, w, dtype=np.uint8)[None, :]
                img[:, (i * 4) % w:(i * 4) % w + 8, :] = 255
                f = av.VideoFrame.from_ndarray(img, format="rgb24")
                f.pts = i
                for p in v.encode(f): mux(p)
            while a is not None and apts < min(a_total, (i + 1) * 48000 // 25):
                af = av.AudioFrame.from_ndarray(np.zeros((1, 1024), dtype=np.float32), format="fltp", layout="mono")
                af.sample_rate, af.pts = 48000, apts
                apts += 1024
                for p in a.encode(af): mux(p)
        for p in v.encode(None): mux(p)
        if a is not None:
            for p in a.encode(None): mux(p)
    return path


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    return _write_clip(tmp_path_factory.mktemp("dec") / "clip.nut", audio_s=3)


# ---------------------------------------------------------------- size across reconnects
def test_size_survives_reconnects_and_sz_sig_fires_once(qapp, clip):
    w = _worker(str(clip))
    sizes = []
    w.sz_sig.connect(lambda a, b: sizes.append((a, b)), Qt.DirectConnection)
    assert w.size() == (0, 0) and w.ratio() == 1.0
    for _ in range(3):                                   # three connections to the same camera
        with av.open(str(clip)) as c:
            w._stream_loop(c, "clip")
        # between connections (the reconnect gap) the last size is still there
        assert (w.width(), w.height()) == (320, 180)
        assert w.ratio() == pytest.approx(320 / 180)
    assert sizes == [(320, 180)]
    assert w.last_frame_time > 0


# ---------------------------------------------------------------- frame rate
@pytest.mark.parametrize("avg, guessed, expect", [
    (Fraction(25), None, 25),
    (None, Fraction(90000), 30),          # RTP clock rate, not a frame rate -> -f value
    (Fraction(90000), Fraction(15), 15),
    (Fraction(1, 2), None, 1),            # time-lapse
    (None, None, 30),
])
def test_frame_rate_is_clamped(qapp, avg, guessed, expect):
    w = _worker()
    assert w._frame_rate(SimpleNamespace(average_rate=avg, guessed_rate=guessed)) == expect


# ---------------------------------------------------------------- open errors
def test_repeated_open_error_is_logged_once(qapp, tmp_path, caplog):
    w = _worker()
    caplog.set_level(logging.DEBUG)
    missing = str(tmp_path / "missing.mp4")
    for _ in range(3):
        assert w._open_container(missing) is None
    loud = [r for r in caplog.records if "open failed" in r.getMessage() and r.levelno >= logging.WARNING]
    quiet = [r for r in caplog.records if "open failed" in r.getMessage() and r.levelno == logging.DEBUG]
    assert len(loud) == 1 and len(quiet) == 2


def test_stop_reports_whether_the_thread_ended(qapp):
    w = _worker()
    assert w.stop() is True                                # never started
