"""A live stream stays at real time when the source delivers faster: seconds of picture at once used to put
the stream that far ahead of the clock for good; YouTube then warns "sending data faster than realtime"."""
import av

import avwriter
from test_output_audio import FPS, video, writer


class Clock:
    def __init__(self): self.t = 5000.0
    def __call__(self): return self.t


def last_video_time(path):
    with av.open(str(path)) as c:
        s = c.streams.video[0]
        return max(float(p.pts * s.time_base) for p in c.demux(s) if p.pts is not None)


def test_a_camera_burst_does_not_put_the_stream_ahead_of_the_clock(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(avwriter.time, "monotonic", clock)
    out = tmp_path / "live.flv"
    w = writer(out, live=True, audio_wait=0)
    t0 = clock.t
    i = 0
    for rnd in range(3):                       # three connections, each opening with a 3.4-s burst
        base = i
        for k in range(85):
            w.write_video(video(base + k)); i += 1
            clock.t += 0.1 / 85
        for k in range(85, 85 + 100):          # then 4 s in real time
            clock.t += 1 / FPS
            w.write_video(video(base + k)); i += 1
        clock.t += 15                          # camera off; the next connection jumps in pts: re-anchored
        i = base + 400
    wall = clock.t - 15 - t0
    w.close()
    ahead = last_video_time(out) - wall
    assert ahead <= avwriter.LIVE_MAX_LEAD + 0.1, f"{ahead:.2f} s ahead of the clock"
    assert w.v_frames > 3 * 100 * 0.7          # the real-time part goes through


def test_a_recording_keeps_every_frame_of_a_burst(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(avwriter.time, "monotonic", clock)
    w = writer(tmp_path / "rec.mp4", audio_wait=0)
    for k in range(85):
        w.write_video(video(k))
        clock.t += 0.1 / 85
    w.close()
    assert w.v_frames == 85
