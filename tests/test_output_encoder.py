"""Encoder opens at the real size: fallback to the next candidate, and no second open of a failed context
(in PyAV 19 an encode() or a header write after a failed avcodec_open2() segfaults)."""
import logging
import os
import subprocess
import sys
import textwrap
from fractions import Fraction
from types import SimpleNamespace

import av
import numpy as np
import pytest

import avwriter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def frames(n, w=320, h=180, rate=25):
    for i in range(n):
        f = av.VideoFrame.from_ndarray(np.full((h, w, 3), (i * 5) % 256, np.uint8), format="rgb24")
        f.pts, f.time_base = i, Fraction(1, rate)
        yield f


@pytest.fixture
def manager(monkeypatch):
    """A fresh EncoderManager whose probe 'passes' mpeg4 first: mpeg4 opens at the probe's 1/30 time base
    but refuses AVWriter's 1/90000, like NVENC passing the 640x360 probe and failing at full size."""
    monkeypatch.setattr(avwriter.EncoderManager, "_instance", None)
    real = avwriter.EncoderManager._perform_hw_test
    calls = []

    def probe(self, *args):
        calls.append(args)
        return "mpeg4" if len(calls) == 1 else real(self, *args)

    monkeypatch.setattr(avwriter.EncoderManager, "_perform_hw_test", probe)
    yield calls


def test_an_encoder_that_fails_at_the_real_open_falls_back(tmp_path, manager, caplog):
    caplog.set_level(logging.INFO)
    out = tmp_path / "a.mp4"
    w = avwriter.AVWriter(None, path=str(out), width=320, height=180, fps=25,
                          enc_v_opts={"preset": "ultrafast"}, audio_enable=False)
    assert w.v_encoder == "mpeg4"
    for f in frames(50):
        w.write_video(f)
    w.close()
    assert w.v_encoder == "libx264"
    with av.open(str(out)) as c:
        assert c.streams.video[0].codec_context.name == "h264"
        assert sum(1 for _ in c.decode(video=0)) == 50          # the frame that failed was not lost
    warns = [r for r in caplog.records if "does not open" in r.getMessage()]
    assert len(warns) == 1 and warns[0].levelno == logging.WARNING and "using libx264" in warns[0].getMessage()

    # the next writer skips mpeg4 without trying it again, and says nothing more
    w2 = avwriter.AVWriter(None, path=str(tmp_path / "b.mp4"), width=320, height=180, fps=25,
                           enc_v_opts={"preset": "ultrafast"}, audio_enable=False)
    assert w2.v_encoder == "libx264"
    w2.close()
    assert len([r for r in caplog.records if "does not open" in r.getMessage()]) == 1


def test_a_skipped_encoder_is_probed_again_after_its_penalty(manager, monkeypatch):
    mgr = avwriter.EncoderManager()
    args = (None, None, "h264", {}, ("h264",))
    assert mgr.get_encoder(*args) == "mpeg4"
    assert mgr.reject("mpeg4") is True and mgr.reject("mpeg4") is False      # warn once
    assert mgr.get_encoder(*args) == "libx264"
    now = avwriter.time.monotonic()
    monkeypatch.setattr(avwriter.time, "monotonic", lambda: now + avwriter.FAILED_RETRY_S + 1)
    assert "mpeg4" not in mgr._skipped()


SCRIPT = textwrap.dedent("""
    import logging, sys, numpy as np, av, logsync, avwriter
    from fractions import Fraction
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    # every probe 'passes' mpeg4, which never opens at 1/90000: no candidate is left
    real_probe = avwriter.EncoderManager._perform_hw_test
    avwriter.EncoderManager._perform_hw_test = lambda self, *a: "mpeg4"
    def frame(i):
        f = av.VideoFrame.from_ndarray(np.zeros((180, 320, 3), np.uint8), format="rgb24")
        f.pts, f.time_base = i, Fraction(1, 25)
        return f
    w = avwriter.AVWriter(None, path=sys.argv[1] + "/v.mp4", width=320, height=180, fps=25, audio_enable=False)
    refused = 0
    for i in range(5):
        try: w.write_video(frame(i))
        except avwriter.OutputOpenError as e: refused += 1; print("refused:", e, flush=True)
    w.close()
    print("video refused", refused, flush=True)

    # AAC cannot open at 6000 Hz: the writer records video without an audio track
    avwriter.EncoderManager._instance = None
    avwriter.EncoderManager._perform_hw_test = real_probe
    w = avwriter.AVWriter(None, path=sys.argv[1] + "/a.mp4", width=320, height=180, fps=25, encoder="libx264",
                          sample_rate=6000, channels=1, enc_v_opts={"tune": "zerolatency"})
    for i in range(25):
        w.write_video(frame(i))
        a = av.AudioFrame.from_ndarray(np.zeros((1, 240), np.int16), format="s16", layout="mono")
        a.sample_rate, a.pts, a.time_base = 6000, i * 240, Fraction(1, 6000)
        w.write_audio(a)
    w.close()
    print("audio track", w.a_stream, "video frames", w.v_frames, flush=True)
""")


