"""Camera window behaviour with a fake core on the offscreen platform: zoom and geometry persistence,
mouse and keyboard handling. QSettings point at a temp dir (conftest), never at the user's Nova.conf."""
import os
import sys
from collections import deque

import pytest

# The offscreen platform would open $DISPLAY for GLX contexts as soon as a QOpenGLWindow is exposed;
# these tests need no OpenGL and must not touch a real display.
os.environ.setdefault("QT_QPA_OFFSCREEN_NO_GLX", "1")

from PySide6.QtCore import QObject, Qt, Signal, QSettings, QRect, QPoint, QPointF, QEvent
from PySide6.QtGui import QGuiApplication, QImage, QKeyEvent, QMouseEvent, QWheelEvent

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import logsync  # noqa: E402,F401  (logging.trace / logging.verbose, as nova.py installs them)


class FakeStreamer:
    target_url = None


class FakeCore(QObject):
    """The parts of NovaCore the window uses; the frame size is what VideoWorker.width()/height() return."""
    img_sig = Signal(); stat_sig = Signal(str); sz_sig = Signal(int, int)
    exit_sig = Signal(); init_done_sig = Signal(); bt_sig = Signal()

    def __init__(self, key):
        super().__init__()
        self.key, self.w, self.h = key, 0, 0
        self.streamer = FakeStreamer()
        self.display_name = key
        self.fq = deque(maxlen=1)
        self.icon = QImage(8, 8, QImage.Format_RGBA8888)
        self.calls = []
        self.rec = self.aud = self.strm = False

    def hash(self): return self.key
    def get_url(self): return "rtsp://admin:s3cret@10.0.0.1:554/stream_0"
    def get_frame_q(self): return self.fq
    def width(self): return self.w
    def height(self): return self.h
    def ratio(self): return self.w / self.h if self.h else 1.0
    def is_streamer_on(self): return self.strm
    def is_recorder_on(self): return self.rec
    def is_audio_on(self): return self.aud
    def get_streamer_icon(self): return self.icon
    def get_recorder_icon(self): return self.icon
    def get_audio_icon(self): return self.icon
    def toggle_recorder(self): self.rec = not self.rec; self.calls.append("rec")
    def toggle_audio(self): self.aud = not self.aud; self.calls.append("aud")
    def toggle_streamer(self): self.strm = not self.strm; self.calls.append("strm")
    def snapshot(self, df): self.calls.append("snap")

    def connected(self, w, h):
        """First frame of a connection: VideoWorker sets its size, then emits sz_sig."""
        self.w, self.h = w, h
        self.sz_sig.emit(w, h)

    def reconnecting(self):
        """VideoWorker._stream_loop resets its size to 0 at the start of every connection."""
        self.w = self.h = 0


def headless(cls):
    """The window class with painting switched off: without an OpenGL context (no GPU, no display) Qt's
    QOpenGLWindow crashes when it is exposed. Geometry, window states and input work as usual."""
    class Headless(cls):
        def exposeEvent(self, e): pass
        def event(self, e):
            if e.type() in (QEvent.Expose, QEvent.UpdateRequest, QEvent.Paint):
                return True
            return super().event(e)
    return Headless


@pytest.fixture(scope="module")
def app():
    a = QGuiApplication.instance() or QGuiApplication([sys.argv[0]])
    a.setOrganizationName("judvi-test")
    a.setApplicationName("NovaTest")
    assert not QSettings().fileName().startswith(os.path.expanduser("~/.config")), QSettings().fileName()
    return a


@pytest.fixture
def make(app, request):
    """make(geom=None, zoom=None, **kw) -> (window, core) with a fresh settings key per test."""
    from view import Nova
    made = []

    def _make(geom=None, zoom=None, key=None, cls=Nova, **kw):
        cls = headless(cls)
        key = key or f"t_{request.node.name}"[:60]
        s = QSettings()
        for k, v in ((f"geom_{key}", geom), (f"zoom_{key}", zoom)):
            if v is None: s.remove(k)
            else: s.setValue(k, v)
        s.sync()
        core = FakeCore(key)
        w = cls(core, **kw)
        app.processEvents()
        made.append(w)
        return w, core

    yield _make
    for w in made:
        w.cleanup()
        w.hide()
        w.deleteLater()
    app.processEvents()


