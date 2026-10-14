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
from util import WindowStore  # noqa: E402


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
        store = WindowStore()
        if geom is None and zoom is None: store.remove(key)
        else: store.write(key, geom, zoom)
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
    return WindowStore().read(core.key)


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


def test_picture_keeps_its_aspect_ratio_while_the_camera_reconnects(make):
    # core.ratio() is 1.0 while the decoder's size is 0: a repaint ("Connecting...") squeezed the last
    # frame to a square and moved the control strip.
    w, core = make("100,100,640,360", None)
    core.connected(1920, 1080)
    strip = w._m_area
    core.reconnecting()
    w.update_stat("Connecting...")
    w.set_bt()
    assert w._pic_ratio() == pytest.approx(16 / 9) and w._m_area == strip


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

    def ev(self, x, y, button=Qt.LeftButton, buttons=None, dbl=False, delta=0, mods=Qt.NoModifier, inverted=False):
        w = self.w
        g = w.mapToGlobal(QPoint(x, y))
        from types import SimpleNamespace
        flags = Qt.MouseEventCreatedDoubleClick if dbl else Qt.NoMouseEventFlag
        return SimpleNamespace(
            button=lambda: button, buttons=lambda: (button if buttons is None else buttons),
            flags=lambda: flags, position=lambda: QPointF(x, y), globalPosition=lambda: QPointF(g),
            angleDelta=lambda: QPoint(0, delta), accept=lambda: None, modifiers=lambda: mods,
            inverted=lambda: inverted)

    def press(self, x, y, **kw): self.w.mousePressEvent(self.ev(x, y, **kw))
    def release(self, x, y, **kw): self.w.mouseReleaseEvent(self.ev(x, y, buttons=Qt.NoButton, **kw))
    def move(self, x, y, **kw): self.w.mouseMoveEvent(self.ev(x, y, button=Qt.NoButton, buttons=Qt.LeftButton, **kw))
    def wheel(self, x, y, delta, **kw): self.w.wheelEvent(self.ev(x, y, button=Qt.NoButton, delta=delta, **kw))

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
    Mouse(w).wheel(320, 180, 120)                   # pushed away: zoom in
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
    ref.zoom(QPoint(300, 170), 1)                # one notch = 10 %, like key I
    m.wheel(300, 170, 120)                      # pushed away: in
    assert w.zoomer.getRect() == pytest.approx(ref.getRect())

    w.zoomer.setRect(*start)
    for _ in range(8):                           # a hi-res wheel: 8 events of 15 make one notch
        m.wheel(300, 170, 15)
    assert w.zoomer.getRect() == pytest.approx(ref.getRect(), abs=0.5)

    w.zoomer.setRect(*start)
    m.wheel(300, 170, 15)
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


# ---------------------------------------------------------------- keyboard

def key(w, k, mods=Qt.NoModifier, repeat=False):
    w.keyPressEvent(QKeyEvent(QEvent.KeyPress, k, mods, "", repeat))


def test_held_keys_do_not_repeat_toggles_and_one_shot_actions(make, app):
    w, core = make("200,100,320,180", None, grid=20)
    core.connected(1920, 1080)
    key(w, Qt.Key_R)
    for _ in range(10):
        key(w, Qt.Key_R, repeat=True)            # R held: recording on once, not on/off/on/...
        key(w, Qt.Key_S, repeat=True)
        key(w, Qt.Key_A, repeat=True)
    assert core.calls == ["rec"] and core.rec
    key(w, Qt.Key_F)
    for _ in range(5):
        key(w, Qt.Key_F, repeat=True)
        key(w, Qt.Key_H, repeat=True)
    app.processEvents()
    assert w.is_fullscreen() and not w.helper.is_on()


def test_held_arrow_and_zoom_keys_still_repeat(make, app):
    w, core = make("200,100,320,180", None, grid=20)
    core.connected(1920, 1080)
    key(w, Qt.Key_Right)
    for _ in range(4):
        key(w, Qt.Key_Right, repeat=True)
    app.processEvents()
    assert w.x() == 300
    key(w, Qt.Key_I)
    one = w.zoomer.width()
    for _ in range(3):
        key(w, Qt.Key_I, repeat=True)
    assert w.zoomer.width() < one