def test_failed_encoder_opens_never_crash(tmp_path):
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8")     # not the ANSI code page (cp949, ...)
    out = subprocess.run([sys.executable, "-c", SCRIPT, str(tmp_path)], cwd=ROOT, env=env,
                         capture_output=True, encoding="utf-8", errors="replace", timeout=120)
    assert out.returncode == 0, f"exit {out.returncode}\n{out.stdout}\n{out.stderr}"
    assert "video refused 5" in out.stdout and "does not open at 320x180" in out.stdout, out.stdout
    assert "audio track None video frames 25" in out.stdout, out.stdout
    if os.name != "nt":          # Windows decides "no audio track" before trying AAC: same result, no warning
        assert "audio encoder aac does not open at 6000 Hz" in out.stdout, out.stdout
    with av.open(str(tmp_path / "a.mp4")) as c:
        assert [s.type for s in c.streams] == ["video"]


def test_a_user_encoder_the_target_cannot_carry_is_not_used(monkeypatch, caplog, tmp_path):
    # -e hevc_nvenc in [defaults] must not turn an RTMP stream into HEVC (YouTube shows "no data")
    monkeypatch.setattr(avwriter.EncoderManager, "_instance", None)
    monkeypatch.setattr(avwriter, "_WARNED", set())
    mgr = avwriter.EncoderManager()
    assert "hevc_nvenc" not in mgr._candidates("cuda", "hevc_nvenc", "hevc", ("h264",))
    assert mgr._candidates("cuda", "hevc_nvenc", "hevc", ("h264", "hevc"))[0] == "hevc_nvenc"
    caplog.set_level(logging.WARNING)
    for name in ("a.flv", "b.flv"):
        w = avwriter.AVWriter(None, path=str(tmp_path / name), width=320, height=180, fps=25,
                              encoder="hevc_nvenc", audio_enable=False)
        assert avwriter._family(w.v_encoder) == "h264"
        w.close()
    warns = [r.getMessage() for r in caplog.records if "carries" in r.getMessage()]
    assert len(warns) == 1 and "-e hevc_nvenc: flv carries h264 only; using" in warns[0], warns


def test_media_foundation_means_the_hardware_encoder_with_a_bitrate():
    """*_mf sits in the list of hardware encoders, but FFmpeg takes Microsoft's software MFT unless
    hw_encoding=1 is set, and Media Foundation refuses a stream without a bitrate (found on an AMD PC)."""
    assert avwriter._mf_opts("h264_mf", {}) == {"hw_encoding": "1"}
    assert avwriter._mf_opts("hevc_mf", {"hw_encoding": "0", "b": "4M"}) == {"hw_encoding": "0", "b": "4M"}
    assert avwriter._mf_opts("h264_amf", {"usage": "transcoding"}) == {"usage": "transcoding"}
    assert avwriter._mf_opts("libx264", {}) == {}
    assert avwriter._default_bitrate(1920, 1080, 25) == 5_184_000
    assert avwriter._default_bitrate(1920, 1080, 10) == 2_073_600
    assert avwriter._default_bitrate(320, 180, 5) == 500_000


@pytest.mark.skipif("h264_mf" not in av.codecs_available, reason="no Media Foundation encoder in this FFmpeg (Windows only)")
def test_h264_mf_opens_for_a_real_recording(tmp_path, caplog, monkeypatch):
    """h264_mf passed the probe (a bare context has 200 kbit/s) and then failed at every real open with
    "could not set output type": add_stream leaves the bitrate at 0. With the software MFT here
    (hw_encoding 0), since a machine without a GPU has no hardware one."""
    if avwriter.EncoderManager._try_open("h264_mf", {"hw_encoding": "0"}) is not None:
        pytest.skip("this Windows has no H.264 encoder MFT (a Server edition without Media Foundation)")
    monkeypatch.setattr(avwriter.EncoderManager, "_instance", None)
    caplog.set_level(logging.INFO)
    out = tmp_path / "mf.mp4"
    w = avwriter.AVWriter(None, path=str(out), width=640, height=360, fps=25, encoder="h264_mf",
                          enc_v_opts={"hw_encoding": "0"}, audio_enable=False)
    for f in frames(50, 640, 360):
        w.write_video(f)
    w.close()
    assert w.v_encoder == "h264_mf"
    assert not [r.getMessage() for r in caplog.records if "does not open" in r.getMessage()]
    with av.open(str(out)) as c:
        assert c.streams.video[0].codec_context.name == "h264"
        assert sum(1 for _ in c.decode(video=0)) == 50


