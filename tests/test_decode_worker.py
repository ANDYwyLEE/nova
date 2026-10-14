"""VideoWorker decoding without a camera (and without a GPU unless NOVA_TEST_GPU is set): hardware
fallback, size across reconnects, corrupt packets, the no-picture watchdog, the frame-rate clamp,
open-error logging and re-resolution."""
import logging
import os
import time
from fractions import Fraction
from types import SimpleNamespace

import av
import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication

import videoworker
from videoworker import VideoWorker, _VideoDecode, _Reconnect


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


# ---------------------------------------------------------------- corrupt packets
def test_corrupt_packets_are_skipped_not_reconnected(qapp, tmp_path, caplog):
    bad = _write_clip(tmp_path / "bad.nut", audio_s=3, corrupt_video=(10, 11, 12), corrupt_audio=(20, 21))
    w = _worker(str(bad))
    caplog.set_level(5)
    with av.open(str(bad)) as c:
        w._stream_loop(c, "bad")                         # must not raise
    assert w._session_frames >= 70, w._session_frames    # 75 frames, 3 damaged
    msgs = [r.getMessage() for r in caplog.records]
    assert any("rejected a video packet" in m for m in msgs)
    assert any("audio packet rejected" in m for m in msgs)
    assert not any("; reconnecting." in m for m in msgs)     # the _Reconnect wording; the clip's end logs "stream ended"
    assert w.sample_rate == 48000


def test_old_behaviour_reference_decode_raises(tmp_path):
    """The damaged packet really makes decode() raise (what used to end the whole session)."""
    bad = _write_clip(tmp_path / "bad.nut", corrupt_video=(10,))
    raised = 0
    with av.open(str(bad)) as c:
        for p in c.demux(video=0):
            if p.dts is None: continue
            try:
                p.stream.codec_context.decode(p)
            except av.error.InvalidDataError:
                raised += 1
    assert raised == 1


# ---------------------------------------------------------------- decoder fakes
class FakeCtx:
    def __init__(self, name="h264", out=None, fail=False):
        self.codec = SimpleNamespace(name=name)
        self.out = out if out is not None else (lambda pkt: [_frame(pkt.pts)])
        self.fail = fail
        self.is_open = False
        self.options = {}
        self.fed = []
        self.flushed = 0

    def open(self):
        self.is_open = True

    def flush_buffers(self):
        self.flushed += 1

    def decode(self, pkt):
        self.fed.append(pkt.pts)
        if self.fail:
            raise av.error.InvalidDataError(22, "Invalid data found when processing input")
        return self.out(pkt)


def _frame(pts):
    return SimpleNamespace(pts=pts, width=320, height=180)


def _pkt(pts, key=False):
    return SimpleNamespace(pts=pts, is_keyframe=key)


def _decode(worker, sw, hw_results):
    """_VideoDecode over a fake stream; hw_results: successive results of _open_hw_decoder."""
    calls = []
    def open_hw(base, codec_name):
        r = hw_results.pop(0) if hw_results else (None, None, "no more")
        calls.append(r)
        return r
    worker._open_hw_decoder = open_hw
    dec = _VideoDecode(worker, SimpleNamespace(codec_context=sw))
    return dec, calls


def test_hw_open_failure_falls_back_to_software_at_once(qapp, caplog):
    w = _worker(accel="cuda")
    sw = FakeCtx()
    dec, _ = _decode(w, sw, [(None, None, "h264_cuvid: Generic error in an external library")])
    caplog.set_level(logging.DEBUG)
    t0 = time.monotonic()
    dec.open()
    assert dec.ctx is sw and not dec.is_hw and sw.is_open
    assert w._hw_fail and w._hw_retry_at >= t0 + videoworker.HW_RETRY_S - 1
    assert [f.pts for f in dec.decode(_pkt(0, key=True))] == [0]
    assert any("No working HW decoder for --accel=cuda" in r.getMessage() and r.levelno == logging.WARNING
               for r in caplog.records)