# ---------------------------------------------------------------- OSD

class RecordingPainter:
    def __init__(self): self.drawn = []
    def setRenderHint(self, *a): pass
    def drawImage(self, *a): self.drawn.append(a[-1])


def osd_layers(w):
    w.set_txt("Unreachable: 10.0.0.1:554")
    w.set_hlp("H : help")
    w.set_bt()
    w.border = True
    w.set_bg("orange")
    return {k: getattr(w, f"osd_{k}") for k in ("txt", "hlp", "bt", "bg")}


def test_status_text_is_drawn_under_help_and_control_strip(make):
    w, core = make("200,100,320,180", None)
    layers = osd_layers(w)
    p = RecordingPainter()
    w._render(p)
    order = [k for img in p.drawn for k, l in layers.items() if l is img]
    assert order == ["txt", "hlp", "bt", "bg"]


def test_opengl_draws_the_osd_layers_in_the_same_order(make, monkeypatch):
    import novagl
    from novagl import NovaGL
    for name in dir(novagl):                     # no GL context here: every GL call is a no-op
        if name.startswith("gl"):
            monkeypatch.setattr(novagl, name, lambda *a, **k: None)
    w, core = make("200,100,320,180", None, cls=NovaGL)
    layers = osd_layers(w)
    w.shader_program, w.loc = 1, {"mode": 0}
    w.osd_texture_ids = {k: i for i, k in enumerate(layers)}
    drawn = []
    monkeypatch.setattr(w, "_upload_osd", lambda tid, img, nearest=False: None)
    monkeypatch.setattr(w, "_draw_quad_fill", lambda tid: drawn.append(tid))
    w.paintGL()
    assert [list(layers)[t] for t in drawn] == ["txt", "hlp", "bt", "bg"]


def test_osd_layers_have_device_pixels_on_hidpi_screens(make, monkeypatch):
    w, core = make("200,100,320,180", None)
    core.connected(1920, 1080)
    monkeypatch.setattr(w, "devicePixelRatio", lambda: 2.0)
    layers = osd_layers(w)
    for k, img in layers.items():
        assert (img.width(), img.height(), img.devicePixelRatio()) == (640, 360, 2.0), k
    # drawn in window coordinates: the dimming covers the whole layer, the border runs along its edge
    assert layers["txt"].pixelColor(638, 358).alpha() == 160
    assert layers["bg"].pixelColor(0, 180).alpha() > 0 and layers["bg"].pixelColor(4, 180).alpha() == 0


def test_border_colour_follows_focus_after_a_resize(make, app):
    w, core = make("200,100,320,180", None, border=True)
    other, _ = make("600,100,320,180", None, key="other_window", border=True)
    w.requestActivate(); app.processEvents()
    w._set_focus(True)
    other.requestActivate(); app.processEvents()
    w._set_focus(False)                          # as WindowDeactivate / focusWindowChanged deliver it
    assert w.osd_bg.pixelColor(0, 90).green() > w.osd_bg.pixelColor(0, 90).red()
    w.setGeometry(200, 100, 360, 200)            # resized (keys, layout, window manager) while unfocused
    app.processEvents()
    c = w.osd_bg.pixelColor(0, 100)
    assert c.green() > c.red(), "unfocused window painted with the focused (orange) border"


# ---------------------------------------------------------------- OpenGL render failures

class FakePainter:
    CompositionMode_Source = CompositionMode_SourceOver = 0
    drawn = []
    def __init__(self, dev): pass
    def setCompositionMode(self, m): pass
    def setRenderHint(self, *a): pass
    def fillRect(self, *a): pass
    def drawImage(self, *a): FakePainter.drawn.append(a[-1])
    def end(self): pass


@pytest.fixture
def gl_window(make, monkeypatch):
    """A NovaGL window whose GL calls are no-ops (there is no context here)."""
    import novagl
    from novagl import NovaGL
    for name in dir(novagl):
        if name.startswith("gl"):
            monkeypatch.setattr(novagl, name, lambda *a, **k: None)
    monkeypatch.setattr(novagl, "QPainter", FakePainter)
    FakePainter.drawn = []
    w, core = make("200,100,320,180", None, cls=NovaGL)
    w.shader_program, w.loc = 1, {"mode": 0}
    w.osd_texture_ids = {k: i for i, k in enumerate(("txt", "hlp", "bt", "bg"))}
    return w, core


