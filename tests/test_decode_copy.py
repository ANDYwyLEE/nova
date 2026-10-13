"""The frame copy made for the live stream while recording (both consumers set pts on their frame)."""
from fractions import Fraction

import av
import numpy as np
import pytest
from PySide6.QtGui import QGuiApplication

from videoworker import VideoWorker, _copy_frame


def _filled(frame, seed=0):
    rng = np.random.default_rng(seed)
    for p in frame.planes:
        a = np.frombuffer(memoryview(p), dtype=np.uint8)
        a[:] = rng.integers(0, 256, a.size, dtype=np.uint8)
    return frame


def _visible(plane, rows=None):
    rows = plane.height if rows is None else rows
    row_bytes = plane.width * (plane.line_size // max(1, plane.width)) if plane.width else plane.line_size
    a = np.frombuffer(memoryview(plane), dtype=np.uint8).reshape(-1, plane.line_size)
    return a[:rows, :min(row_bytes, plane.line_size)].copy()


@pytest.mark.parametrize("fmt, w, h", [
    ("yuvj422p", 640, 360),      # software MJPEG
    ("p010le", 1280, 720),       # 10-bit HEVC from hevc_cuvid
    ("nv12", 321, 181),          # cuvid output, odd size
    ("yuv420p", 321, 181),       # odd size
    ("yuv420p", 1920, 1080),
    ("yuv444p", 64, 48),
    ("gray", 33, 17),
    ("rgb24", 35, 19),
])
def test_video_copy_is_exact_and_independent(fmt, w, h):
    src = _filled(av.VideoFrame(w, h, fmt))
    src.pts, src.time_base = 1234, Fraction(1, 90000)
    try: src.color_range = 2                              # full range (yuvj / MJPEG)
    except Exception: pass
    c = _copy_frame(src)
    assert c is not src and (c.width, c.height, c.format.name) == (w, h, fmt)
    assert c.pts == 1234 and c.time_base == Fraction(1, 90000)
    assert c.color_range == src.color_range
    for ps, pc in zip(src.planes, c.planes):
        assert np.array_equal(_visible(ps), _visible(pc))
    # really a copy: changing it leaves the original alone, and pts are independent
    before = _visible(src.planes[0])
    np.frombuffer(memoryview(c.planes[0]), dtype=np.uint8)[:] = 0
    c.pts = 1
    assert np.array_equal(before, _visible(src.planes[0])) and src.pts == 1234
    # what the writers do with it
    c2 = _copy_frame(src)
    assert c2.reformat(format="yuv420p", width=w - w % 2, height=h - h % 2).format.name == "yuv420p"


@pytest.mark.parametrize("fmt, layout, samples", [("fltp", "stereo", 1024), ("s16", "mono", 160),
                                                  ("s16", "stereo", 480), ("fltp", "mono", 1024)])
def test_audio_copy(fmt, layout, samples):
    src = _filled(av.AudioFrame(format=fmt, layout=layout, samples=samples))
    src.sample_rate, src.pts, src.time_base = 8000, 77, Fraction(1, 8000)
    c = _copy_frame(src)
    assert (c.format.name, c.layout.name, c.samples, c.sample_rate, c.pts) == (fmt, layout, samples, 8000, 77)
    assert c.to_ndarray().tobytes() == src.to_ndarray().tobytes()          # random bytes: compare bits, not NaNs
    c.planes[0].update(bytes(c.planes[0].buffer_size))
    assert c.to_ndarray().tobytes() != src.to_ndarray().tobytes()


def test_decoded_mjpeg_422_reaches_both_queues(tmp_path):
    """A real yuvj422p frame from the MJPEG decoder, recording and streaming both on."""
    QGuiApplication.instance() or QGuiApplication([])
    path = tmp_path / "m.avi"
    with av.open(str(path), "w") as out:
        s = out.add_stream("mjpeg", rate=25)
        s.width, s.height, s.pix_fmt = 641, 361, "yuvj422p"
        f = av.VideoFrame.from_ndarray(np.full((361, 641, 3), 99, np.uint8), format="rgb24").reformat(format="yuvj422p")
        f.pts = 0
        for p in s.encode(f): out.mux(p)
        for p in s.encode(None): out.mux(p)
    with av.open(str(path)) as c:
        frame = next(c.decode(video=0))
    assert frame.format.name == "yuvj422p"
    w = VideoWorker(str(path), 30, False, None, True, None)
    w.rec_on = w.stream_on = True
    w._dispatch_av(1, frame)
    (_, a), (_, b) = w.rec_q.get_nowait(), w.stream_q.get_nowait()
    assert a is frame and b is not frame and b.format.name == "yuvj422p" and (b.width, b.height) == (641, 361)
    assert np.array_equal(a.to_ndarray(format="rgb24"), b.to_ndarray(format="rgb24"))
