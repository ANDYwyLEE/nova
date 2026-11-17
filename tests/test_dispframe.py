"""The picture a window without --opengl shows: converted to RGBA on the CPU, once for every picture."""
import os

import av
import numpy as np
import pytest

import dispframe
import util


def _picture(fmt="yuv420p", width=1920, height=1080):
    frame = av.VideoFrame(width, height, fmt)
    rng = np.random.default_rng(1)
    for plane in frame.planes:
        np.frombuffer(plane, np.uint8)[:] = rng.integers(0, 255, plane.buffer_size, dtype=np.uint8)
    return frame


def _threads():
    return len(os.listdir("/proc/self/task"))


@pytest.mark.skipif(not os.path.isdir("/proc/self/task"), reason="counts this process's threads in /proc")
def test_a_picture_is_converted_on_the_thread_that_asks():
    """FFmpeg's scaler takes a thread per core for every picture unless it is told a number.  On a Raspberry
    Pi 5 that made 8.0 ms of CPU out of the 2.2 ms a 1080p picture needs, and no picture came sooner for it:
    a third of what a camera's window cost there."""
    frame = _picture()
    before = _threads()
    rgba = dispframe.rgba_array(frame)              # the scaler lives as long as the picture it was made for
    assert rgba.shape == (1080, 1920, 4)
    assert _threads() == before, "the conversion started threads of its own"
    assert util.CONV_THREADS == 1


def test_the_one_thread_gives_the_same_picture():
    for fmt in ("yuv420p", "yuvj420p", "nv12"):
        frame = _picture(fmt, 320, 240)
        assert np.array_equal(dispframe.rgba_array(frame), frame.to_ndarray(format="rgba")), fmt
    shown = dispframe.DisplayFrame.from_av_frame(_picture("yuv420p", 320, 240)).to_qimage()
    assert (shown.width(), shown.height()) == (320, 240)
