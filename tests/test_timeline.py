import time

import pytest

from avwriter import _Timeline


def test_continuous_run_preserves_source_timing():
    tl = _Timeline(time.monotonic())
    t0 = tl.map(10.0)
    assert tl.map(10.04) == pytest.approx(t0 + 0.04)
    assert tl.map(11.00) == pytest.approx(t0 + 1.00)


def test_reanchor_never_runs_backwards():
    tl = _Timeline(time.monotonic() - 100.0)     # the output clock is already at ~100 s
    a = tl.map(5.0)
    b = tl.map(5.1)
    assert b > a
    c = tl.map(0.0)                              # pts reset (reconnect): re-anchor, not rewind
    assert c >= b
    d = tl.map(0.0 + _Timeline.MAX_JUMP + 1)     # jump beyond MAX_JUMP: re-anchor again
    assert d >= c
    assert tl.map(d - 50) >= d                   # a jump back re-anchors too


def test_missing_pts_follows_the_wall_clock_monotonically():
    tl = _Timeline(time.monotonic())
    a = tl.map(1.0)
    b = tl.map(None)
    c = tl.map(None)
    assert a <= b <= c


class _Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


def test_live_burst_is_held_to_the_clock(monkeypatch):
    """3.4 s of picture delivered at once: a live timeline never runs more than max_lead ahead of
    the clock, holds frames until the clock has caught up, then goes on from the clock, so ordinary
    jitter afterwards holds nothing."""
    clock = _Clock()
    monkeypatch.setattr("avwriter.time.monotonic", clock)
    tl = _Timeline(clock(), max_lead=1.0)
    outs, held = [], 0
    for i in range(85):                          # 3.4 s at 25 fps within 0.1 s of wall time
        out = tl.map(i * 0.04)
        held += tl.held
        if not tl.held: outs.append(out)
        clock.t += 0.1 / 85
    assert held > 50
    assert max(outs) - (clock.t - 1000.0) <= 1.0 + 0.04
    late = []
    for i in range(85, 85 + 250):                # then real time with +-15 ms jitter
        clock.t += 0.04 + (0.015 if i % 2 else -0.015)
        out = tl.map(i * 0.04)
        late.append(tl.held)
        if not tl.held: outs.append(out)
    assert any(late[:30]) and not any(late[30:])
    assert all(b > a for a, b in zip(outs, outs[1:]))
    assert abs(tl.last_out - (clock.t - 1000.0)) < 0.1


