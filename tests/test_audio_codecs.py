"""Camera audio codecs over real RTP (the depacketizers RTSP uses): the system ffmpeg sends a tone, PyAV
receives it, the frames are converted as the player does and re-encoded to AAC as the recorder does.
Skipped where no ffmpeg binary with these encoders is installed (GitHub's runners have one)."""
import os
import shutil
import subprocess
import time
from fractions import Fraction

import numpy as np
import pytest

import av

import avwriter
from videoworker import _frame_to_pcm16, _nb_channels

FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(FFMPEG is None, reason="no ffmpeg binary to send RTP with")

W, H, FPS = 160, 96, 25


def video(i):
    f = av.VideoFrame.from_ndarray(np.zeros((H, W, 3), np.uint8), format="rgb24")
    f.pts, f.time_base = i, Fraction(1, FPS)
    return f


def encoders():
    return subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout


@pytest.mark.parametrize("port, encoder, extra, rate, ch, decoded_as", [
    (21002, "pcm_mulaw", [], 8000, 1, "pcm_mulaw"),
    (21004, "pcm_alaw", [], 8000, 1, "pcm_alaw"),
    (21006, "g726", ["-code_size", "4"], 8000, 1, "g726"),
    (21008, "g722", [], 16000, 1, "g722"),
    (21010, "aac", ["-b:a", "32k"], 8000, 1, "aac"),
    (21012, "aac", ["-b:a", "128k"], 48000, 2, "aac"),
    (21014, "libopus", ["-b:a", "32k"], 48000, 1, "opus"),
    (21016, "pcm_s16be", [], 16000, 1, "pcm_s16be"),
])
def test_camera_audio_codec_plays_and_records(tmp_path, port, encoder, extra, rate, ch, decoded_as):
    if f" {encoder} " not in encoders():
        pytest.skip(f"ffmpeg here has no {encoder} encoder")
    sdp = tmp_path / "a.sdp"
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={rate}",
           "-t", "4", "-ar", str(rate), "-ac", str(ch), "-c:a", encoder] + extra + ["-f", "rtp", f"rtp://127.0.0.1:{port}", "-sdp_file", str(sdp)]
    p = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(60):
            if sdp.exists() and sdp.stat().st_size and "m=audio" in sdp.read_text():
                break
            assert p.poll() is None, p.stderr.read()
            time.sleep(0.05)
        c = None
        for attempt in range(2):                           # the first packets may be gone before the socket is open
            try:
                c = av.open(str(sdp), options={"protocol_whitelist": "file,rtp,udp"}, timeout=(3.0, 3.0))
                break
            except av.error.ExitError:
                if attempt: raise
        s = c.streams.audio[0]
        assert s.codec_context.name == decoded_as
        frames, t0 = [], time.monotonic()
        for pk in c.demux(s):
            frames.extend(pk.decode())
            if len(frames) >= 40 or time.monotonic() - t0 > 2.5:
                break
        c.close()
    finally:
        p.kill(); p.wait()
    assert frames, "nothing decoded"
    f0 = frames[0]
    peak = max(int(np.abs(np.frombuffer(_frame_to_pcm16(f, _nb_channels(f))[0], np.int16)).max()) for f in frames)
    assert peak > 500, peak                                  # the tone came through the player's conversion
    out = tmp_path / "rec.mp4"
    w = avwriter.AVWriter(None, path=str(out), width=W, height=H, fps=FPS, encoder="libx264",
                          enc_v_opts={"preset": "ultrafast"}, sample_rate=f0.sample_rate, channels=_nb_channels(f0))
    n = 0
    for i, fr in enumerate(frames):
        if i % 3 == 0:
            w.write_video(video(n)); n += 1
        w.write_audio(fr)
    w.write_video(video(n))
    w.close()
    with av.open(str(out)) as r:
        a = r.streams.audio[0]
        assert a.codec_context.name == "aac" and a.rate == f0.sample_rate
        assert sum(fr.samples for fr in r.decode(audio=0)) > 0
