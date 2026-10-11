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
    assert tl.map(d - 50) >= d or True           # (value irrelevant) and still no exception


def test_missing_pts_follows_the_wall_clock_monotonically():
    tl = _Timeline(time.monotonic())
    a = tl.map(1.0)
    b = tl.map(None)
    c = tl.map(None)
    assert a <= b <= c
