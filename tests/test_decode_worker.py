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
    w._accel_never = lambda: None            # whatever GPUs the test machine has or lacks
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


def test_an_nvidia_index_that_does_not_open_is_answered_with_gpu_0(qapp, caplog):
    """NOVA_GPU_DEVICES = 0,1 carried to a machine with one card: the cameras dealt GPU 1 got "invalid
    device ordinal", decoded in software and recorded with libx264, 30 % of a core each instead of 2."""
    w = _worker(accel="cuda", gpu="1")
    seen = []
    def on(base, codec_name):
        seen.append(w.gpu_idx)
        return ("CTX", "h264_cuvid", None) if w.gpu_idx == 0 else (None, None, "h264_cuvid: Generic error")
    w._open_hw_decoder_on = on
    with caplog.at_level(logging.WARNING):
        assert w._open_hw_decoder(None, "h264") == ("CTX", "h264_cuvid", None)
    assert seen == [1, 0] and w.get_gpu_idx() == 0 and "GPU 1" in caplog.text and "using GPU 0" in caplog.text
    w = _worker(accel="cuda", gpu="1")                      # GPU 0 does not open either: the index stays
    w._open_hw_decoder_on = lambda base, codec_name: (None, None, "h264_cuvid: Generic error")
    assert w._open_hw_decoder(None, "h264") == (None, None, "h264_cuvid: Generic error") and w.get_gpu_idx() == 1
    w = _worker(accel="vaapi", gpu="1")                     # other devices are not NVIDIA indexes
    calls = []
    w._open_hw_decoder_on = lambda base, codec_name: calls.append(1) or (None, None, "no")
    w._open_hw_decoder(None, "h264")
    assert calls == [1]


def test_vaapi_looks_for_its_node_unless_gpu_names_one(qapp, monkeypatch, tmp_path):
    import util
    for node, vendor in (("renderD128", "0x10de"), ("renderD129", "0x1002")):
        (tmp_path / node / "device").mkdir(parents=True)
        (tmp_path / node / "device" / "vendor").write_text(vendor)
    monkeypatch.setattr(util, "DRM_ROOT", str(tmp_path))
    monkeypatch.delenv("NOVA_GPU_DEVICES", raising=False)
    w = _worker(accel="vaapi")
    assert w._hw_device("vaapi") == "/dev/dri/renderD129" and not w.gpu_given()
    w = _worker(accel="vaapi", gpu="0")
    assert w._hw_device("vaapi") == "/dev/dri/renderD128" and w.gpu_given()
    monkeypatch.setenv("NOVA_GPU_DEVICES", "0")
    assert _worker(accel="vaapi")._hw_device("vaapi") == "/dev/dri/renderD128"


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


# ---------------------------------------------------------------- parameter sets for Quick Sync
SPS_PPS = b"\x00\x00\x00\x01\x67\x4d\x00\x28\x9a" + b"\x00\x00\x00\x01\x68\xee\x3c\x80"
IDR, PFRAME = b"\x00\x00\x00\x01\x65\x88\x84\x00\x33", b"\x00\x00\x00\x01\x41\x9a\x24\x6c"


def _real_pkt(data, pts, key=False):
    p = av.Packet(data)
    p.pts = p.dts = pts
    p.time_base, p.duration, p.is_keyframe = Fraction(1, 90000), 9000, key
    return p


def test_parameter_sets_are_found_before_the_first_slice():
    has = videoworker._has_param_sets
    assert has(SPS_PPS + IDR, False) and not has(IDR, False) and not has(PFRAME, False)
    assert has(b"\x00\x00\x01\x09\xf0" + SPS_PPS + IDR, False)              # after an access unit delimiter
    assert has(b"\x00\x00\x01\x06\x05\x01\x80" + SPS_PPS + IDR, False)        # after an SEI
    assert not has(b"\x00\x00\x01\x06\x05\x01\x80" + IDR, False)
    assert not has(IDR + SPS_PPS, False)                                      # behind the slice: too late
    assert not has(b"", False) and not has(b"\x00\x00\x01", False)
    vps, sps, idr_w_radl = b"\x00\x00\x00\x01\x40\x01\x0c", b"\x00\x00\x00\x01\x42\x01\x01", b"\x00\x00\x00\x01\x26\x01\xaf"
    assert has(vps + sps + idr_w_radl, True) and not has(idr_w_radl, True)
    assert has(b"\x00\x00\x00\x01\x4e\x01\x05" + vps + idr_w_radl, True)      # prefix SEI (39) first