# ---------------------------------------------------------------- dts
def test_quick_sync_gets_no_b_frames_unless_asked(tmp_path, monkeypatch):
    """Quick Sync reorders by default and computes dts from pts and the nominal frame rate; with a camera's
    real frame times that dts runs backwards (Intel UHD 620: h264_qsv and hevc_qsv, not with -bf 0)."""
    if "h264_qsv" not in av.codecs_available:
        pytest.skip("no h264_qsv in this FFmpeg build")
    monkeypatch.setattr(avwriter.EncoderManager, "_instance", None)
    monkeypatch.setattr(avwriter.EncoderManager, "_perform_hw_test", lambda self, *a: "h264_qsv")

    def options(**enc):
        w = avwriter.AVWriter("qsv", path=str(tmp_path / "q.mp4"), width=320, height=180, fps=10,
                              enc_v_opts=enc, audio_enable=False)
        try:
            return w.v_encoder, dict(w.v_stream.codec_context.options)
        finally:
            w.open_failed = "not opened in this test"         # nothing to flush
            w.close()

    assert options() == ("h264_qsv", {"bf": "0"})
    assert options(bf="2")[1]["bf"] == "2"                    # the user's choice stays


def test_a_dts_that_does_not_increase_costs_no_frame(tmp_path, caplog):
    """The mp4 muxer answers EINVAL to such a packet; the writer moves the dts instead of losing the frame."""
    caplog.set_level(logging.WARNING)
    avwriter.AVWriter._dts_warned.discard("libx264")
    out = tmp_path / "d.mp4"
    w = avwriter.AVWriter(None, path=str(out), width=320, height=180, fps=25, encoder="libx264",
                          enc_v_opts={"preset": "ultrafast", "bf": "0", "tune": "zerolatency"}, audio_enable=False)
    real, seen = w._write, []

    def write(packet):                                        # an encoder whose dts stands still now and then
        if packet.stream is w.v_stream:
            seen.append(packet.dts)
            if len(seen) in (10, 30):
                packet.dts = seen[-2]
        real(packet)

    w._write = write
    for f in frames(50):
        w.write_video(f)
    w.close()
    assert w._dts_fixed == 2
    with av.open(str(out)) as c:
        pk = [p for p in c.demux(video=0) if p.dts is not None]
        assert len(pk) == 50 and all(b.dts > a.dts for a, b in zip(pk, pk[1:]))
    with av.open(str(out)) as c:
        assert sum(1 for _ in c.decode(video=0)) == 50
    assert len([r for r in caplog.records if "does not increase" in r.getMessage()]) == 1


def test_an_encoder_asked_for_that_does_not_open_is_named(monkeypatch, caplog):
    """`-e h264_vaapi` on a build that cannot feed it, `-e hevc_nvenc` without the driver: the recording went
    to another encoder (in the end libx264, at ten times the CPU) with nothing above DEBUG to say so."""
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(avwriter.EncoderManager, "_instance", None)
    mgr = avwriter.EncoderManager()
    assert mgr.get_encoder(None, "h264_nothere", "h264", {}, ("h264",)) == "libx264"
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1 and "-e h264_nothere does not open here" in warns[0] and "using libx264" in warns[0]
    caplog.clear()
    assert mgr.get_encoder(None, "h264_nothere", "h264", {}, ("h264",)) == "libx264"     # cached: said once
    assert mgr.get_encoder(None, "libx264", "h264", {}, ("h264",)) == "libx264"          # the one asked for: nothing
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_nvenc_on_a_card_without_its_kernel_driver_says_so(monkeypatch, caplog):
    """NVIDIA's module not loaded (Secure Boot refused it on a new mainboard): `-e h264_nvenc does not open
    here (Unknown error occurred); using h264_qsv`."""
    import types
    said = "the NVIDIA card has no kernel driver: NVIDIA's driver is not installed, or its module is not loaded; nova doctor says which"
    def refuse(*a, **k):
        raise OSError("[Errno 1313558101] Unknown error occurred: 'avcodec_open2(\"h264_nvenc\", {})'")
    monkeypatch.setattr(avwriter, "av", types.SimpleNamespace(Codec=lambda name, mode: name,
                                                              CodecContext=types.SimpleNamespace(create=refuse)))
    monkeypatch.setattr(avwriter, "nvidia_driver_missing", lambda: said)
    assert avwriter.EncoderManager._try_open("h264_nvenc", {}) == said
    assert avwriter.EncoderManager._try_open("hevc_qsv", {}) == "Unknown error occurred"      # not NVIDIA's: FFmpeg's words
    monkeypatch.setattr(avwriter, "nvidia_driver_missing", lambda: None)
    assert avwriter.EncoderManager._try_open("h264_nvenc", {}) == "Unknown error occurred"    # a card with its driver
    # the whole sentence is in the line about -e
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(avwriter.EncoderManager, "_instance", None)
    monkeypatch.setattr(avwriter.EncoderManager, "_try_open", staticmethod(
        lambda name, opts, gpu_idx=None: said if "nvenc" in name else None))
    assert avwriter.EncoderManager().get_encoder(None, "h264_nvenc", "h264", {}, ("h264",)) == "libx264"
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warns == [f"[EncoderManager] ⚠️ -e h264_nvenc does not open here ({said}); using libx264"]