def test_hw_without_frames_switches_to_software_in_session(qapp):
    w = _worker(accel="cuda")
    hw, sw = FakeCtx("h264_cuvid", out=lambda p: []), FakeCtx()
    dec, _ = _decode(w, sw, [(hw, "h264_cuvid", None)])
    dec.open()
    assert dec.is_hw
    pts = 0
    for gop in range(3):                                  # three GOPs without a picture
        for i in range(5):
            assert dec.decode(_pkt(pts, key=(i == 0))) == []
            pts += 1
    out = dec.decode(_pkt(pts, key=True))                 # 4th keyframe: software takes it
    assert not dec.is_hw and dec.ctx is sw and [f.pts for f in out] == [pts]
    assert "produced no frames after 3 keyframes" in w._hw_fail


def test_hw_decode_errors_switch_to_software(qapp):
    w = _worker(accel="cuda")
    hw, sw = FakeCtx("h264_cuvid", fail=True), FakeCtx()
    dec, _ = _decode(w, sw, [(hw, "h264_cuvid", None)])
    dec.open()
    dec.got = True                                        # it worked for a while, then broke
    for i in range(videoworker.MAX_DECODE_ERRORS - 1):
        assert dec.decode(_pkt(i)) == []
    assert dec.is_hw
    dec.decode(_pkt(100))                                 # the last straw: software waits for a keyframe
    assert not dec.is_hw
    assert dec.decode(_pkt(101)) == [] and sw.fed == []
    assert [f.pts for f in dec.decode(_pkt(102, key=True))] == [102]


def test_software_decode_errors_end_the_connection(qapp):
    w = _worker()
    dec, _ = _decode(w, FakeCtx(fail=True), [])
    dec.open()
    with pytest.raises(_Reconnect, match="in a row failed to decode"):
        for i in range(videoworker.MAX_DECODE_ERRORS):
            dec.decode(_pkt(i))


def test_hardware_retry_runs_alongside_software_and_takes_over(qapp):
    w = _worker(accel="cuda")
    sw = FakeCtx()
    hw = FakeCtx("h264_cuvid", out=lambda p: [] if p.pts < 12 else [_frame(p.pts - 2)])   # 2 frames of delay
    dec, calls = _decode(w, sw, [(None, None, "CUDA_ERROR_OUT_OF_MEMORY"), (hw, "h264_cuvid", None)])
    dec.open()
    assert not dec.is_hw
    shown = []
    for pts in range(10):
        shown += [f.pts for f in dec.decode(_pkt(pts, key=(pts % 10 == 0)))]
    assert dec.trial is None and len(calls) == 1          # not due yet
    w._hw_retry_at = 0                                    # the retry delay has passed
    for pts in range(10, 20):
        shown += [f.pts for f in dec.decode(_pkt(pts, key=(pts % 10 == 0)))]
    assert dec.is_hw and dec.ctx is hw and w._hw_fail is None
    assert shown == sorted(set(shown)) and shown == list(range(20 - 2)), shown   # no gap, no repeat
    assert sw.fed[-1] == 11 and hw.fed[0] == 10           # software stopped once hardware delivered


def test_failed_retry_backs_off(qapp):
    w = _worker(accel="cuda")
    dec, _ = _decode(w, FakeCtx(), [(None, None, "x"), (None, None, "x"), (None, None, "x")])
    dec.open()
    assert w._hw_backoff == videoworker.HW_RETRY_S
    w._hw_retry_at = 0
    dec.decode(_pkt(0, key=True))                         # trial cannot open
    assert w._hw_backoff == 2 * videoworker.HW_RETRY_S and not dec.is_hw


def test_rawvideo_with_accel_decodes_in_software(qapp, tmp_path):
    """--accel with a stream no hardware can decode (a webcam's rawvideo) used to fail av.open() every 10 s."""
    raw = _write_clip(tmp_path / "raw.nut", frames=10, codec="rawvideo")
    w = _worker(str(raw), accel="cuda")
    c = w._open_container(str(raw))
    assert c is not None
    with c:
        w._stream_loop(c, "raw")
    assert w._session_frames == 10 and w._hw_fail


