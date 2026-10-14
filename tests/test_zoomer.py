"""Zoomer: the zoom is a region of the picture; the window shows it widened to its own shape (view_rect)."""
import pytest
from PySide6.QtCore import QRect

import logsync  # noqa: F401  (installs logging.trace before zoomer logs)
from zoomer import Zoomer

REGION = (768, 108, 384, 864)      # a tall region of a 1920x1080 frame, the shape of a 400x900 window


def tall():
    z = Zoomer(1920, 1080, 400, 900)
    z.set_rect(QRect(*REGION))
    return z


def test_resizing_keeps_the_region_and_shows_the_picture_around_it():
    z = tall()
    assert z.view_rect().getRect() == REGION and z.scale() == pytest.approx(900 / 864)
    z.resize(1600, 900)                                     # widened: the sides show the picture
    assert z.getRect() == REGION                            # the region is untouched
    v = z.view_rect()
    assert v.height() == 864 and v.width() == pytest.approx(864 * 1600 / 900)
    assert v.center().x() == pytest.approx(768 + 192) and z.ratio() == pytest.approx(1600 / 900)
    assert z.roi_ratio() == pytest.approx(384 / 864) and z.scale() == pytest.approx(900 / 864)
    z.resize(400, 1200)                                     # taller: more above and below, until the frame ends
    v = z.view_rect()
    assert (v.width(), v.height(), v.y()) == (384, 1080, 0) and v.contains(QRect(*REGION))
    assert z.ratio() == pytest.approx(384 / 1080)           # the rest of the window is letterboxed
    z.resize(400, 900)                                      # and back: exactly the region
    assert z.view_rect().getRect() == REGION


def test_view_stops_at_the_frame_edge_and_letterboxes_only_what_no_picture_can_fill():
    z = Zoomer(1920, 1080, 1600, 900)
    z.set_rect(QRect(0, 108, 384, 864))                     # region at the left edge: the view shifts right
    v = z.view_rect()
    assert v.x() == 0 and v.width() == pytest.approx(1536)
    z = Zoomer(1920, 1080, 2000, 900)                       # wider than the frame can fill
    z.set_rect(QRect(100, 0, 200, 1080))
    v = z.view_rect()
    assert (v.width(), v.height()) == (1920, 1080) and z.ratio() == pytest.approx(16 / 9)   # bars at the sides
    z = Zoomer(1920, 1080, 400, 900)                        # no zoom: the whole frame, as before
    assert z.view_rect().getRect() == (0, 0, 1920, 1080) and z.ratio() == pytest.approx(16 / 9)


def test_wheel_pan_and_middle_click_act_on_what_is_shown():
    z = tall()
    z.resize(1600, 900)
    z.zoom(None, 1)                                         # one notch in, from the widened view
    assert z.width() == pytest.approx(1536 * 10 / 11) and z.roi_ratio() == pytest.approx(1600 / 900)
    z = tall()
    z.pan(100, 0)                                           # the picture follows the pointer: 100 px right
    assert z.x() == pytest.approx(768 - 100 * 864 / 900) and z.getRect()[2:] == (384, 864)
    z.pan(-100000, 0); z.pan(0, 100000)                     # stops at the frame's edges
    assert (z.x(), z.y()) == (1920 - 384, 0)
    z = tall()
    z.resize(1600, 900)
    z.fit_widget()                                          # middle click: the shown picture is the region now
    assert z.getRect() == tuple(round(v) for v in z.view_rect().getRect()) and z.width() == pytest.approx(1536)
    z = Zoomer(1920, 1080, 400, 900)
    z.fit_widget()                                          # unzoomed: the frame cut to the window's shape
    assert z.is_zoom() and z.height() == 1080 and z.width() == pytest.approx(480)
    z.resize(400, 450); z.reset()                           # Esc: the whole frame again
    assert not z.is_zoom()