def test_an_accel_without_an_encoder_that_opens_is_named(monkeypatch, caplog):
    """`--accel vaapi` on a GPU that decodes and cannot encode (an NVIDIA ION on nouveau): the recording
    went to libx264, which that machine's CPU could not keep up with, and nothing said that a hardware
    encoder had been looked for."""
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(avwriter, "IS_PI", False)
    monkeypatch.setattr(avwriter, "WINDOWS", False)
    monkeypatch.setattr(avwriter, "MSYS", False)
    monkeypatch.setattr(avwriter, "MAC", False)
    monkeypatch.setattr(avwriter.EncoderManager, "_instance", None)
    opened = {"libx264"}
    monkeypatch.setattr(avwriter.EncoderManager, "_try_open", staticmethod(
        lambda name, opts, gpu_idx=None: None if name in opened else
        "Function not implemented: No usable encoding entrypoint found" if name.endswith("_vaapi") else "no such encoder"))
    mgr = avwriter.EncoderManager()
    assert mgr.get_encoder("vaapi", None, "h264", {}, ("h264",)) == "libx264"
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1, warns
    assert "no hardware encoder opens here for --accel vaapi (h264_vaapi: Function not implemented: No usable " \
           "encoding entrypoint found); encoding with libx264 on the CPU" in warns[0]
    caplog.clear()
    assert mgr.get_encoder("vaapi", None, "h264", {}, ("h264",)) == "libx264"            # cached: said once
    assert mgr.get_encoder("vaapi", "libx264", "h264", {}, ("h264",)) == "libx264"       # asked for by name: nothing
    assert mgr.get_encoder(None, None, "h264", {}, ("h264",)) == "libx264"               # no --accel: nothing
    assert mgr.get_encoder("vdpau", None, "h264", {}, ("h264",)) == "libx264"            # an API that only decodes
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING][-1].endswith(
        "--accel vdpau only decodes, and no other hardware encoder opens on this machine; encoding with libx264 on the CPU")
    caplog.clear()
    opened.add("h264_vaapi")
    assert mgr.get_encoder("vaapi", None, "h264", {"g": "30"}, ("h264",)) == "h264_vaapi"   # one that opens: nothing
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


# ---------------------------------------------------------------- encoders that are handed a device
def test_vaapi_encoders_are_handed_the_render_node_gpu_counts(monkeypatch, tmp_path):
    """The VAAPI encoders take pictures that live on the GPU: without a device they never opened, and
    recording with --accel vaapi went to the CPU. PyAV 19 uploads the pictures once the encoder has one.
    Without --gpu (None) the node is looked for: here the NVIDIA card is renderD128, the AMD GPU renderD129."""
    import util
    for node, vendor in (("renderD128", "0x10de"), ("renderD129", "0x1002")):
        (tmp_path / node / "device").mkdir(parents=True)
        (tmp_path / node / "device" / "vendor").write_text(vendor)
    monkeypatch.setattr(util, "DRM_ROOT", str(tmp_path))
    made = []
    monkeypatch.setattr(avwriter, "_make_hwaccel", lambda kind, device: made.append((kind, device)) or "HW")
    assert avwriter._encoder_hwaccel("h264_vaapi", 0) == "HW" and avwriter._encoder_hwaccel("hevc_VAAPI", 2) == "HW"
    assert avwriter._encoder_hwaccel("av1_vaapi", None) == "HW" and avwriter._encoder_hwaccel("vp9_vaapi", "x") == "HW"
    assert made == [("vaapi", "/dev/dri/renderD128"), ("vaapi", "/dev/dri/renderD130"),
                    ("vaapi", "/dev/dri/renderD129"), ("vaapi", "/dev/dri/renderD129")]
    del made[:]
    for name in ("h264_qsv", "h264_nvenc", "h264_amf", "libx264", "h264_videotoolbox"):
        assert avwriter._encoder_hwaccel(name, 1) is None
    assert made == []