def _qsv_feed(qapp, name, extradata, packets, codec="h264"):
    """What decoder `name` is fed when the stream's extradata is `extradata`."""
    fed = []
    hw = FakeCtx(name, out=lambda p: fed.append((bytes(p), p.pts, p.dts, p.is_keyframe)) or [_frame(p.pts)])
    sw = FakeCtx(codec)
    sw.extradata = extradata
    dec, _ = _decode(_worker(accel="qsv"), sw, [(hw, name, None)])
    dec.open()
    for p in packets:
        dec.decode(p)
    return fed


def test_quick_sync_keyframes_get_the_parameter_sets_the_camera_left_out(qapp, caplog):
    """A camera that sends SPS/PPS in the RTSP session description only: h264_qsv answered "More data is
    required to decode header" to every packet and software decoding took over (Intel UHD 620)."""
    caplog.set_level(logging.DEBUG)
    fed = _qsv_feed(qapp, "h264_qsv", SPS_PPS, [_real_pkt(IDR, 0, True), _real_pkt(PFRAME, 9000),
                                               _real_pkt(IDR, 18000, True), _real_pkt(SPS_PPS + IDR, 27000, True)])
    assert fed[0] == (SPS_PPS + IDR, 0, 0, True)
    assert fed[1][0] == PFRAME
    assert fed[2] == (SPS_PPS + IDR, 18000, 18000, True)      # every keyframe: a decoder reset finds them too
    assert fed[3][0] == SPS_PPS + IDR                         # the camera's own: left alone
    assert len([r for r in caplog.records if "no parameter sets inside the stream" in r.getMessage()]) == 1


def test_parameter_sets_are_added_for_quick_sync_and_annex_b_only(qapp):
    pkts = lambda: [_real_pkt(IDR, 0, True)]
    assert _qsv_feed(qapp, "h264_cuvid", SPS_PPS, pkts())[0][0] == IDR         # cuvid reads the extradata itself
    avcc = b"\x01\x4d\x00\x28\xff\xe1\x00\x05" + SPS_PPS[4:9]
    assert _qsv_feed(qapp, "h264_qsv", avcc, pkts())[0][0] == IDR              # MP4-style: the decoder's own filter
    assert _qsv_feed(qapp, "h264_qsv", None, pkts())[0][0] == IDR              # nothing to add
    assert _qsv_feed(qapp, "vp9_qsv", SPS_PPS, pkts(), codec="vp9")[0][0] == IDR


def test_a_quick_sync_retry_gets_the_parameter_sets_too(qapp):
    w = _worker(accel="qsv")
    fed = []
    hw = FakeCtx("h264_qsv", out=lambda p: fed.append(bytes(p)) or [_frame(p.pts)])
    sw = FakeCtx(out=lambda p: [_frame(p.pts)])
    sw.extradata = SPS_PPS
    dec, _ = _decode(w, sw, [(None, None, "h264_qsv: device busy"), (hw, "h264_qsv", None)])
    dec.open()
    dec.decode(_real_pkt(IDR, 0, True))
    w._hw_retry_at = 0
    dec.decode(_real_pkt(IDR, 9000, True))                    # the trial starts on this keyframe
    assert dec.is_hw and fed == [SPS_PPS + IDR]


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