def render_logs(caplog, level):
    return [r for r in caplog.records if r.levelno == level and "[NovaGL]" in r.getMessage()]


def test_a_failing_renderer_logs_rarely_rebuilds_once_then_gives_up(gl_window, monkeypatch, caplog):
    import logging
    w, core = gl_window
    def frame(): raise RuntimeError("GL_INVALID_OPERATION")
    def init():
        rebuilds.append(1); w.shader_program = 2
        return True
    rebuilds = []
    monkeypatch.setattr(w, "_paint_frame", frame)
    monkeypatch.setattr(w, "_init_gl", init)
    with caplog.at_level(logging.INFO):
        for _ in range(300):                     # ten seconds of frames
            w.paintGL()
    errors = render_logs(caplog, logging.ERROR)
    assert len(errors) == 2, [r.getMessage()[:80] for r in errors]    # first failure, giving up
    assert "Traceback" in errors[0].getMessage() and "stopped" in errors[1].getMessage()
    assert len(render_logs(caplog, logging.WARNING)) == 1 and rebuilds == [1]
    assert w._gl_dead and w.render_error
    FakePainter.drawn = []
    w.paintGL()                                  # from now on only the OSD, with the message
    assert w.osd_txt in FakePainter.drawn
    core.stat_sig.emit("Connecting...\n10.0.0.1")
    core.fq.append(object())
    w.update_img()
    assert w.curr_txt == w.render_error and not w.osd_txt.isNull()


def test_a_rebuild_that_helps_resumes_rendering(gl_window, monkeypatch, caplog):
    import logging
    w, core = gl_window
    broken = [True]
    def frame():
        if broken[0]: raise RuntimeError("invalid program")
    def init():
        broken[0] = False; w.shader_program = 2
        return True
    monkeypatch.setattr(w, "_paint_frame", frame)
    monkeypatch.setattr(w, "_init_gl", init)
    with caplog.at_level(logging.INFO):
        for _ in range(100):
            w.paintGL()
    assert not w._gl_dead and w._gl_failures == 0 and w.render_error is None
    assert any("works again" in r.getMessage() for r in render_logs(caplog, logging.INFO))


def test_a_failed_rebuild_gives_up_at_once(gl_window, monkeypatch):
    w, core = gl_window
    def frame(): raise RuntimeError("context lost")
    monkeypatch.setattr(w, "_paint_frame", frame)
    monkeypatch.setattr(w, "_init_gl", lambda: False)
    for _ in range(30):
        w.paintGL()
    assert w._gl_dead and w.render_error


def test_intermittent_render_errors_are_reported_once_a_minute(gl_window, monkeypatch, caplog):
    import logging
    w, core = gl_window
    n = [0]
    def frame():
        n[0] += 1
        if n[0] % 2: raise RuntimeError("sometimes")
    monkeypatch.setattr(w, "_paint_frame", frame)
    with caplog.at_level(logging.INFO):
        for _ in range(400):
            w.paintGL()
    assert len(render_logs(caplog, logging.ERROR)) == 1 and not w._gl_dead
    assert len(caplog.records) == 1


# ---------------------------------------------------------------- P key

class Out:
    def __init__(self, tty): self.tty, self.text = tty, ""
    def isatty(self): return self.tty
    def write(self, s): self.text += s
    def flush(self): pass


def test_p_prints_the_password_only_to_a_terminal(make, monkeypatch):
    w, core = make("200,100,320,180", None)
    log = Out(False)                             # nova start: stdout is logs/ID.log
    monkeypatch.setattr(sys, "stdout", log)
    key(w, Qt.Key_P)
    assert "s3cret" not in log.text and "10.0.0.1:554/stream_0" in log.text
    term = Out(True)                             # nova run: a terminal
    monkeypatch.setattr(sys, "stdout", term)
    key(w, Qt.Key_P)
    assert term.text.strip() == core.get_url()


# ---------------------------------------------------------------- nova layout apply (control.py -> place)