def test_a_vaapi_probe_that_cannot_get_its_device_is_a_failed_probe(monkeypatch):
    def no_device(kind, device):
        raise ValueError(f"no {kind} in this build")
    monkeypatch.setattr(avwriter, "_make_hwaccel", no_device)
    assert avwriter.EncoderManager._try_open("h264_vaapi", {}, 1) is not None      # an error text, not an exception


def test_the_probe_uses_the_camera_s_gpu(tmp_path, monkeypatch):
    monkeypatch.setattr(avwriter.EncoderManager, "_instance", None)
    w = avwriter.AVWriter(None, path=str(tmp_path / "g.mp4"), width=320, height=180, fps=25, encoder="libx264",
                          audio_enable=False, gpu_idx=1)
    try:
        assert avwriter.EncoderManager().gpu_idx == 1
    finally:
        w.open_failed = "not opened in this test"
        w.close()
    w = avwriter.AVWriter(None, path=str(tmp_path / "h.mp4"), width=320, height=180, fps=25, encoder="libx264",
                          audio_enable=False, gpu_idx=0, gpu_given=False)
    try:
        assert avwriter.EncoderManager().gpu_idx is None and w.gpu_idx == 0     # no --gpu: the VAAPI node is looked for
    finally:
        w.open_failed = "not opened in this test"
        w.close()


def test_nvenc_on_a_gpu_that_is_not_there_is_tried_on_gpu_0(tmp_path, monkeypatch, caplog):
    """--gpu 1 (or NOVA_GPU_DEVICES = 0,1 from a bigger machine) on a machine with one NVIDIA card: every
    nvenc encoder failed with "Generic error in an external library" and the camera recorded with libx264."""
    monkeypatch.setattr(avwriter.EncoderManager, "_instance", None)
    w = avwriter.AVWriter(None, path=str(tmp_path / "n.mp4"), width=320, height=180, fps=25, encoder="libx264",
                          audio_enable=False, gpu_idx=1)
    rebuilt = []
    try:
        w.v_encoder = "h264_nvenc"
        monkeypatch.setattr(w, "_rebuild", rebuilt.append)
        with caplog.at_level(logging.WARNING):
            w._next_video_encoder(RuntimeError("Generic error in an external library"))
        assert rebuilt == ["h264_nvenc"] and w.gpu_idx == 0 and "h264_nvenc" not in w._tried
        assert "does not open on GPU 1" in caplog.text and "trying GPU 0" in caplog.text
    finally:
        w.open_failed = "not opened in this test"
        w.close()


def test_pictures_are_converted_to_the_format_chosen_not_to_the_gpu_s():
    """Once an encoder that was handed a device is open, the stream's pix_fmt reads 'vaapi'."""
    from types import SimpleNamespace
    w = avwriter.AVWriter.__new__(avwriter.AVWriter)
    w.v_stream, w._v_pix_fmt, w._colour = SimpleNamespace(pix_fmt="vaapi"), "nv12", None
    w.width, w.height, w.v_time_base = 320, 180, Fraction(1, 90000)
    f = w._fit(next(frames(1)), 9000)
    assert (f.format.name, f.width, f.height, f.pts) == ("nv12", 320, 180, 9000)


# ---------------------------------------------------------------- the camera's colour range
def _yuv_frames(n, fmt="nv12", rng=None, space=None, lo=0, hi=255):
    """n pictures whose luma runs from lo to hi across the width, flagged as given."""
    ramp = np.tile(np.linspace(lo, hi, 320).astype(np.uint8), (180, 1))
    for i in range(n):
        f = av.VideoFrame.from_ndarray(np.concatenate([ramp, np.full((90, 320), 128, np.uint8)]), format="nv12")
        if fmt != "nv12":
            f = f.reformat(format=fmt, src_color_range=2, dst_color_range=2)
        if rng is not None: f.color_range = rng
        if space is not None: f.colorspace = space
        f.pts, f.time_base = i, Fraction(1, 25)
        yield f