def test_accel_in_build_looks_at_the_decoders(monkeypatch):
    """The arm64 wheel has no NVIDIA and no Intel decoder: `nova doctor` answered "--accel cuda has a
    decoder in this FFmpeg build" from a table of names (seen on GitHub's ARM runner)."""
    monkeypatch.setattr(videoworker, "HWACCELS", ["drm"])
    monkeypatch.setattr(videoworker, "CODECS_AVAILABLE", {"h264", "h264_v4l2m2m", "hevc_v4l2m2m", "libx264"})
    assert not videoworker.accel_in_build("cuda") and not videoworker.accel_in_build("qsv")
    assert videoworker.accel_in_build("v4l2m2m") and videoworker.accel_in_build("drm") and videoworker.accel_in_build("")
    assert not videoworker.accel_in_build("vaapi") and not videoworker.accel_in_build("nonsense")
    assert videoworker.accel_supported("cuda")           # still a name Nova tries: software when it does not open
    monkeypatch.setattr(videoworker, "CODECS_AVAILABLE", {"h264_cuvid"})
    assert videoworker.accel_in_build("cuda") and videoworker.accel_in_build("nvdec")


def test_audio_that_stops_while_video_flows_reconnects_when_it_is_used(qapp, tmp_path, monkeypatch, caplog):
    """Two of five cameras in a 12-hour soak stopped sending audio on their RTSP connection after 10 and 23
    minutes (video alone on the wire, audio again for a new client): the recordings went on without sound
    until the wall was stopped, and the log said nothing."""
    clip = _write_clip(tmp_path / "quiet.nut", frames=100, audio_s=1)       # 4 s of video, 1 s of audio
    monkeypatch.setenv("NOVA_AUDIO_STALL", "0.5")
    caplog.set_level(logging.DEBUG)

    def play(**on):
        w = _worker(str(clip))
        for k, v in on.items():
            setattr(w, k, v)
        del caplog.records[:]
        with av.open(str(clip)) as c:
            w._stream_loop(SlowContainer(c, 0.01), "quiet")
        return w, any("no audio for" in r.getMessage() and "reconnecting" in r.getMessage() for r in caplog.records)

    w, said = play(rec_on=True)
    assert said and w._a_stall_wait == 1.0             # dropped early: the next wait is twice as long
    assert play(audio_on=True)[1] and play(stream_on=True)[1]
    assert not play()[1]                               # nobody listens and nothing records: no reconnect
    w = _worker(str(clip)); w.rec_on = True; w.ffopts.no_audio = True      # recording with -an
    with av.open(str(clip)) as c:
        del caplog.records[:]
        w._stream_loop(SlowContainer(c, 0.01), "quiet")
    assert not any("no audio for" in r.getMessage() for r in caplog.records)
    monkeypatch.setenv("NOVA_AUDIO_STALL", "0")        # switched off
    assert not play(rec_on=True)[1]
    monkeypatch.setenv("NOVA_AUDIO_STALL", "nonsense")
    assert videoworker._audio_stall_s() == videoworker.AUDIO_STALL_S


def test_a_stream_without_audio_never_waits_for_it(qapp, tmp_path, monkeypatch, caplog):
    silent = _write_clip(tmp_path / "silent.nut", frames=50)                # video alone
    monkeypatch.setenv("NOVA_AUDIO_STALL", "0.01")
    caplog.set_level(logging.DEBUG)
    w = _worker(str(silent)); w.rec_on = True
    with av.open(str(silent)) as c:
        assert not c.streams.audio
        w._stream_loop(SlowContainer(c, 0.005), "silent")
    assert not any("no audio for" in r.getMessage() for r in caplog.records)


class Pauses(SlowContainer):
    """A camera that sends nothing for `pause` seconds before its video packet number `at`, then goes on."""
    def __init__(self, c, delay, at, pause):
        super().__init__(c, delay)
        self.at, self.pause = at, pause

    def demux(self, *s):
        n = 0
        for p in super().demux(*s):
            if p.stream.type == "video":
                n += 1
                if n == self.at:
                    time.sleep(self.pause)
            yield p