def test_recording_timeline_keeps_the_source_timing_of_a_burst(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr("avwriter.time.monotonic", clock)
    tl = _Timeline(clock())
    outs = [tl.map(i * 0.04) for i in range(85)]
    assert outs[-1] == pytest.approx(84 * 0.04)
    assert not tl.held


FAST = 1.0046       # a camera measured in a soak: 1808.4 s of timestamps in a 1800 s segment


def _fast_pair(monkeypatch, seconds):
    """A writer's two timelines after `seconds` of a camera whose clock runs fast: the file is ahead."""
    clock = _Clock()
    monkeypatch.setattr("avwriter.time.monotonic", clock)
    v, a = _Timeline(clock()), _Timeline(clock())
    v.peer, a.peer = a, v
    for i in range(seconds * 10):
        clock.t = 1000.0 + i * 0.1
        v.map(i * 0.1 * FAST); a.map(i * 0.1 * FAST)
    assert v.last_out - (clock.t - 1000.0) == pytest.approx(seconds * (FAST - 1), abs=0.01)
    return clock, v, a


@pytest.mark.parametrize("first", ["video", "audio"])
def test_a_quick_reconnect_keeps_sound_and_picture_together_when_the_file_is_ahead(monkeypatch, first):
    """25 minutes of a fast camera clock put the file 6.9 s ahead of the wall clock. The camera then
    stops sending audio, the watchdog reconnects 30 s later and the new connection is there in 5 s:
    less than the file is ahead. The picture went on from its own end, the sound from the wall clock,
    1.9 s early until the segment ended."""
    clock, v, a = _fast_pair(monkeypatch, 1500)
    t0 = clock.t
    for i in range(300):                             # 30 s of video only
        clock.t = t0 + i * 0.1
        v.map((1500 + i * 0.1) * FAST)
    end = v.last_out
    clock.t += 5.0                                   # the reconnect; timestamps start again
    for i in range(20):                              # audio and video of one instant arrive together
        outs = {}
        for name in (first, "audio" if first == "video" else "video"):
            outs[name] = (v if name == "video" else a).map(i * 0.1)
        assert outs["audio"] - outs["video"] == pytest.approx(0.0, abs=0.11)
        assert outs["video"] >= end
        clock.t += 0.1


def test_one_stream_that_starts_again_joins_the_other_where_it_is(monkeypatch):
    """Only the audio's timestamps start again (the video goes on): it goes on next to the video,
    not at the wall clock that the file has left behind."""
    clock, v, a = _fast_pair(monkeypatch, 1500)
    ahead = v.last_out - (clock.t - 1000.0)
    assert ahead > 6
    clock.t += 0.1
    vo = v.map(1500.1 * FAST)
    ao = a.map(0.0)
    assert ao == pytest.approx(vo, abs=0.11)


def test_a_reconnect_longer_than_the_lead_goes_back_to_the_wall_clock(monkeypatch):
    clock, v, a = _fast_pair(monkeypatch, 600)       # 2.8 s ahead
    clock.t += 60.0                                  # the camera was away for a minute
    now = clock.t - 1000.0
    assert v.map(0.0) == pytest.approx(now) and a.map(0.0) == pytest.approx(now)


def test_the_writer_pairs_its_timelines(tmp_path):
    import avwriter
    w = avwriter.AVWriter(None, path=str(tmp_path / "t.mp4"), width=160, height=96, fps=25, encoder="libx264")
    try:
        assert w.v_tl.peer is w.a_tl and w.a_tl.peer is w.v_tl
    finally:
        w.close()


# ---- timestamps that step while the stream goes on (cameras whose RTCP sender reports do not fit) ----

from avwriter import TimestampWatch, timestamp_watch


def _feed(tl, clock, packets):
    """packets: (arrival, timestamp[, dur]) -> the output times."""
    outs = []
    for p in packets:
        clock.t = 1000.0 + p[0]
        outs.append(tl.map(p[1], *p[2:]))
    return outs


def _clocked(monkeypatch, watch=None):
    clock = _Clock()
    monkeypatch.setattr("avwriter.time.monotonic", clock)
    return clock, _Timeline(clock(), watch=watch)


def test_a_step_back_while_the_stream_goes_on_is_not_a_new_connection(monkeypatch):
    """The timestamps go back 0.8 s between two frames that arrive 0.1 s apart: the stream goes on by
    its frame interval (it was put at the last frame, a frame lost in time), and the source is doubted."""
    clock, tl = _clocked(monkeypatch)
    outs = _feed(tl, clock, [(i * 0.1, 50.0 + i * 0.1) for i in range(20)]
                 + [(2.0 + i * 0.1, 51.2 + i * 0.1) for i in range(20)])
    steps = [b - a for a, b in zip(outs, outs[1:])]
    assert all(s == pytest.approx(0.1) for s in steps)
    assert tl.watch.on and tl.watch.taken == 1


def test_a_step_back_after_two_seconds_without_a_packet_is_a_new_connection(monkeypatch):
    clock, tl = _clocked(monkeypatch)
    outs = _feed(tl, clock, [(i * 0.1, 50.0 + i * 0.1) for i in range(20)] + [(7.0, 3.0), (7.1, 3.1)])
    assert outs[20] == pytest.approx(7.0) and outs[21] == pytest.approx(7.1)     # the wall clock again
    assert not tl.watch.on


def test_a_step_forward_is_a_gap_as_long_as_the_source_is_not_doubted(monkeypatch):
    """A camera that leaves out a second of frames: written as the second it is."""
    clock, tl = _clocked(monkeypatch)
    outs = _feed(tl, clock, [(i * 0.1, 50.0 + i * 0.1) for i in range(20)]
                 + [(1.95 + i * 0.1, 52.9 + i * 0.1) for i in range(5)])           # they arrive in a burst's tail
    assert outs[20] - outs[19] == pytest.approx(1.0)
    assert not tl.watch.on and tl.watch.taken == 0


def _doubted(monkeypatch, audio=False):
    clock, tl = _clocked(monkeypatch)
    step = 0.16 if audio else 0.1
    dur = (step,) if audio else ()
    pk = [(i * step, 50.0 + i * step) + dur for i in range(20)]
    pk += [(20 * step, 49.0) + dur]                                               # the step back
    pk += [((21 + i) * step, 49.0 + (1 + i) * step) + dur for i in range(10)]
    outs = _feed(tl, clock, pk)
    assert tl.watch.on
    return clock, tl, outs[-1], pk[-1][0], pk[-1][1], step, dur


@pytest.mark.parametrize("audio", [False, True])
def test_a_doubted_source_gets_no_gap_for_a_step_its_packets_do_not_show(monkeypatch, audio):
    clock, tl, out, t, ts, step, dur = _doubted(monkeypatch, audio)
    a = _feed(tl, clock, [(t + step, ts + step + 1.18) + dur])[0]                 # +1.18 s, on time
    assert a - out == pytest.approx(step)
    b = _feed(tl, clock, [(t + 2 * step, ts + 2 * step + 1.18 - 0.07) + dur])[0]  # a step shorter than its own
    assert b - a == pytest.approx(step)
    assert tl.watch.taken == 3


@pytest.mark.parametrize("audio", [False, True])
def test_a_doubted_source_gets_its_gap_when_nothing_came_for_that_long(monkeypatch, audio):
    clock, tl, out, t, ts, step, dur = _doubted(monkeypatch, audio)
    a = _feed(tl, clock, [(t + 3.0, ts + 3.0) + dur])[0]
    assert a - out == pytest.approx(3.0)


def test_frames_a_doubted_camera_left_out_are_a_gap(monkeypatch):
    """Whole frame intervals, and the next frame comes later by about as much: three frames left out.
    The same step with the frames arriving one behind the other is a step of the timestamps."""
    clock, tl, out, t, ts, step, dur = _doubted(monkeypatch)
    a = _feed(tl, clock, [(t + 0.4, ts + 0.4)])[0]
    assert a - out == pytest.approx(0.4)
    b = _feed(tl, clock, [(t + 0.41, ts + 0.8)])[0]
    assert b - a == pytest.approx(0.1)


def test_the_picture_of_a_doubted_source_keeps_its_small_irregularities(monkeypatch):
    clock, tl, out, t, ts, step, dur = _doubted(monkeypatch)
    a = _feed(tl, clock, [(t + 0.1, ts + 0.112)])[0]
    assert a - out == pytest.approx(0.112)


def test_a_file_that_is_read_is_never_doubted(monkeypatch):
    clock, tl = _clocked(monkeypatch, TimestampWatch(live=False))
    outs = _feed(tl, clock, [(i * 0.1, 5.0 + i * 0.1) for i in range(20)]
                 + [(2.0 + i * 0.1, i * 0.1) for i in range(20)]                  # the file starts again
                 + [(4.0, 3.0)])                                                  # and has a gap of its own
    assert not tl.watch.on
    assert outs[20] - outs[19] == pytest.approx(0.1)
    assert outs[-1] - outs[-2] == pytest.approx(1.1)


def test_the_steps_can_be_kept_as_they_come(monkeypatch):
    monkeypatch.setenv("NOVA_TIMESTAMP_STEPS", "keep")
    clock, tl = _clocked(monkeypatch)
    outs = _feed(tl, clock, [(i * 0.1, 50.0 + i * 0.1) for i in range(20)]
                 + [(2.0 + i * 0.1, 51.9 + 1.1 + i * 0.1) for i in range(5)]      # forward: a gap
                 + [(2.5 + i * 0.1, 51.0 + i * 0.1) for i in range(5)])           # back: put at the last frame
    assert outs[20] - outs[19] == pytest.approx(1.1)
    assert outs[25] == pytest.approx(outs[24])
    assert not tl.watch.on


def _sloppy_camera(seconds):
    """Packets of a camera like the four measured ones: frames and samples complete and in real time,
    delivered in bursts, and each stream's timestamps off by an error that changes with every sender
    report (7 s apart, the two streams at their own moments). -> (arrival, 'v'|'a', timestamp, true time)"""
    errors = [-0.31, 0.83, -0.77, 0.52, 0.13, -0.46, 0.94, -0.12, -0.58, 0.37, 0.71, -0.89, 0.24]
    out = []
    for kind, step, phase, burst in (("v", 0.1, 0.0, 0.4), ("a", 0.16, 3.5, 0.64)):
        for i in range(int(seconds / step)):
            true = i * step
            k = int((true - phase) // 7.0)
            err = errors[(k + (5 if kind == "a" else 0)) % len(errors)] if k >= 0 else 0.0
            arrival = (true // burst + 1) * burst + i * 1e-6                       # with the end of its burst
            out.append((arrival, kind, 100.0 + true + err, true))
    return sorted(out)


def _record(monkeypatch, packets):
    clock = _Clock()
    monkeypatch.setattr("avwriter.time.monotonic", clock)
    watch = TimestampWatch()
    v, a = _Timeline(clock(), watch=watch), _Timeline(clock(), watch=watch)
    v.peer, a.peer = a, v
    err = {"v": [], "a": []}
    for arrival, kind, ts, true in packets:
        clock.t = 1000.0 + arrival
        out = v.map(ts) if kind == "v" else a.map(ts, 0.16)
        err[kind].append(out - true)                  # how far the file is ahead of what the camera made
    return watch, err


def test_ten_minutes_of_a_camera_whose_sender_reports_do_not_fit(monkeypatch):
    """Measured on four cameras of one make and replayed here in small: every step forward was a gap
    and every step back was dropped, so the file ran ahead of the camera by seconds a minute, each
    stream by its own amount. Now neither stream leaves the camera's own count of frames and samples
    once the first step back has been seen."""
    packets = _sloppy_camera(600)
    watch, err = _record(monkeypatch, packets)
    assert watch.on
    for kind in "va":
        settled = err[kind][len(err[kind]) // 20:]
        assert max(settled) - min(settled) < 0.05, kind
    assert abs(err["a"][-1] - err["v"][-1]) < 0.7        # as they started (bursts of 0.4 and 0.64 s)

    monkeypatch.setenv("NOVA_TIMESTAMP_STEPS", "keep")
    watch, old = _record(monkeypatch, packets)
    assert old["v"][-1] - old["v"][0] > 20 and old["a"][-1] - old["a"][0] > 20     # what it was


def test_recorder_and_streamer_of_a_source_share_what_its_timestamps_showed():
    class Worker: url = "rtsp://camera/stream"
    w = Worker()
    assert timestamp_watch(w) is timestamp_watch(w) and timestamp_watch(w).live
    class File: url = "/videos/clip.mp4"
    assert not timestamp_watch(File()).live
    class Device: url = "0"
    assert not timestamp_watch(Device()).live