def _recorded(tmp_path, name, source, **enc):
    out = tmp_path / name
    w = avwriter.AVWriter(None, path=str(out), width=320, height=180, fps=25, encoder="libx264",
                          enc_v_opts={"preset": "ultrafast", "crf": "10", **enc}, audio_enable=False)
    for f in source:
        w.write_video(f)
    w.close()
    with av.open(str(out)) as c:
        ctx = c.streams.video[0].codec_context
        f = next(c.decode(video=0))
        y = np.frombuffer(f.planes[0], np.uint8).reshape(f.height, -1)[:, :f.width]
        return int(ctx.color_range), int(ctx.colorspace), int(y[90, 2]), int(y[90, 317])


def test_frame_colour_reads_what_the_picture_says():
    plain = next(_yuv_frames(1))
    assert avwriter._frame_colour(plain) == (None, None, None, None)
    assert avwriter._frame_colour(next(_yuv_frames(1, rng=2, space=1))) == (2, 1, None, None)
    assert avwriter._frame_colour(next(_yuv_frames(1, rng=1))) == (1, None, None, None)
    assert avwriter._frame_colour(next(_yuv_frames(1, fmt="yuvj420p")))[0] == 2      # the old name of full range


def test_a_full_range_camera_is_recorded_as_full_range(tmp_path):
    """A camera that sends full range (yuvj420p; 0-255) was recorded without a word about it: players take
    such a file for limited range and clip its blacks and whites (6 of 255 off on average, 21 at worst,
    through libx264, NVENC, Quick Sync and VAAPI alike)."""
    rng, space, dark, bright = _recorded(tmp_path, "full.mp4", _yuv_frames(12, rng=2, space=1))
    assert (rng, space) == (2, 1)                               # the file says full range, BT.709
    assert dark <= 6 and bright >= 249                          # and the samples are the camera's own
    rng, space, dark, bright = _recorded(tmp_path, "j.mp4", _yuv_frames(12, fmt="yuvj420p"))
    assert rng == 2 and dark <= 6 and bright >= 249             # converted to yuv420p for x264, still full


def test_limited_range_and_unsaid_stay_limited(tmp_path):
    # (x264 writes no range flag for limited range unless it has other colour information to write:
    #  1 and 0 both mean limited to a player)
    assert _recorded(tmp_path, "tv.mp4", _yuv_frames(12, rng=1, lo=16, hi=235))[0] in (0, 1)
    assert _recorded(tmp_path, "tv709.mp4", _yuv_frames(12, rng=1, space=1, lo=16, hi=235))[:2] == (1, 1)
    assert _recorded(tmp_path, "unsaid.mp4", _yuv_frames(12, lo=16, hi=235))[0] == 0


def test_a_colour_range_given_with_ffmpeg_options_wins(tmp_path):
    assert _recorded(tmp_path, "forced.mp4", _yuv_frames(12, rng=2), color_range="tv")[0] != 2


# ---------------------------------------------------------------- V4L2 encoders (Raspberry Pi 4)
SPS_PPS = b"\x00\x00\x00\x01\x67\x4d\x00\x28\x9a" + b"\x00\x00\x00\x01\x68\xee\x3c\x80"
IDR = b"\x00\x00\x00\x01\x65\x88\x84\x00\x33"


def test_a_packet_of_parameter_sets_alone_is_told_from_a_picture():
    only = avwriter._only_param_sets
    assert only(SPS_PPS, False) and not only(SPS_PPS + IDR, False) and not only(IDR, False)
    assert only(b"\x00\x00\x01\x09\xf0" + SPS_PPS, False)                  # behind an access unit delimiter
    assert not only(b"\x00\x00\x01\x09\xf0", False) and not only(b"", False) and not only(b"\x00\x00\x01", False)
    vps, sps, slice_ = b"\x00\x00\x00\x01\x40\x01\x0c", b"\x00\x00\x00\x01\x42\x01\x01", b"\x00\x00\x00\x01\x26\x01\xaf"
    assert only(vps + sps, True) and not only(vps + sps + slice_, True) and not only(slice_, True)


def test_v4l2_and_media_foundation_encoders_are_told_a_bit_rate():
    """h264_v4l2m2m without one wrote a 1080p camera in 120 kbit/s (FFmpeg's default of 200)."""
    assert "h264_v4l2m2m".endswith(avwriter.BITRATE_NEEDED) and "h264_mf".endswith(avwriter.BITRATE_NEEDED)
    assert not "libx264".endswith(avwriter.BITRATE_NEEDED) and not "h264_nvenc".endswith(avwriter.BITRATE_NEEDED)