def test_picture_and_sound_that_come_back_on_their_connection_are_in_the_log(qapp, tmp_path, monkeypatch, caplog):
    """A camera paused sound and picture for 8 s and went on by itself on the same connection. Nothing
    reconnected and no watchdog was due, so the recording had a hole and the log not a word about it."""
    clip = _write_clip(tmp_path / "pause.nut", frames=100, audio_s=4)
    monkeypatch.setattr(videoworker, "GAP_SAID_S", 0.3)
    caplog.set_level(logging.DEBUG)

    def play(pause):
        w = _worker(str(clip))
        del caplog.records[:]
        with av.open(str(clip)) as c:
            w._stream_loop(Pauses(c, 0.005, at=40, pause=pause), "pause")
        return [r.getMessage() for r in caplog.records if "on this connection and is back" in r.getMessage()]

    said = play(0.7)
    picture = [m for m in said if "the picture was gone for 1 s" in m]
    assert len(picture) == 1 and "the longest wait for one was 1 s" in picture[0], said
    assert any("the sound was gone for 1 s" in m for m in said), said
    assert play(0.0) == []


class ClockRestarts(SlowContainer):
    """A camera whose clock starts again: from the video packet number `at` on, the video timestamps
    are `back` seconds lower and no audio packet comes any more."""
    def __init__(self, c, delay, at, back, audio_goes=True):
        super().__init__(c, delay)
        self.at, self.back, self.audio_goes = at, back, audio_goes

    def demux(self, *s):
        n = 0
        for p in super().demux(*s):
            if p.stream.type == "video":
                n += 1
                if n >= self.at and p.pts is not None:
                    shift = int(self.back / p.time_base)
                    p.pts -= shift
                    p.dts -= shift
            elif n >= self.at and self.audio_goes:
                continue
            yield p


def test_audio_that_stops_when_the_camera_s_clock_starts_again_is_fetched_within_seconds(qapp, tmp_path, monkeypatch, caplog):
    """Four cameras of one make count time in 1/360000 s in 32 bits. Every 3 h 18 min 50 s the counter
    starts again: the video timestamps go back by 2^30 (11930 s) and the camera sends no more audio to
    the sessions that exist (captured on the wire). The watchdog waited its 30 s each time, seven times a
    day per camera; timestamps that go back this far say at once what has happened."""
    clip = _write_clip(tmp_path / "wrap.nut", frames=100, audio_s=4)        # 4 s of both
    monkeypatch.setenv("NOVA_AUDIO_STALL", "30")
    monkeypatch.setattr(videoworker, "AUDIO_AFTER_BACK_S", 0.4)
    caplog.set_level(logging.DEBUG)

    def play(**kw):
        w = _worker(str(clip)); w.rec_on = True
        del caplog.records[:]
        t = time.monotonic()
        with av.open(str(clip)) as c:
            w._stream_loop(ClockRestarts(c, 0.01, **kw), "wrap")
        said = [r.getMessage() for r in caplog.records if "no audio for" in r.getMessage()]
        return w, said, time.monotonic() - t

    w, said, took = play(at=30, back=2 ** 30 / 90000)
    assert said and "since the video timestamps went back 11930 s" in said[0]   # (30 s would outlast the clip)
    assert w._a_stall_wait == 60.0                     # it had its audio for a second only: asked less often
    assert any("the video timestamps went back 11930 s" in r.getMessage() for r in caplog.records)
    assert not play(at=30, back=2 ** 30 / 90000, audio_goes=False)[1]      # the audio goes on: nothing to fetch
    assert not play(at=30, back=1.0)[1]                                    # a second back is no new clock: the usual wait
    monkeypatch.setenv("NOVA_AUDIO_STALL", "0")                            # switched off stays switched off
    assert not play(at=30, back=2 ** 30 / 90000)[1]


# ---------------------------------------------------------------- a CPU that one thread is not enough for
def _feed(load, seconds, worked, every=None):
    """Video packets for `seconds`, each taking `worked` s, one every `every` s (None: as fast as they
    are done, which is what a thread that is behind sees).  The first verdict and when it came."""
    t = 0.0
    while t < seconds:
        verdict = load.add(t, worked, 1)
        if verdict is not None:
            return verdict, t + worked
        t += max(worked, every or 0.0)
    return None, t