def stored(core):
    s = QSettings()
    s.sync()
    return s.value(f"geom_{core.key}"), s.value(f"zoom_{core.key}")


def ratios(text):
    return tuple(float(v) for v in text.split(","))


# ---------------------------------------------------------------- zoom across reconnects

def test_zoom_survives_a_reconnect_with_the_same_size(make):
    w, core = make("100,100,640,360", "0.25,0.25,0.5,0.5")
    core.connected(1920, 1080)
    assert w.zoomer.getRect() == (480, 270, 960, 540)
    w.zoomer.zoom(None, 1)                       # the user zooms further in
    rect = w.zoomer.getRect()
    zoomer = w.zoomer
    core.reconnecting()
    w._persist_if_changed()                      # the 2 s timer fires while the decoder reconnects
    assert ratios(stored(core)[1]) != (0.0, 0.0, 1.0, 1.0)
    core.connected(1920, 1080)                   # first frame after the reconnect: same size
    assert w.zoomer is zoomer and w.zoomer.getRect() == rect
    w._persist_if_changed()
    rx, ry, rw, rh = ratios(stored(core)[1])
    assert (rx * 1920, ry * 1080, rw * 1920, rh * 1080) == pytest.approx(rect)


def test_zoom_is_carried_over_as_ratios_when_the_size_changes(make):
    w, core = make("100,100,640,360", "0.25,0.25,0.5,0.5")
    core.connected(1920, 1080)
    core.reconnecting()
    core.connected(1280, 720)                    # the camera's stream was switched to 720p
    assert w.zoomer.getRect() == (320, 180, 640, 360)
    w._persist_if_changed()
    assert ratios(stored(core)[1]) == (0.25, 0.25, 0.5, 0.5)


def test_no_zoom_is_persisted_as_a_guess_before_the_first_frame(make):
    w, core = make("100,100,640,360", "0.1,0.2,0.3,0.4")
    w._persist_if_changed()                      # camera still connecting: size 0
    assert ratios(stored(core)[1]) == (0.1, 0.2, 0.3, 0.4)
    w.cleanup()                                  # nova stop before the camera ever answered
    assert ratios(stored(core)[1]) == (0.1, 0.2, 0.3, 0.4)


def test_zoom_round_trips_without_drift(make):
    # The ratios are written as zx / vw; reading them back with int() lost a pixel for some offsets
    # (225 of 13,625 in the review), and every restart lost another one.
    w, core = make("100,100,640,360", None)
    core.connected(1920, 1080)
    for rect in ((1001, 3, 917, 515), (7, 11, 1913, 1069), (571, 289, 1003, 563)):
        w.zoomer.set_rect(QRect(*rect))
        w.cleanup()
        z = stored(core)[1]
        for _ in range(5):                       # five restarts
            w2, core2 = make(None, z, key=core.key)
            core2.connected(1920, 1080)
            assert w2.zoomer.getRect() == rect
            w2.cleanup()
            z = stored(core2)[1]
        w.is_running = True


def test_escape_before_the_first_frame_resets_the_remembered_zoom(make):
    w, core = make("100,100,640,360", "0.25,0.25,0.5,0.5")
    w.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    core.connected(1920, 1080)
    assert not w.zoomer.is_zoom()


# ---------------------------------------------------------------- geometry on record

def screen_rect():
    return QGuiApplication.primaryScreen().geometry()


def test_exit_while_fullscreen_keeps_the_normal_geometry(make, app):
    w, core = make("200,100,320,180", None)
    core.connected(1920, 1080)
    w.fullscreen()
    app.processEvents()
    assert w.geometry() == screen_rect()
    w._persist_if_changed()                      # the 2 s timer while fullscreen
    assert stored(core)[0] == "200,100,320,180"
    w.zoomer.zoom(None, 1)                       # zooming in fullscreen is kept
    w.cleanup()                                  # nova stop / Ctrl-C while fullscreen
    geom, zoom = stored(core)
    assert geom == "200,100,320,180"
    assert ratios(zoom) != (0.0, 0.0, 1.0, 1.0)


