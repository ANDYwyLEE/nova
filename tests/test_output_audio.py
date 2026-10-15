"""Audio track decisions in AVWriter: decided by the first audio frame, late audio replacing the silent
live track, a camera audio format change, and no silence burst after an outage."""
import logging
import time
from fractions import Fraction

import av
import numpy as np

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


def test_late_camera_audio_replaces_the_silent_live_track(tmp_path, caplog):
    caplog.set_level(logging.INFO)
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