def _split_first(packets):
    """An encoder's packets as a V4L2 encoder hands them out: the parameter sets as a packet of their own."""
    first = bytes(packets[0])
    cut = first.index(b"\x00\x00\x01\x65")                      # the IDR slice
    cut -= 1 if first[cut - 1] == 0 else 0
    head = [n for n in first[:cut].split(b"\x00\x00\x00\x01") if n and n[0] & 0x1f in (7, 8)]
    hdr = av.Packet(b"".join(b"\x00\x00\x00\x01" + n for n in head))
    pic = av.Packet(first[cut:])
    for p in (hdr, pic):
        p.pts, p.dts, p.time_base, p.stream = packets[0].pts, packets[0].dts, packets[0].time_base, packets[0].stream
    pic.is_keyframe = True
    return [hdr, pic] + list(packets[1:])


@pytest.mark.parametrize("movflags", ["", "+frag_keyframe+empty_moov+default_base_moof"])
def test_parameter_sets_that_come_as_a_packet_become_the_mp4_header(tmp_path, movflags):
    """FFmpeg's V4L2 encoders, asked for a global header, send SPS/PPS as a first packet of their own and
    leave the extradata empty: the MP4 had no avcC and a first sample that is no picture (Raspberry Pi 4).
    Here libx264 without its global header plays that encoder; what is written must read as any MP4."""
    path = str(tmp_path / "v.mp4")
    out = av.open(path, "w", format="mp4", options={"movflags": movflags} if movflags else {})
    st = out.add_stream("libx264", rate=10)
    st.width, st.height, st.pix_fmt = 320, 180, "yuv420p"
    ctx = st.codec_context
    ctx.flags &= ~int(av.codec.context.Flags.global_header)      # headers inside the stream, none in the context
    ctx.options = {"bf": "0", "tune": "zerolatency"}
    packets = []
    for i in range(12):
        f = av.VideoFrame.from_ndarray(np.full((180, 320, 3), 20 * i, np.uint8), format="rgb24").reformat(format="yuv420p")
        f.pts = i
        packets += st.encode(f)
    packets += st.encode(None)
    assert not ctx.extradata
    packets = _split_first(packets)
    assert avwriter._only_param_sets(bytes(packets[0]), False)

    w = avwriter.AVWriter.__new__(avwriter.AVWriter)
    w.v_stream, w.v_encoder, w._video_started, w._held_audio, w._audio_pending = st, "libx264", False, [], False
    written = []
    w._write = lambda p: (written.append(p), out.mux(p))
    w._mux(packets)
    out.close()
    assert len(written) == 12 and bytes(ctx.extradata) == bytes(packets[0])
    with av.open(path) as c:
        v = c.streams.video[0]
        assert bytes(v.codec_context.extradata)[:1] == b"\x01"            # an avcC, as every player expects
        assert sum(1 for _ in c.decode(v)) == 12


def test_a_first_packet_with_a_picture_is_written_as_it_is():
    w = avwriter.AVWriter.__new__(avwriter.AVWriter)
    ctx = SimpleNamespace(extradata=None)
    w.v_stream, w.v_encoder = SimpleNamespace(codec_context=ctx), "h264_v4l2m2m"
    assert not w._takes_headers(av.Packet(SPS_PPS + IDR)) and ctx.extradata is None
    assert w._takes_headers(av.Packet(SPS_PPS)) and ctx.extradata == SPS_PPS
    ctx.extradata = b"\x01avcC"
    assert not w._takes_headers(av.Packet(SPS_PPS)) and ctx.extradata == b"\x01avcC"   # an encoder with a header of its own


def test_a_v4l2_encoders_packets_are_copied_out_of_the_devices_buffers():
    """Four buffers by default; the muxer keeps video packets while it waits for audio, and with all four
    out FFmpeg warned "All capture buffers returned to userspace" (Raspberry Pi 4)."""
    w = avwriter.AVWriter.__new__(avwriter.AVWriter)
    p = av.Packet(IDR)
    p.pts, p.dts, p.duration, p.time_base, p.is_keyframe = 900, 800, 100, Fraction(1, 1000), True
    w.v_encoder = "libx264"
    assert w._owned([p])[0] is p
    w.v_encoder = "h264_v4l2m2m"
    (q,) = w._owned([p])
    assert q is not p and bytes(q) == IDR
    assert (q.pts, q.dts, q.duration, q.time_base, q.is_keyframe) == (900, 800, 100, Fraction(1, 1000), True)
    assert w._owned([]) == []


