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


# ---------------------------------------------------------------- mouse

class Mouse:
    """Mouse events as Qt delivers them to the window, in window-local coordinates."""
    def __init__(self, w):
        self.w = w

    def ev(self, x, y, button=Qt.LeftButton, buttons=None, dbl=False, delta=0):
        w = self.w
        g = w.mapToGlobal(QPoint(x, y))
        from types import SimpleNamespace
        flags = Qt.MouseEventCreatedDoubleClick if dbl else Qt.NoMouseEventFlag
        return SimpleNamespace(
            button=lambda: button, buttons=lambda: (button if buttons is None else buttons),
            flags=lambda: flags, position=lambda: QPointF(x, y), globalPosition=lambda: QPointF(g),
            angleDelta=lambda: QPoint(0, delta), accept=lambda: None)

    def press(self, x, y, **kw): self.w.mousePressEvent(self.ev(x, y, **kw))
    def release(self, x, y, **kw): self.w.mouseReleaseEvent(self.ev(x, y, buttons=Qt.NoButton, **kw))
    def move(self, x, y, **kw): self.w.mouseMoveEvent(self.ev(x, y, button=Qt.NoButton, buttons=Qt.LeftButton, **kw))
    def wheel(self, x, y, delta): self.w.wheelEvent(self.ev(x, y, button=Qt.NoButton, delta=delta))

    def double_click(self, x, y):
        """Qt: press, release, press (flagged as part of a double-click), double-click, release."""
        self.press(x, y); self.release(x, y)
        self.press(x, y, dbl=True)
        self.w.mouseDoubleClickEvent(self.ev(x, y))
        self.release(x, y)


def spy(monkeypatch, w, name):
    calls = []
    orig = getattr(w, name)
    def wrapper(*a, **kw):
        calls.append(a)
        return orig(*a, **kw)
    monkeypatch.setattr(w, name, wrapper)
    return calls


def test_double_click_toggles_fullscreen_without_snapping_the_stale_rectangle(make, app, monkeypatch):
    w, core = make("200,100,320,180", None)
    core.connected(1920, 1080)
    m = Mouse(w)
    snaps = spy(monkeypatch, w, "align_grid")
    m.double_click(100, 100)
    app.processEvents()
    assert w.is_fullscreen()
    m.double_click(100, 100)                     # leave fullscreen
    app.processEvents()
    assert not w.is_fullscreen()
    # one snap for the first click of each double-click (window was normal / fullscreen), none for the
    # release after the double-click: geometry() is still the fullscreen rectangle at that moment
    assert len(snaps) == 2
    assert w.geometry() == QRect(200, 100, 320, 180)
    m.press(100, 100); m.release(100, 100)      # an ordinary click afterwards snaps again
    assert len(snaps) == 3


def test_double_click_on_a_control_strip_icon_toggles_it_once(make, app):
    w, core = make("200,100,640,360", None)
    core.connected(1920, 1080)
    m = Mouse(w)
    m.move(10, 10)                               # the control strip appears
    x = w._m_x + w._m_sz // 2
    y = w._m_y + (w._m_sz + 4) * 1 + w._m_sz // 2   # the record icon
    assert w.in_bt(QPoint(x, y), 1)
    m.double_click(x, y)
    app.processEvents()
    assert core.calls == ["rec"] and core.rec
    assert not w.is_fullscreen()


def test_wheel_and_middle_click_repaint(make, monkeypatch):
    w, core = make("200,100,640,360", None)
    core.connected(1920, 1080)
    repaints = spy(monkeypatch, w, "update")
    Mouse(w).wheel(320, 180, -120)
    assert w.zoomer.is_zoom() and repaints
    repaints.clear()
    Mouse(w).press(320, 180, button=Qt.MiddleButton)
    assert repaints


def test_a_wheel_notch_zooms_ten_percent_and_fine_steps_zoom_by_their_fraction(make):
    w, core = make("200,100,640,360", None)
    core.connected(1920, 1080)
    m = Mouse(w)
    for _ in range(5):
        w.zoomer.zoom(None, 1)                   # start zoomed in, away from the edges
    start = w.zoomer.getRect()

    from zoomer import Zoomer
    ref = Zoomer(1920, 1080, 640, 360)
    ref.setRect(*start)
    ref.zoom(QPoint(300, 170), 1)                # what a notch has always done: 10 % (key I)
    m.wheel(300, 170, -120)
    assert w.zoomer.getRect() == pytest.approx(ref.getRect())

    w.zoomer.setRect(*start)
    for _ in range(8):                           # a hi-res wheel: 8 events of 15 make one notch
        m.wheel(300, 170, -15)
    assert w.zoomer.getRect() == pytest.approx(ref.getRect(), abs=0.5)

    w.zoomer.setRect(*start)
    m.wheel(300, 170, -15)
    assert w.zoomer.width() == pytest.approx(start[2] * (10 / 11) ** 0.125)


def test_move_and_resize_by_hand_where_the_platform_cannot(make, app):
    # The offscreen platform, like macOS for startSystemResize, does not move or resize windows itself.
    w, core = make("200,100,320,180", None, grid=1)
    core.connected(1920, 1080)
    m = Mouse(w)
    m.press(200, 100)                            # right of the control strip
    m.move(250, 130)
    assert (w.x(), w.y()) == (250, 130)
    m.release(250, 130)
    m.press(318, 178)                            # bottom-right corner
    m.move(358, 198)
    assert (w.width(), w.height()) == (360, 200)
    m.release(358, 198)
    m.press(1, 90)                               # left edge: the right edge stays
    right = w.x() + w.width()
    m.move(-29, 90)
    assert w.x() + w.width() == right and w.width() == 390
    m.release(-29, 90)