def test_exit_while_maximized_keeps_the_normal_geometry(make, app):
    w, core = make("200,100,320,180", None)
    w.showMaximized()                            # e.g. the window manager's maximize shortcut
    app.processEvents()
    assert w.geometry().width() > 320
    w.cleanup()
    assert stored(core)[0] == "200,100,320,180"


def test_a_place_on_a_missing_monitor_opens_on_the_primary_screen(make, app):
    w, core = make("6000,100,640,360", None)    # the second monitor is not connected
    assert screen_rect().contains(w.geometry())
    assert (w.width(), w.height()) == (640, 360)
    w._persist_if_changed()
    assert stored(core)[0] == "6000,100,640,360"  # kept for when the monitor is back
    w.move_left()                                # the user places the window: that is its place now
    w._persist_if_changed()
    g = w.geometry()
    assert stored(core)[0] == f"{g.x()},{g.y()},{g.width()},{g.height()}"


def test_a_larger_than_screen_window_off_screen_is_shrunk_with_its_ratio(make):
    s = screen_rect()
    w, core = make(f"9000,9000,{s.width() * 2},{s.height()}", None)
    assert screen_rect().contains(w.geometry())
    assert w.width() == s.width() and w.height() == s.height() // 2


def test_a_partly_visible_place_is_left_alone(make):
    s = screen_rect()
    x = s.right() - 100                          # 100 px of the window on the screen
    w, core = make(f"{x},50,320,180", None)
    assert (w.x(), w.y()) == (x, 50)


def test_window_manager_moves_after_a_monitor_change_are_not_persisted(make, app):
    w, core = make("100,100,320,180", None)
    w._persist_if_changed()
    w._on_screens_changed()                      # QGuiApplication.screenRemoved / screenAdded
    w.setGeometry(400, 300, 320, 180)            # the window manager moves the window
    app.processEvents()
    w._persist_if_changed()
    assert stored(core)[0] == "100,100,320,180"
    w._screens_settled()                         # SCREEN_SETTLE_MS later: the place is on a screen
    app.processEvents()
    assert w.geometry() == QRect(100, 100, 320, 180)
    w.setGeometry(140, 100, 320, 180)            # afterwards moves are the user's again
    app.processEvents()
    w._persist_if_changed()
    assert stored(core)[0] == "140,100,320,180"


def test_place_kept_while_its_monitor_sleeps_and_restored_when_it_returns(make, app, monkeypatch):
    w, core = make("100,100,320,180", None)
    w._persist_if_changed()
    real = w._screen_rects()
    monkeypatch.setattr(w, "_screen_rects", lambda: [QRect(1000, 0, 800, 600)])   # its monitor dropped out
    w._on_screens_changed()
    w._screens_settled()
    w._persist_if_changed()
    assert w._geom_hold and stored(core)[0] == "100,100,320,180"
    w.setGeometry(1100, 100, 320, 180)           # where the window manager put it, overnight
    app.processEvents()
    w._persist_if_changed()
    w.cleanup(); w.is_running = True             # even a nova stop in between keeps the place
    assert stored(core)[0] == "100,100,320,180"
    monkeypatch.setattr(w, "_screen_rects", lambda: real)                        # it wakes up
    w._on_screens_changed()
    w._screens_settled()
    app.processEvents()
    assert w.geometry() == QRect(100, 100, 320, 180) and not w._geom_hold


def test_a_user_move_during_the_hold_wins(make, app):
    w, core = make("100,100,320,180", None)
    w._on_screens_changed()
    w.setGeometry(400, 300, 320, 180)            # moved by the window manager ...
    w.move_right()                               # ... then by the user
    app.processEvents()
    w._screens_settled()
    w._persist_if_changed()
    assert stored(core)[0] == f"{w.x()},{w.y()},320,180" and w.x() != 100
