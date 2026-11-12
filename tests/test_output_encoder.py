"""Encoder opens at the real size: fallback to the next candidate, and no second open of a failed context
(in PyAV 19 an encode() or a header write after a failed avcodec_open2() segfaults)."""
import logging
import os
import subprocess
import sys
import textwrap
from fractions import Fraction

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
    assert avwriter._mf_bitrate(1920, 1080, 25) == 5_184_000
    assert avwriter._mf_bitrate(1920, 1080, 10) == 2_073_600
    assert avwriter._mf_bitrate(320, 180, 5) == 500_000


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