def test_load_tells_a_thread_that_falls_behind_from_one_that_is_merely_busy():
    # an Atom D525 on a 1080p camera at 25 fps: 58 ms a picture, 17 a second come out
    verdict, when = _feed(videoworker._Load(25), 5, 0.058)
    assert verdict == pytest.approx(0.058, rel=0.02) and 2.0 <= when < 2.2
    # the same camera on a machine that decodes it in 5 ms: the thread waits most of the time
    assert _feed(videoworker._Load(25), 10, 0.005, every=0.04) == (None, pytest.approx(10, abs=0.1))
    # a file, or the burst a camera opens with: busy all the time, and far ahead of the frame rate
    assert _feed(videoworker._Load(25), 10, 0.004)[0] is None
    # a 10 fps camera whose pictures take 85 ms: it keeps up, with little to spare; at 95 ms it is at the limit
    assert _feed(videoworker._Load(10), 10, 0.085, every=0.1)[0] is None
    assert _feed(videoworker._Load(10), 10, 0.095, every=0.1)[0] == pytest.approx(0.095, rel=0.02)


def test_load_judges_the_stretch_a_dropped_connection_cut_short():
    load = videoworker._Load(25)
    assert _feed(load, 1.5, 0.058)[0] is None            # the camera ended the connection after 1.5 s
    assert load.end(1.5) == pytest.approx(0.058, rel=0.05)
    assert load.end(1.6) is None                          # judged once
    brief = videoworker._Load(25)
    _feed(brief, 0.5, 0.058)
    assert brief.end(0.5) is None                         # too short to say anything
    assert videoworker._Load(25).end(3.0) is None         # no picture at all: not this question


def test_a_decoder_that_falls_behind_gets_frame_threads_and_says_so(qapp, monkeypatch, caplog):
    monkeypatch.setattr(videoworker.os, "cpu_count", lambda: 4)
    caplog.set_level(logging.WARNING)
    w = _worker()
    assert w._slow(None, 25) is None and w._sw_threads == 0
    said = w._slow(0.058, 25)        # 1.45 threads' worth of work, and half as much again
    assert w._sw_threads == 3 and "58 ms" in said and "every 40 ms" in said and "gets 3 threads" in said
    assert "gets 4 threads" in w._slow(0.045, 25) and w._sw_threads == 4       # then twice as many, up to the cores
    assert w._slow(0.045, 25) is None and w._slow(0.045, 25) is None          # nothing left to add: one line, once
    lines = [r.getMessage() for r in caplog.records if "does not keep up" in r.getMessage()]
    assert len(lines) == 1 and "45 ms" in lines[0] and "on 4 threads" in lines[0], lines
    assert "second (smaller) stream" in lines[0] and "--opengl" not in lines[0]     # headless: nothing is converted


def test_threads_named_with_ffmpeg_are_left_as_they_are(qapp, monkeypatch, caplog):
    monkeypatch.setattr(videoworker.os, "cpu_count", lambda: 8)
    caplog.set_level(logging.WARNING)
    w = _worker(headless=False)
    w.dec_opts = {"threads": "2"}
    assert w._slow(0.058, 25) is None and w._sw_threads == 0
    lines = [r.getMessage() for r in caplog.records if "does not keep up" in r.getMessage()]
    assert len(lines) == 1 and "--opengl" in lines[0] and " threads" not in lines[0].split("What needs less")[0]


class RealTime(SlowContainer):
    """demux() as a camera sends: packet k is there at k * delay, whether the reader was ready for it or not."""
    def demux(self, *s):
        t0 = time.monotonic()
        for k, p in enumerate(self.c.demux(*s)):
            wait = t0 + k * self.delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            yield p