def test_place_moves_and_zooms_a_running_window(make, app):
    w, core = make("100,100,640,360", "0.25,0.25,0.5,0.5")
    core.connected(1920, 1080)
    w.place("200,150,800,450", "0,0,0.5,0.5")
    app.processEvents()
    assert w.geometry() == QRect(200, 150, 800, 450)
    assert w.zoomer.getRect() == (0, 0, 960, 540)
    w._persist_if_changed()
    assert stored(core) == ("200,150,800,450", "0.0,0.0,0.5,0.5")
    w.place(None, "0,0,1,1")                     # zoom only: back to the whole picture
    assert not w.zoomer.is_zoom()


def test_place_before_the_first_frame_keeps_the_zoom_for_it(make, app):
    w, core = make("100,100,640,360")
    w.place("120,130,640,360", "0.5,0.5,0.5,0.5")
    core.connected(1920, 1080)
    assert w.zoomer.getRect() == (960, 540, 960, 540)


# ---------------------------------------------------------------- zoom is a region: resizing keeps it

def test_resizing_the_window_keeps_the_region_and_what_is_saved(make, app):
    w, core = make("100,100,400,900", "0.4,0.1,0.2,0.8")
    core.connected(1920, 1080)
    assert w.zoomer.getRect() == (768, 108, 384, 864)
    w._persist_if_changed()
    saved = stored(core)[1]
    w.resize(1600, 900); app.processEvents()
    assert w.zoomer.w_widget == 1600
    assert w.zoomer.getRect() == (768, 108, 384, 864)                 # the region stays
    assert w.zoomer.view_rect().width() == pytest.approx(1536)        # the sides show the picture
    assert w._pic_ratio() == pytest.approx(1600 / 900)                # no black bars
    w._persist_if_changed()
    assert stored(core)[1] == saved                                   # and nothing new is saved
    v = w.zoomer.view_rect()
    w.pan_region(0.1, 0)                                              # Shift+Right: a tenth of the view further right
    assert w.zoomer.x() == pytest.approx(v.x() + 0.1 * v.width())
    w.resize(400, 900); app.processEvents()
    assert w.zoomer.view_rect().width() == pytest.approx(1536)        # the pan made the wide view the region


def test_ctrl_drag_moves_the_region_not_the_window(make, app):
    w, core = make("100,100,400,900", "0.4,0.1,0.2,0.8")
    core.connected(1920, 1080)
    m = Mouse(w)
    g = w.geometry()
    v, s = w.zoomer.view_rect(), w.zoomer.scale()         # what is shown (the window need not have the region's shape)
    m.press(200, 450, mods=Qt.ControlModifier)
    m.move(150, 450, mods=Qt.ControlModifier)             # the picture follows the pointer 50 px to the left
    m.release(150, 450, mods=Qt.ControlModifier)
    assert w.geometry() == g                              # the window did not move
    moved = v.x() + 50 / s
    assert w.zoomer.x() == pytest.approx(moved) and w.zoomer.getRect()[2:] == (v.width(), v.height())
    m.press(200, 450); m.release(200, 450)                # a plain click: no pan
    assert w.zoomer.x() == pytest.approx(moved)


def test_wheel_direction_follows_the_hand_on_natural_scrolling_and_pinch_zooms_by_its_value(make):
    w, core = make("200,100,640,360", None)
    core.connected(1920, 1080)
    m = Mouse(w)
    for _ in range(5):
        w.zoomer.zoom(None, 1)
    start = w.zoomer.getRect()
    m.wheel(300, 170, -120)                                  # pulled toward you: out
    assert w.zoomer.width() > start[2]
    w.zoomer.setRect(*start)
    m.wheel(300, 170, -120, inverted=True)                   # macOS natural scrolling: fingers pushed away = in
    assert w.zoomer.width() < start[2]
    w.zoomer.setRect(*start)
    from types import SimpleNamespace
    w.mouse_hdlr.pinch(SimpleNamespace(value=lambda: 0.25, position=lambda: QPointF(300, 170)))
    assert w.zoomer.width() == pytest.approx(start[2] / 1.25)       # 25 % bigger picture = region 1/1.25
    w.mouse_hdlr.pinch(SimpleNamespace(value=lambda: -0.2, position=lambda: QPointF(300, 170)))
    assert w.zoomer.width() == pytest.approx(start[2] / 1.25 / 0.8)