def test_an_encoder_that_opens_and_takes_no_picture_is_left_for_the_next(caplog):
    """Six cameras recording through a Raspberry Pi 4's encoder, which has room for two: h264_v4l2m2m opened
    for every one and answered each picture with EAGAIN.  The recorder counted 60 errors, gave up, and
    began again a minute later, with a kernel warning and its call trace for every picture."""
    class Stream:
        def __init__(self, name, fail):
            self.name, self.fail, self.fed = name, fail, 0
            self.codec_context = SimpleNamespace(is_open=True)

        def encode(self, frame):
            self.fed += 1
            if self.fail:
                raise BlockingIOError(11, "Resource temporarily unavailable", "avcodec_send_frame()")
            return []

    w = avwriter.AVWriter.__new__(avwriter.AVWriter)
    hw, sw = Stream("h264_v4l2m2m", True), Stream("libx264", False)
    w.v_stream, w.v_encoder, w.v_frames, w.last_v_pts, w.v_time_base = hw, "h264_v4l2m2m", 0, -1, Fraction(1, 90000)
    w._colour, w._v_opts_checked, w._applied_v = (None, None, None, None), True, {}
    w._fit = lambda frame, pts: frame
    w._mux = lambda packets: None
    said = []

    def next_encoder(e, taking=False):
        said.append((type(e).__name__, taking))
        w.v_stream, w.v_encoder = sw, "libx264"

    w._next_video_encoder = next_encoder
    for i in range(5):
        w._encode_video(SimpleNamespace(), i / 10)
    assert said == [("BlockingIOError", True)]            # at the first picture, and once
    assert (hw.fed, sw.fed, w.v_frames) == (1, 5, 5)
    sw.fail = True                                        # an encoder that has taken pictures: its errors are the recorder's
    with pytest.raises(BlockingIOError):
        w._encode_video(SimpleNamespace(), 0.6)
    assert len(said) == 1


def _noted(accel, asked, used, why=None):
    w = avwriter.AVWriter.__new__(avwriter.AVWriter)
    w._enc_args, w.v_encoder = (accel, asked, "h264", {}, ("h264", "hevc")), used
    mgr = avwriter.EncoderManager()
    mgr._why.clear()
    for name, text in (why or {}).items():
        mgr.reject(name, text)
    return w.encoder_note()


def test_an_encoder_that_is_not_the_one_asked_for_says_so_for_nova_status(monkeypatch):
    """As for decoders: the warning about it is not written by a camera started with -l ERROR."""
    monkeypatch.setattr(avwriter.EncoderManager, "_instance", None)
    monkeypatch.setattr(avwriter, "IS_PI", False)
    monkeypatch.setattr(avwriter, "WINDOWS", False)
    monkeypatch.setattr(avwriter, "MSYS", False)
    monkeypatch.setattr(avwriter, "MAC", False)
    assert _noted(None, None, "libx264") is None                    # nothing asked for
    assert _noted("cuda", None, "h264_nvenc") is None and _noted(None, "libx265", "libx265") is None
    assert _noted("cuda", "h264_nvenc", "h264_nvenc") is None
    assert _noted("vdpau", None, "libx264") is None                 # an --accel that only decodes
    assert _noted(None, "h264_v4l2m2m", "libx264", {"h264_v4l2m2m": "opens and takes no picture at 1920x1080: EAGAIN"}) == \
        "libx264 on the CPU in place of -e h264_v4l2m2m (opens and takes no picture at 1920x1080: EAGAIN)"
    assert _noted(None, "hevc_nvenc", "h264_nvenc") == "h264_nvenc in place of -e hevc_nvenc"
    assert _noted("cuda", None, "libx264", {"h264_nvenc": "No capable devices found"}) == \
        "libx264 on the CPU: no hardware encoder is in use for --accel cuda (h264_nvenc: No capable devices found)"
    assert _noted("cuda", None, "libx264") == "libx264 on the CPU: no hardware encoder is in use for --accel cuda"


def test_the_reason_an_encoder_did_not_open_is_kept_until_it_opens(manager):
    mgr = avwriter.EncoderManager()
    assert mgr.why("mpeg4") is None
    assert mgr.reject("mpeg4", "does not open at 320x180: time base") is True and mgr.why("mpeg4").startswith("does not open")
    mgr.reject("mpeg4")                                             # no new reason: the old one stands
    assert mgr.why("mpeg4").startswith("does not open")