def test_a_connection_that_falls_behind_comes_back_with_frame_threads(qapp, tmp_path, monkeypatch, caplog):
    """On an old CPU one thread could not decode a 1080p camera at 25 fps: the camera dropped packets and,
    every few seconds, the connection, while three of four cores had nothing to do."""
    clip = _write_clip(tmp_path / "busy.nut", frames=75)
    monkeypatch.setattr(videoworker, "LOAD_JUDGE_S", 0.4)
    monkeypatch.setattr(videoworker.os, "cpu_count", lambda: 4)
    caplog.set_level(logging.INFO)
    real = _VideoDecode.decode

    def slow(self, packet):                  # 50 ms a picture, and the camera sends one every 40 ms
        time.sleep(0.05)
        return real(self, packet)

    w = _worker(str(clip))
    monkeypatch.setattr(_VideoDecode, "decode", slow)
    with av.open(str(clip)) as c:
        w._stream_loop(RealTime(c, 0.04), "busy")
    said = [r.getMessage() for r in caplog.records]
    n = w._sw_threads                        # 2 by the 50 ms; a slow test machine adds to them (a macOS runner: 145 ms)
    assert 2 <= n <= 4, said
    assert any(f"the decoder gets {n} threads from now on" in m and "reconnecting" in m for m in said), said
    assert w._session_frames < 40            # it did not stay to the end of the clip

    monkeypatch.setattr(_VideoDecode, "decode", real)
    monkeypatch.setattr(videoworker, "LOAD_JUDGE_S", 60)      # the next connection is not judged here
    del caplog.records[:]
    with av.open(str(clip)) as c:
        w._stream_loop(RealTime(c, 0.04), "busy")
    said = [r.getMessage() for r in caplog.records]
    assert any(f"First frame (h264, SW, {n} threads)" in m for m in said), said
    assert w._sw_threads == n and not any("threads from now on" in m for m in said), said


def test_a_frame_threaded_decoder_is_drained_before_it_is_let_go(qapp, tmp_path):
    """PyAV frees a decoder under the interpreter lock while FFmpeg waits for the frame threads: a thread
    that still decodes and logs then stops the process for good.  release() leaves them without work."""
    clip = _write_clip(tmp_path / "threads.nut", frames=50)
    w = _worker(str(clip))
    w._sw_threads = 4
    with av.open(str(clip)) as c:
        dec = _VideoDecode(w, c.streams.video[0])
        dec.open()
        assert dec.threads == 4 and dec.base.thread_type & videoworker.ThreadType.FRAME and dec.base.thread_count == 4
        got = 0
        for i, p in enumerate(c.demux(video=0)):
            if p.dts is None or i >= 20:
                break
            got += len(dec.decode(p))
        assert got < 20                                   # pictures are still inside the decoder's threads
        dec.release()
        with pytest.raises(av.error.EOFError):            # drained: it takes no more packets
            dec.base.decode(p)
        dec.release()                                     # and a second call is harmless


def test_a_single_core_has_no_threads_to_add(qapp, monkeypatch, caplog):
    monkeypatch.setattr(videoworker.os, "cpu_count", lambda: 1)
    caplog.set_level(logging.WARNING)
    w = _worker()
    assert w._slow(0.058, 25) is None and w._sw_threads == 0          # no reconnect for nothing
    assert sum("does not keep up" in r.getMessage() for r in caplog.records) == 1


# ---------------------------------------------------------------- an --accel that cannot work on this machine
def _said(caplog, words):
    return [r.getMessage() for r in caplog.records if words in r.getMessage()]


