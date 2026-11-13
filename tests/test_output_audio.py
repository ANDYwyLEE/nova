"""Audio track decisions in AVWriter: decided by the first audio frame, late audio replacing the silent
live track, a camera audio format change, and no silence burst after an outage."""
import logging
import time
from fractions import Fraction

import av
import numpy as np
import pytest

import logsync  # noqa: F401  (logging.verbose)
import avwriter

W, H, FPS = 160, 96, 25


def video(i):
    f = av.VideoFrame.from_ndarray(np.full((H, W, 3), (i * 7) % 256, np.uint8), format="rgb24")
    f.pts, f.time_base = i, Fraction(1, FPS)
    return f


def audio(i, rate=16000, layout="stereo", fmt="fltp", samples=640, amp=0.5):
    """Frame i of a 440 Hz tone, 40 ms per frame at 16 kHz."""
    t = (np.arange(samples) + i * samples) / rate
    tone = (amp * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    ch = 2 if layout == "stereo" else 1
    if fmt == "fltp":
        a = av.AudioFrame.from_ndarray(np.tile(tone, (ch, 1)), format="fltp", layout=layout)
    else:
        pcm = (np.repeat(tone, ch) * 32767).astype(np.int16).reshape(1, -1)
        a = av.AudioFrame.from_ndarray(pcm, format="s16", layout=layout)
    a.sample_rate, a.pts, a.time_base = rate, i * samples, Fraction(1, rate)
    return a


def writer(path, **kw):
    kw.setdefault("encoder", "libx264")
    kw.setdefault("enc_v_opts", {"preset": "ultrafast"})
    return avwriter.AVWriter(None, path=str(path), width=W, height=H, fps=FPS, **kw)


def decoded_audio(path):
    with av.open(str(path)) as c:
        if not c.streams.audio:
            return None
        return np.concatenate([f.to_ndarray().reshape(-1) for f in c.decode(audio=0)])


def test_audio_that_starts_after_the_first_video_frame_gets_a_track(tmp_path):
    out = tmp_path / "rec.mp4"
    w = writer(out)                                   # sample_rate 0: the decoder has seen no audio yet
    assert w.a_stream is None and w._audio_pending
    for i in range(12):                               # 0.5 s of video first
        w.write_video(video(i))
    for i in range(12, 62):
        w.write_video(video(i))
        w.write_audio(audio(i - 12))
    w.close()
    with av.open(str(out)) as c:
        a = c.streams.audio[0]
        assert (a.codec_context.name, a.rate, a.codec_context.layout.name) == ("aac", 16000, "stereo")
        assert sum(1 for _ in c.decode(video=0)) == 62
    assert len(decoded_audio(out)) / 2 > 16000 * 1.5            # ~2 s of stereo samples


def test_a_camera_without_audio_records_video_only_after_the_wait(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    out = tmp_path / "rec.mp4"
    w = writer(out)
    for i in range(int(FPS * (avwriter.AUDIO_WAIT + 1))):
        w.write_video(video(i))
    assert not w._audio_pending and not w._pending      # decided, held packets released
    w.close()
    assert decoded_audio(out) is None
    assert any("no camera audio within" in r.getMessage() for r in caplog.records)


def test_late_camera_audio_replaces_the_silent_live_track(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(avwriter, "LIVE_MAX_LEAD", None)   # seconds of frames written at once on purpose
    out = tmp_path / "live.flv"
    w = writer(out, live=True)
    n_silent = int(FPS * (avwriter.AUDIO_WAIT + 0.5))
    for i in range(n_silent):
        w.write_video(video(i))
    assert w.silence                                   # no audio within the wait: silent AAC, 48 kHz mono
    for i in range(n_silent, n_silent + 75):
        w.write_video(video(i))
        w.write_audio(audio(i - n_silent))             # 16 kHz stereo, converted into the track
    w.close()
    assert not w.silence
    pcm = decoded_audio(out)
    rate = 48000
    assert np.abs(pcm[: rate * 2]).max() < 1e-3          # silence first
    assert np.abs(pcm[-rate:]).max() > 0.2               # then the camera's tone
    assert any("replaces the silent track" in r.getMessage() for r in caplog.records)


def test_a_camera_audio_format_change_keeps_the_track(tmp_path):
    out = tmp_path / "rec.mp4"
    w = writer(out, sample_rate=16000, channels=2)
    for i in range(50):
        w.write_video(video(i))
        if i < 25:
            w.write_audio(audio(i))                                       # fltp stereo 16 kHz
        else:
            w.write_audio(audio(i, rate=8000, layout="mono", fmt="s16", samples=320))   # after a reconnect
    w.close()
    assert w.a_frames == 50
    assert len(decoded_audio(out)) / 2 > 16000 * 1.5


def test_a_long_outage_is_not_backfilled_with_silence(tmp_path):
    w = writer(tmp_path / "live.flv", live=True, audio_wait=0)
    assert w.silence
    for i in range(25):
        w.write_video(video(i))
    before = w.a_frames
    for tl in (w.v_tl, w.a_tl):                          # 10 h pass without frames
        tl.origin -= 36000
    t0 = time.monotonic()
    w.write_video(video(25 + 36000 * FPS))
    took = time.monotonic() - t0
    w.write_video(video(26 + 36000 * FPS))
    w.close()
    assert took < 1.0 and w.a_frames - before < 10, (took, w.a_frames - before)


def test_closing_frees_the_resampler_graphs_and_their_threads(tmp_path):
    """PyAV's AudioResampler (ours, and the one inside its audio encoders) runs a libavfilter graph with
    a thread per core; a Graph and its filter contexts form a reference cycle that only the cyclic GC
    frees.  A stream reconnecting 20 times kept 315 idle threads for hours: close() frees them now."""
    import gc
    import os
    def graphs():
        return sum(isinstance(o, av.filter.Graph) for o in gc.get_objects())
    gc.collect()
    before = graphs()
    gc.disable()                                      # only close() may collect here
    try:
        for r in range(2):
            w = writer(tmp_path / f"seg{r}.mp4", sample_rate=8000, channels=1)
            for i in range(30):
                w.write_video(video(i))
                w.write_audio(audio(i, rate=8000, layout="mono", fmt="s16", samples=320))
            assert graphs() > before                  # camera audio -> AAC went through resamplers
            threads = len(os.listdir("/proc/self/task")) if os.path.isdir("/proc/self/task") else None
            w.close()
            del w
            assert graphs() == before
            if threads is not None:
                assert len(os.listdir("/proc/self/task")) < threads
    finally:
        gc.enable()


def test_sound_and_picture_stay_together_after_a_quick_reconnect_of_a_fast_camera_clock(tmp_path, monkeypatch):
    """The camera's clock runs fast, so the file is ahead of the wall clock (here 5 %: 3 s after a
    minute; 0.46 % and 8 s per half hour were measured). Its audio stops, the connection is made
    again within a second: the new connection's sound is written next to its picture. It was written
    2 s before it, until the segment ended."""
    t = [1000.0]
    monkeypatch.setattr(avwriter.time, "monotonic", lambda: t[0])
    out = tmp_path / "rec.mp4"
    w = writer(out)

    def frames(i, src):                               # one video frame and its 40 ms of sound
        v = video(0); v.pts, v.time_base = int(round(src * 90000)), Fraction(1, 90000)
        a = audio(i); a.pts = int(round(src * 16000))
        return v, a

    n = 0
    for i in range(60 * FPS):                         # a minute, timestamps 5 % fast
        t[0] = 1000.0 + i / FPS
        v, a = frames(n, i / FPS * 1.05); n += 1
        w.write_video(v); w.write_audio(a)
    assert w.v_tl.last_out - (t[0] - 1000.0) > 2.9
    for i in range(60 * FPS, 62 * FPS):               # the audio is gone, the video goes on
        t[0] = 1000.0 + i / FPS
        w.write_video(frames(n, i / FPS * 1.05)[0])
    first_new = 62 * FPS                              # index of the new connection's first video frame
    t[0] += 1.0                                       # reconnected in a second; timestamps start again
    t1 = t[0]
    for i in range(2 * FPS):
        t[0] = t1 + i / FPS
        v, a = frames(n, i / FPS); n += 1
        w.write_video(v); w.write_audio(a)
    w.close()

    with av.open(str(out)) as c:
        vs, au = c.streams.video[0], c.streams.audio[0]
        vt = sorted(float(p.pts * vs.time_base) for p in c.demux(vs) if p.pts is not None)
        c.seek(0)
        at = sorted(float(p.pts * au.time_base) for p in c.demux(au) if p.pts is not None)
    assert at[-1] - vt[-1] == pytest.approx(0.0, abs=0.15)          # they end together, as they came
    after = [x for x in at if x >= vt[first_new] - 0.15]
    assert len(after) * 1024 / 16000 == pytest.approx(2.0, abs=0.2)  # all of the new sound is past the new picture's start


def test_a_camera_whose_timestamps_step_records_without_the_steps(tmp_path, monkeypatch):
    """A camera whose RTCP sender reports do not fit: complete frames and samples, in real time, with
    timestamps that step forward and back every few seconds, picture and sound at their own moments.
    The file got a gap for every step forward: these 38 s of camera made 40.6 s of picture and 40.3 s
    of sound, with a hole of up to 1.5 s in it at every step. Now the file is as long as what the camera
    made, and its sound has no hole."""
    t = [1000.0]
    monkeypatch.setattr(avwriter.time, "monotonic", lambda: t[0])
    v_err = [0.0, -0.31, 0.83, -0.77, 0.52, 0.13]       # the error of each stream's timestamps, changing
    a_err = [0.0, -0.46, 0.94, -0.12, -0.58, 0.37]      # every 7 s (the sound's 3.5 s after the picture's)
    w = writer(tmp_path / "rec.mp4")
    seconds = 38
    for i in range(seconds * FPS):
        true = i / FPS
        t[0] = 1000.0 + true
        v = video(0); v.pts, v.time_base = int(round((50.0 + true + v_err[int(true // 7)]) * 90000)), Fraction(1, 90000)
        a = audio(i); a.pts = int(round((50.0 + true + a_err[int(max(0.0, true - 3.5) // 7 + (true >= 3.5))]) * 16000))
        w.write_video(v); w.write_audio(a)
    assert w.watch.on
    w.close()

    with av.open(str(tmp_path / "rec.mp4")) as c:
        vs, au = c.streams.video[0], c.streams.audio[0]
        vt = sorted(float(p.pts * vs.time_base) for p in c.demux(vs) if p.pts is not None)
        c.seek(0)
        at = sorted(float(p.pts * au.time_base) for p in c.demux(au) if p.pts is not None)
    assert len(vt) == seconds * FPS
    vt = vt[1:]                                    # (the muxer moves all but the first packet by the AAC priming)
    assert vt[-1] - vt[0] == pytest.approx(seconds - 2 / FPS, abs=0.01)
    assert max(b - a for a, b in zip(vt, vt[1:])) < 0.05
    assert max(b - a for a, b in zip(at, at[1:])) < 0.07                 # 1024 samples at 16 kHz are 0.064 s
    assert at[-1] - vt[-1] == pytest.approx(0.0, abs=0.15)
