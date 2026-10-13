"""Segment boundaries on the local wall clock (incl. DST), segment names after outages and failures."""
import os
import time

import pytest

import logsync  # noqa: F401  (logging.verbose)
import recorder
from avwriter import OutputOpenError
from test_output_recorder import FPS, Clock, app, frame, make_recorder, segments  # noqa: F401  (app: fixture)

pytestmark = pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs time.tzset() to switch time zones")


@pytest.fixture
def tz(monkeypatch):
    def use(name):
        monkeypatch.setenv("TZ", name)
        time.tzset()
    yield use
    monkeypatch.undo()
    time.tzset()


def at(y, mo, d, h, mi, s=0, dst=-1):
    return time.mktime((y, mo, d, h, mi, s, 0, 0, dst))


def wall(t):
    return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(t))


def test_boundaries_follow_the_local_clock(tz):
    tz("Europe/Berlin")
    assert wall(recorder.next_boundary(at(2026, 10, 14, 10, 10), 1800)) == "2026-10-14 10:30 CEST"
    assert wall(recorder.next_boundary(at(2026, 10, 14, 10, 30), 1800)) == "2026-10-14 11:00 CEST"
    # 24h: local midnight, not 02:00 (UTC midnight)
    assert wall(recorder.next_boundary(at(2026, 10, 14, 12, 0), 86400)) == "2026-10-15 00:00 CEST"
    # an interval that does not divide the day starts again at midnight
    assert wall(recorder.next_boundary(at(2026, 10, 14, 23, 58), 7 * 60)) == "2026-10-15 00:00 CEST"
    # 48h: every other local midnight
    a = recorder.next_boundary(at(2026, 10, 14, 12, 0), 2 * 86400)
    assert wall(a).endswith("00:00 CEST") and recorder.next_boundary(a, 2 * 86400) - a == 2 * 86400
    tz("Asia/Kolkata")                                        # +5:30: hours start at :00, not :30
    assert wall(recorder.next_boundary(at(2026, 10, 14, 10, 10), 3600)) == "2026-10-14 11:00 IST"
    assert wall(recorder.next_boundary(at(2026, 10, 14, 10, 10), 86400)) == "2026-10-15 00:00 IST"


def test_fall_back_night(tz):
    tz("Europe/Berlin")                                       # 2026-10-25 03:00 CEST -> 02:00 CET
    t = at(2026, 10, 25, 1, 30)
    marks = []
    for _ in range(4):
        t = recorder.next_boundary(t, 3600)
        marks.append(wall(t))
    assert marks == ["2026-10-25 02:00 CEST", "2026-10-25 02:00 CET", "2026-10-25 03:00 CET", "2026-10-25 04:00 CET"]
    a = recorder.next_boundary(at(2026, 10, 25, 2, 40, dst=1), 1800)      # inside the first 02:xx hour
    assert wall(a) == "2026-10-25 02:00 CET"
    assert wall(recorder.next_boundary(a, 1800)) == "2026-10-25 02:30 CET"
    assert recorder.next_boundary(at(2026, 10, 25, 0, 0), 86400) - at(2026, 10, 25, 0, 0) == 25 * 3600


def test_spring_forward_night(tz):
    tz("Europe/Berlin")                                       # 2026-03-29 02:00 CET -> 03:00 CEST
    assert wall(recorder.next_boundary(at(2026, 3, 29, 1, 30), 3600)) == "2026-03-29 03:00 CEST"
    assert wall(recorder.next_boundary(at(2026, 3, 29, 1, 45), 1800)) == "2026-03-29 03:00 CEST"
    assert wall(recorder.next_boundary(at(2026, 3, 29, 1, 0), 2 * 3600)) == "2026-03-29 04:00 CEST"
    tz("Australia/Lord_Howe")                                 # a 30-minute DST shift
    t, seen = at(2026, 4, 4, 22, 0), []
    for _ in range(8):
        t2 = recorder.next_boundary(t, 1800)
        assert 0 < t2 - t <= 1800
        t = t2
        seen.append(time.strftime("%H:%M", time.localtime(t)))
    assert seen.count("01:30") == 2                           # 02:00 LHDT -> 01:30 LHST


def feed(r, clock, times, i=0):
    for t in times:
        clock.t = t
        r.write(1, frame(i))
        i += 1
    return i


def test_segment_names_on_the_fall_back_night(app, tz, tmp_path, monkeypatch):
    tz("Europe/Berlin")
    r = make_recorder(tmp_path, monkeypatch)
    r.set_interval(3600)
    clock = Clock(0)
    monkeypatch.setattr(recorder, "time", clock)
    r._open_writer()
    t0 = at(2026, 10, 25, 1, 58)
    feed(r, clock, [t0 + 20 * k for k in range(int(2.2 * 3600 / 20))])   # a frame every 20 s until 03:09:40 CET
    r._close_writer()
    assert segments(tmp_path / "videos" / "cam") == [
        "20261025_0158_0200.mp4", "20261025_0200_0200.mp4", "20261025_0200_0300.mp4", "20261025_0300_0309.mp4"]


def test_an_outage_ends_the_segment_at_its_last_frame(app, tz, tmp_path, monkeypatch):
    tz("Europe/Berlin")
    r = make_recorder(tmp_path, monkeypatch)
    r.set_interval(1800)
    clock = Clock(0)
    monkeypatch.setattr(recorder, "time", clock)
    r._open_writer()
    t0 = at(2026, 10, 14, 10, 20)
    i = feed(r, clock, [t0 + k / FPS for k in range(30 * FPS)])                   # 10:20:00 .. 10:20:30
    i = feed(r, clock, [at(2026, 10, 14, 12, 40) + k / FPS for k in range(FPS)], i)   # back 2 h 20 min later
    clock.t = at(2026, 10, 14, 12, 45)                         # and gone again when Nova stops 5 min later
    r._close_writer()
    assert segments(tmp_path / "videos" / "cam") == ["20261014_1020_1020.mp4", "20261014_1240_1240.mp4"]


def test_a_segment_that_cannot_start_leaves_the_previous_one_correctly_named(app, tz, tmp_path, monkeypatch):
    tz("Europe/Berlin")
    r = make_recorder(tmp_path, monkeypatch)
    r.set_interval(60)
    clock = Clock(0)
    monkeypatch.setattr(recorder, "time", clock)
    real = recorder.AVWriter
    made = []

    def writer(*a, **kw):
        made.append(1)
        if len(made) == 2:
            raise RuntimeError("encoder exploded")
        return real(*a, **kw)

    monkeypatch.setattr(recorder, "AVWriter", writer)
    r._open_writer()
    t0 = at(2026, 10, 14, 10, 29, 50)
    i = feed(r, clock, [t0 + k / FPS for k in range(10 * FPS)])
    start, nxt = r.start_ts, r.next_ts
    with pytest.raises(OutputOpenError, match="encoder exploded"):
        feed(r, clock, [at(2026, 10, 14, 10, 30, 0)], i)
    assert (r.start_ts, r.next_ts, r.writer) == (start, nxt, None)
    r._close_writer()
    assert segments(tmp_path / "videos" / "cam") == ["20261014_1029_1030.mp4"]


def test_the_minimum_interval_can_be_lowered_for_tests(monkeypatch, app, tmp_path):
    r = make_recorder(tmp_path, monkeypatch)
    r.set_interval(4)
    assert r.interval_s == 60
    monkeypatch.setenv("NOVA_MIN_RECORD_INTERVAL", "1")
    r.set_interval(4)
    assert r.interval_s == 4