def test_an_accel_the_ffmpeg_build_lacks_is_said_once_and_decoded_in_software(qapp, clip, monkeypatch, caplog):
    """`--accel vaapi` with the wheel from PyPI (a cameras.ini carried over, an AMD or an old NVIDIA GPU):
    the camera ended with `--accel=vaapi is not supported by this FFmpeg/PyAV build`, a black window."""
    monkeypatch.setattr(videoworker, "HWACCELS", ["cuda", "qsv", "drm", "amf"])
    assert videoworker.accel_supported("vaapi") and videoworker.accel_supported("vdpau")       # known names
    assert not videoworker.accel_supported("nonsense")
    caplog.set_level(logging.INFO)
    w = _worker(str(clip), accel="vaapi")
    del w._accel_never                                   # the real one
    w._open_hw_decoder = lambda *a: pytest.fail("nothing is there to open")
    for _ in range(2):                                   # two connections
        with av.open(str(clip)) as c:
            w._stream_loop(c, "clip")
    said = _said(caplog, "cannot work here")
    assert len(said) == 1, said
    assert "--accel=vaapi cannot work here: this FFmpeg build was made without vaapi (it has: cuda, qsv, drm, amf; " \
           "docs/custom-ffmpeg.md builds one that has it). Decoding in software (h264)." in said[0]
    assert len(_said(caplog, "First frame (h264, SW)")) == 2 and not _said(caplog, "tried again")
    assert not w._hw_retry_due()                         # and never tried: the build does not change


def test_an_accel_this_machine_has_no_gpu_for_is_said_once(qapp, clip, monkeypatch, caplog):
    monkeypatch.setattr(videoworker, "accel_obstacle", lambda a: "the NVIDIA card runs on the nouveau driver, and "
                        "NVIDIA's decoder (NVDEC) comes with NVIDIA's own driver only" if a == "cuda" else None)
    monkeypatch.setattr(videoworker, "accel_in_build", lambda a: True)
    caplog.set_level(logging.INFO)
    w = _worker(str(clip), accel="cuda")
    del w._accel_never
    w._open_hw_decoder = lambda *a: pytest.fail("nothing is there to open")
    with av.open(str(clip)) as c:
        w._stream_loop(c, "clip")
    said = _said(caplog, "cannot work here")
    assert len(said) == 1 and "--accel=cuda cannot work here: the NVIDIA card runs on the nouveau driver" in said[0], said
    assert _said(caplog, "First frame (h264, SW)") and not w._hw_retry_due()


def test_a_gpu_decoder_that_does_not_keep_up_is_left_for_software(qapp, caplog):
    """An NVIDIA ION of 2010 decoding a 15 Mbit/s camera through VA-API: 57 ms a picture, one every 40 ms.
    The camera ended the connection every six seconds and the log had nothing but `stream ended`."""
    caplog.set_level(logging.WARNING)
    w = _worker(accel="vaapi")
    dec = SimpleNamespace(is_hw=True, name="h264 + vaapi hwaccel", codec_name="h264")
    said = w._slow(0.057, 25, dec)
    assert said == "the GPU's decoder does not keep up with the camera: software decoding"
    first = [r.getMessage() for r in caplog.records][-1]
    assert "No working HW decoder for --accel=vaapi (the GPU's decoder does not keep up with this camera: a picture " \
           "takes 57 ms through h264 + vaapi hwaccel and the camera sends one every 40 ms)" in first
    assert "hardware is tried again in 5 min" in first and w._sw_threads == 0      # once may be a moment's load
    assert w._slow(0.060, 25, dec)
    second = [r.getMessage() for r in caplog.records][-1]
    assert "--accel=vaapi cannot work here: the GPU's decoder does not keep up" in second and "60 ms" in second
    assert not w._hw_retry_due() and w._hw_retry_at == float("inf")               # twice: software for good
    assert w._slow(None, 25, dec) is None


def test_vdpau_without_an_x_display_is_said_once(qapp, monkeypatch):
    """`nova record --accel vdpau` over ssh: `Cannot open the X11 display`, and a new try every few minutes."""
    monkeypatch.setattr(videoworker, "accel_in_build", lambda a: True)
    monkeypatch.setattr(videoworker, "accel_obstacle", lambda a: None)
    w = _worker(accel="vdpau")
    del w._accel_never
    monkeypatch.delenv("DISPLAY", raising=False)
    assert "VDPAU decodes through an X display" in w._accel_never()
    monkeypatch.setenv("DISPLAY", ":0")
    assert w._accel_never() is None
    w.accel = "vaapi"
    monkeypatch.delenv("DISPLAY")
    assert w._accel_never() is None
