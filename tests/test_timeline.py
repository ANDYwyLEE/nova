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