@pytest.mark.skipif(not os.getenv("NOVA_TEST_GPU"), reason="set NOVA_TEST_GPU=<index> to test NVDEC on that GPU")
def test_cuvid_on_a_real_gpu(qapp, clip, caplog):
    gpu = os.environ["NOVA_TEST_GPU"]
    caplog.set_level(logging.INFO)
    w = _worker(str(clip), accel="cuda", gpu=gpu)
    w.rec_on = True
    with av.open(str(clip)) as c:
        w._stream_loop(c, "clip")
    # cuvid keeps a few frames queued at the end of a file (a live stream has no end)
    assert w._hw_fail is None and w._session_frames >= 70
    assert any("First frame (h264_cuvid, HW) pix_fmt=nv12" in r.getMessage() for r in caplog.records)
    queued = [w.rec_q.get_nowait() for _ in range(w.rec_q.qsize())]
    assert {f.format.name for typ, f in queued if typ == 1} == {"nv12"}
    bad = _worker(str(clip), accel="cuda", gpu="99")      # no such GPU: software at once, no reconnect
    with av.open(str(clip)) as c:
        bad._stream_loop(c, "clip")
    assert bad._hw_fail and bad._session_frames == 75


# ---------------------------------------------------------------- watchdog
class SlowContainer:
    """demux() paced like a live source."""
    def __init__(self, c, delay):
        self.c, self.delay, self.streams = c, delay, c.streams

    def demux(self, *s):
        for p in self.c.demux(*s):
            time.sleep(self.delay)
            yield p


def test_video_stall_with_audio_flowing_reconnects(qapp, tmp_path, monkeypatch, caplog):
    stalled = _write_clip(tmp_path / "stall.nut", frames=25, audio_s=4)     # 1 s of video, 4 s of audio
    monkeypatch.setattr(videoworker, "VIDEO_STALL_S", 0.3)
    w = _worker(str(stalled))
    caplog.set_level(logging.DEBUG)
    with av.open(str(stalled)) as c:
        w._stream_loop(SlowContainer(c, 0.01), "stall")
    assert any("no video for" in r.getMessage() and "reconnecting" in r.getMessage() for r in caplog.records)


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


# ---------------------------------------------------------------- open errors, re-resolution
def test_repeated_open_error_is_logged_once(qapp, tmp_path, caplog):
    w = _worker()
    caplog.set_level(logging.DEBUG)
    missing = str(tmp_path / "missing.mp4")
    for _ in range(3):
        assert w._open_container(missing) is None
    loud = [r for r in caplog.records if "open failed" in r.getMessage() and r.levelno >= logging.WARNING]
    quiet = [r for r in caplog.records if "open failed" in r.getMessage() and r.levelno == logging.DEBUG]
    assert len(loud) == 1 and len(quiet) == 2


def test_shortcut_is_resolved_again_after_failures(qapp, monkeypatch):
    import auth
    answers = ["rtsp://u:p@10.0.0.5:554/a", "rtsp://u:p@10.0.0.5:8554/a", None]
    seen = []
    def fake(uri, use_cache=True, should_stop=None, quiet=False):
        seen.append(use_cache)
        assert should_stop is not None and should_stop() is False
        return answers.pop(0)
    monkeypatch.setattr(auth, "resolve_input_uri", fake)
    w = _worker("onvif://u:p@10.0.0.5")
    assert w._resolve_source().endswith(":554/a")
    assert w._resolve_source().endswith(":554/a") and seen == [True]          # kept
    w._fail_streak = videoworker.RERESOLVE_AFTER
    w._resolve_t -= videoworker.RERESOLVE_MIN_S
    assert w._resolve_source().endswith(":8554/a") and seen == [True, False]  # asked the device again
    w._fail_streak = videoworker.RERESOLVE_AFTER
    w._resolve_t -= videoworker.RERESOLVE_MIN_S
    assert w._resolve_source().endswith(":8554/a")                            # failed: previous URL kept


def test_stop_reports_whether_the_thread_ended(qapp):
    w = _worker()
    assert w.stop() is True                                # never started
