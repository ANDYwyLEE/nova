"""The camera window: frameless QOpenGLWindow with the picture (OpenGL or QPainter), the overlay and
status strip, geometry and zoom kept in QSettings, fullscreen / maximize / snap, and `place()` for
layouts and the control channel."""
import sys
import logging

from PySide6.QtCore import Qt, QTimer, Slot, QEvent, QSettings, QSize, QRect
from PySide6.QtGui import QGuiApplication, QImage, QPainter, QColor, QPen, QFont

try: from PySide6.QtOpenGL import QOpenGLWindow
except ImportError:
    from PySide6.QtGui import QOpenGLWindow

from zoomer import Zoomer
from mousehandler import MouseHdlr
from keyhandler import KeyHdlr
from helper import Helper
from fonts import MONO
from util import WSL, MAC, WINDOWS, mask_url, window_title, parse_geom, parse_zoom

GS = 20 # default grid size
MIN_VISIBLE = 40          # px of a window that must be on some screen for it to count as reachable
SCREEN_SETTLE_MS = 5000   # after a monitor change, wait this long for the window manager to finish moving windows

# OSD layers are painted premultiplied: QPainter composes them natively and the OpenGL path
# uploads them without a per-frame format conversion.
OSD_FORMAT = QImage.Format_RGBA8888_Premultiplied
# Drawing order of the OSD layers, bottom to top, in both renderers: the status text ("Connecting...",
# "Unreachable...") with its dimming under the help overlay and the control strip, the border on top.
OSD_LAYERS = ("txt", "hlp", "bt", "bg")

# Qt 6.6+: the window moved to a screen with another device pixel ratio
_DPR_CHANGE = getattr(QEvent, "DevicePixelRatioChange", None)

def _is_tty(stream):
    try:
        return stream is not None and stream.isatty()
    except Exception:
        return False

def _geom_str(g: QRect):
    return f"{g.x()},{g.y()},{g.width()},{g.height()}"

def _parse_geom(text):
    """QRect from 'x,y,w,h' (util.parse_geom: sizes positive, values within a window's range), or None."""
    g = parse_geom(text)
    return QRect(*g) if g else None

def snap_to_grid(x, grid=GS):
    if grid <= 0: return x
    r = x % grid
    return x - r if r < grid / 2 else x + (grid - r)


class Nova(QOpenGLWindow):
    def __init__(
        self,
        core,
        grid=20,
        border=False,
        geometry=None,
        zoom=None,
    ):
        super().__init__()


        self.core = core
        self.grid = max(1, int(grid))
        self.border = border
        self.url_hash = self.core.hash()
        self.frame_q = core.get_frame_q()
        self.is_running = True
        self.curr_df = None      # DisplayFrame currently shown
        self.curr_txt = None
        self.render_error = None # set when the window can no longer draw the video; stays on screen
        self._focused = self.isActive()     # the border colour follows it (orange focused, green not)
        app = QGuiApplication.instance()
        app.applicationStateChanged.connect(self._on_app_state)
        app.focusWindowChanged.connect(self._on_focus_window)

        self.osd_bg  = QImage()
        self.osd_bt  = QImage()
        self.osd_txt = QImage()
        self.osd_hlp = QImage()

        self._fullscrn_flag = False

        self._m_flag = False # Mouse in button area
        self._m_sz = 15 # Icon size
        self._m_x = 0 # left-top x-coordinate
        self._m_y = 0 # left-top y-coordinate
        self._m_area = (0,0,0,0)
        self._m_strm = self.core.streamer.target_url is not None

        self.key_hdlr = KeyHdlr(self)
        km = self.key_hdlr.get_modifier()
        self.mouse_hdlr = MouseHdlr(self, km)
        self.zoomer = Zoomer(320, 180, 320, 180)
        self.helper = Helper()

        self.setMinimumSize(QSize(96, 54))
        self.setFlags(self.flags() | Qt.FramelessWindowHint)
        #self.setFlags(self.flags() | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)

        # The window's place on record. While a monitor is missing (unplugged, or a DisplayPort monitor
        # asleep) the window manager moves windows around; then the place the user chose stays on record
        # (_held_place) until its monitor is back or the user moves the window.
        self._geom_hold = False
        self._held_place = None
        self.qset = QSettings()
        gk = f"geom_{self.url_hash}"
        g = geometry or self.qset.value(gk)        # --geometry (a layout) wins over the remembered place
        pg = parse_geom(g) if g else None
        if pg:
            try:
                x, y, w, h = pg
                fit = self._fit_on_screen(x, y, w, h)
                self._geom(*fit)
                if fit != (x, y, w, h):
                    logging.info(f"[Nova] remembered place {x},{y},{w},{h} is on no screen; "
                                 f"opened at {','.join(map(str, fit))} until its monitor is back")
                    self._geom_hold = True
                    self._held_place = f"{x},{y},{w},{h}"
            except Exception:
                self.resize(320, 180)
        else:
            if g:
                logging.warning(f"[Nova] ⚠️ ignoring geometry {str(g)[:60]!r}: not x,y,w,h with a positive size")
            self.resize(320, 180)
        # Geometry of the window when neither fullscreen nor maximized: what is persisted in those states.
        self._normal_geom = self.geometry()

        # The zoom is kept as ratios of the picture until the decoder reports the frame size (vid_sz);
        # from then on the zoomer holds it in frame pixels and keeps it across reconnects.
        self.vid_sz = None
        zk = f"zoom_{self.url_hash}"
        z = zoom or self.qset.value(zk)
        self.z_ratio = parse_zoom(z) if z else None
        if z and self.z_ratio is None:
            logging.warning(f"[Nova] ⚠️ ignoring zoom {str(z)[:60]!r}: not rx,ry,rw,rh inside the picture")
        self.z_ratio = self.z_ratio or (0.0, 0.0, 1.0, 1.0)

        # Persist geometry/zoom within 2 s of a change, not only at exit: a crash loses nothing and
        # `nova layout save` sees the windows where they are now.
        self._persisted = None
        self._persist_timer = QTimer(self)
        self._persist_timer.setInterval(2000)
        self._persist_timer.timeout.connect(self._persist_if_changed)
        self._persist_timer.start()

        self._screen_timer = QTimer(self)
        self._screen_timer.setSingleShot(True)
        self._screen_timer.setInterval(SCREEN_SETTLE_MS)
        self._screen_timer.timeout.connect(self._screens_settled)
        app.screenAdded.connect(self._on_screen_added)
        app.screenRemoved.connect(self._on_screens_changed)
        app.primaryScreenChanged.connect(self._on_screens_changed)
        for scr in app.screens():
            scr.geometryChanged.connect(self._on_screens_changed)

        self.core.img_sig.connect(self.update_img)
        self.core.stat_sig.connect(self.update_stat)
        self.core.sz_sig.connect(self.set_vid_sz)
        self.core.init_done_sig.connect(self.set_bt)
        self.core.bt_sig.connect(self.set_bt)
        self.helper.hlp_sig.connect(self.set_hlp)
        self.core.exit_sig.connect(lambda: QTimer.singleShot(1000, self.close))

        # The surface format must be fixed before the native window is created by show().
        fmt = self._surface_format()
        if fmt is not None:
            self.setFormat(fmt)
        # Before show(): Wayland compositors evaluate their window rules against the title at map time.
        self.setTitle(window_title(self.core.display_name))
        self.show()


    def _surface_format(self):
        """Subclasses return a QSurfaceFormat to apply before show(); None keeps the app default."""
        return None

    def initializeGL(self): pass
    def resizeGL(self, w: int, h: int): pass


    @Slot(int, int)
    def set_vid_sz(self, w, h):
        """The decoder reports the frame size with the first frame of every connection, so after each
        reconnect too. The same size keeps the zoom as it is; a new size (the camera's stream settings were
        changed) keeps the zoomed region as fractions of the picture; the first size applies the remembered
        (or --zoom) region."""
        if not self.is_running or w <= 0 or h <= 0: return
        if self.vid_sz == (w, h):
            self.set_bt(); self.update()
            return
        if self.vid_sz is not None:
            self.z_ratio = self._zoom_ratio()
        self.vid_sz = (w, h)
        ww, wh = self._widget_wh()
        self.zoomer = Zoomer(w, h, ww, wh)
        rx, ry, rw, rh = self.z_ratio
        if (rx, ry, rw, rh) != (0.0, 0.0, 1.0, 1.0):
            # round(), not int(): the ratios were written from whole pixels, int() lost one now and then
            self.zoomer.set_rect(QRect(round(rx * w), round(ry * h), round(rw * w), round(rh * h)))
        self.setMinimumHeight(max(1, int(self.minimumWidth() / w * h)))
        self.set_bt()
        self.update()

    def _zoom_ratio(self):
        """The zoomed region as (rx, ry, rw, rh) fractions of the picture. Computed from the zoomer's own
        frame size: the decoder's is 0 while it reconnects."""
        if self.vid_sz is None:
            return self.z_ratio
        vw, vh = self.vid_sz
        zx, zy, zw, zh = self.zoomer.getRect()
        return (zx / vw, zy / vh, zw / vw, zh / vh)

    def _vid_wh(self):
        """Frame size: known since the first frame and kept while the decoder reconnects."""
        return self.vid_sz or (self.core.width(), self.core.height())

    def _pic_ratio(self):
        """Aspect ratio of what is shown: the zoomed region, else the whole frame."""
        if self.zoomer.is_zoom() or self.vid_sz is not None:
            return self.zoomer.ratio()
        return self.core.ratio()

    @Slot(str)
    def update_stat(self, text):
        if not self.is_running or self.render_error: return
        self.curr_txt = text
        self.set_txt(text)
        self.update()

    @Slot()
    def update_img(self):
        if not self.is_running: return
        try:
            df = self.frame_q.pop()
        except IndexError: return
        if df is None: return
        self.curr_df = df                   # kept for snapshots also when it cannot be shown
        if self.render_error: return
        if not self.osd_txt.isNull():
            self.curr_txt = None
            self.osd_txt = QImage()
        self.update()

    def show_render_error(self, text):
        """The renderer gave up: show text instead of the video until the window is closed."""
        self.render_error = text
        self.curr_txt = text
        self.set_txt(text)
        self.update()


    def paintGL(self):
        if not self.is_running: return
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(0, 0, self.width(), self.height(), QColor(0, 0, 0, 204))
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        self._render(p)
        p.end()

    def _render(self, p: QPainter):
        if not self.is_running: return
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        ww, wh = self._widget_wh()
        df = self.curr_df
        if df is not None:
            img = df.to_qimage()
            if img is not None and not img.isNull():
                src = self.zoomer.view_rect().toRect() if self.zoomer.is_zoom() else img.rect()
                r = self._pic_ratio()
                if wh < ww / r: h = wh; w = round(h * r)
                else: w = ww; h = round(w / r)
                x, y = (ww - w) // 2, (wh - h) // 2
                # scaling is done by the GL paint engine; no per-frame CPU copy/scaled()
                p.drawImage(QRect(x, y, w, h), img, src)

        self._draw_osd(p)

    def _draw_osd(self, p: QPainter):
        for k in OSD_LAYERS:
            img = getattr(self, f"osd_{k}")
            if not img.isNull(): p.drawImage(0, 0, img)

    def _osd_image(self):
        """A transparent OSD layer covering the window, in device pixels so text and icons stay sharp on
        HiDPI screens; QPainter draws on it in window coordinates."""
        dpr = self.devicePixelRatio()
        w, h = self._widget_wh()
        img = QImage(max(1, round(w * dpr)), max(1, round(h * dpr)), OSD_FORMAT)
        img.setDevicePixelRatio(dpr)
        img.fill(Qt.transparent)
        return img

    def _border_color(self):
        if not self.border: return "none"
        return "orange" if self._focused else "green"


    def set_bg(self, border="no-change"):
        if not self.is_running: return
        if border == "none":
            if not self.osd_bg.isNull():
                self.osd_bg = QImage()
            self.update()
            return
        elif border == "green": c = QColor(0, 120, 0)    #Qt.green
        elif border == "orange": c = QColor(200, 100, 0) #QColor("orange")
        else: return
        if self.is_fullscreen(): return

        w, h = self._widget_wh()
        img = self._osd_image()
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(c, 1))
        p.drawRect(0, 0, w - 1, h - 1)
        p.end()
        self.osd_bg = img
        self.update()

    def set_txt(self, text="", font=MONO, size=14, align=Qt.AlignCenter):
        if not self.is_running: return
        w, h = self._widget_wh()
        img = self._osd_image()
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        bg_color = QColor(0, 0, 0, 160)
        p.fillRect(0, 0, w, h, bg_color)
        f = QFont(font)
        f.setPointSize(size)
        p.setFont(f)
        p.setPen(QColor(255, 255, 255))
        p.drawText(QRect(0, 0, w, h), align, text)
        p.end()
        self.osd_txt = img

    def set_hlp(self, text=None):
        if not self.is_running: return
        if not text:
            # Helper.off() emits None through a Signal(str), which arrives here as "": treat both as
            # "help off", otherwise the dimming layer is rebuilt without text and the window stays dark.
            self.osd_hlp = QImage()
            self.update()
            return
        w, h = self._widget_wh()
        dim = min(w, h)
        _min, _max, _base = 6, 12, 80
        if MAC: _min, _max, _base = 5, 14, 50
        if WINDOWS: _min, _max, _base = 5, 14, 70
        fsize = min(_max, max(_min, int(dim / _base)))
        img = self._osd_image()
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.fillRect(0, 0, w, h, QColor(0, 0, 0, 160))
        font = QFont(MONO)
        font.setPointSize(fsize)
        p.setFont(font)
        p.setPen(QColor(255, 255, 255))
        max_w = int(w * 0.8)
        flags = Qt.AlignLeft | Qt.TextWordWrap
        measure = QRect(0, 0, max_w, 10000)
        br = p.boundingRect(measure, flags, text)
        x = int((w - br.width()) / 2)
        y = int((h - br.height()) / 2)
        if y < 0: y = 0
        draw_rect = QRect(x, y, br.width(), br.height())
        p.drawText(draw_rect, flags, text)
        p.end()
        self.osd_hlp = img


    def set_bt(self):
        if not self.is_running: return
        ww, wh = self._widget_wh()
        r = self._pic_ratio()
        if wh < ww / r: h = wh; w = round(h * r)
        else: w = ww; h = round(w / r)
        x, y = (ww - w) // 2, (wh - h) // 2

        sz = min(48, int(min(w, h)/600 * 48))# icon size
        ew = w // 100 + 4 # edge of width
        eh = h // 20 + 4 # edge of height
        gap = 4 # gap between icons
        n = 5 # number of icons (strm, rec, aud, _, help)
        tw = ew + sz + ew # total width
        th = eh + n * sz + (n-1) * gap + eh # total height
        b = 6 # BORDER size of ZONE_EDGE for resizing
        self._m_area = (x+b, y+b, x+b+tw, y+b+th) # (x1,y1, x2,y2)

        _x = x + b + ew # x-coor of first icon
        _y = y + b + eh # y-coor of first icon

        self._m_x = _x
        self._m_y = _y
        self._m_sz = sz

        img = self._osd_image()
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        if self._m_flag:
            # This gives that "grayish tone" UI feel
            x1, y1, x2, y2 = self._m_area
            p.setBrush(QColor(30, 30, 30, 100)) # Dark gray with transparency
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(x1, y1, x2-x1, y2-y1, 10, 10)

        def icon_at(icon, idx):
            _y1 = _y + (sz + gap) * idx
            p.drawImage(QRect(_x, _y1, sz, sz), icon)

        if self.core.is_streamer_on() or (self._m_flag and self._m_strm):
            icon_at(self.core.get_streamer_icon(), 0)
        if self.core.is_recorder_on() or self._m_flag:
            icon_at(self.core.get_recorder_icon(), 1)
        if self.core.is_audio_on() or self._m_flag:
            icon_at(self.core.get_audio_icon(), 2)
        if self.helper.is_on() or self._m_flag:
            icon_at(self.helper.get_icon(), 4)
        p.end()
        self.osd_bt = img


    def in_bt(self, pos, idx):
        if idx == 0 and not self._m_strm: return False
        ex, ey = pos.x(), pos.y()
        x = self._m_x
        # Calculate y for the idx-th button
        sz = self._m_sz
        y = self._m_y + (sz + 4) * idx
        return (x <= ex <= x + sz) and  (y <= ey <= y + sz)

    def in_bt_zone(self, pos):
        x, y = pos.x(), pos.y()
        x1, y1, x2, y2 = self._m_area
        flag = (x1 <= x <= x2) and (y1 <= y <= y2)
        # Trigger update only when state changes
        if self._m_flag != flag:
            self._m_flag = flag
            self.set_bt()
            self.update()
        return flag

    def pan_region(self, fx, fy):
        """Shift+Arrow: move the zoomed region by a fraction of what is shown (+fx: see more to the right)."""
        if not self.zoomer.is_zoom(): return
        ww, wh = self._widget_wh()
        self.zoomer.pan(-fx * ww, -fy * wh)
        self.set_bt()
        self.update()

    def reset_zoom(self):
        self.z_ratio = (0.0, 0.0, 1.0, 1.0)     # also before the first frame, while the zoomer is a placeholder
        self.zoomer.reset()
        self.set_bt()
        self.update()

    def place(self, geometry=None, zoom=None):
        """Put the window where a layout says, while it runs (`nova layout apply`, via control.py):
        geometry 'x,y,w,h' and zoom 'rx,ry,rw,rh', each optional. Counts as the user's own move."""
        self._placed_by_user()
        if self.is_fullscreen(): self.show_normal()
        if self._isMaximized(): self.showNormal()
        g = _parse_geom(geometry or "")
        if g is not None:
            self._geom(*self._fit_on_screen(g.x(), g.y(), g.width(), g.height()))
        if zoom:
            zr = parse_zoom(zoom)
            if zr is None:
                raise ValueError(f"zoom {str(zoom)[:60]!r} is not rx,ry,rw,rh inside the picture")
            rx, ry, rw, rh = zr
            self.z_ratio = zr
            if self.vid_sz is not None:              # else set_vid_sz applies it with the first frame
                w, h = self.vid_sz
                self.zoomer.reset()
                if self.z_ratio != (0.0, 0.0, 1.0, 1.0):
                    self.zoomer.set_rect(QRect(round(rx * w), round(ry * h), round(rw * w), round(rh * h)))
        self.set_bt()
        self.update()

    def toggle_streamer(self):
        self.core.toggle_streamer()
        self.set_bt()
        self.update()

    def toggle_recorder(self):
        self.core.toggle_recorder()
        self.set_bt()
        self.update()

    def toggle_audio(self):
        self.core.toggle_audio()
        self.set_bt()
        self.update()

    def toggle_help(self):
        self.helper.toggle_help()
        self.set_bt()
        self.update()

    def snapshot(self):
        self.core.snapshot(self.curr_df)

    def print_info(self):
        """P: print the camera URL. With the password only to a terminal: under the launcher stdout is
        logs/ID.log, where it would sit in plain text (log records are masked, print() is not)."""
        url = self.core.get_url()
        print(url if _is_tty(sys.stdout) else mask_url(url), flush=True)

    def keyPressEvent(self, e): self.key_hdlr.press(e)
    def keyReleaseEvent(self, e): self.key_hdlr.release(e)

    def mousePressEvent(self, e): self.mouse_hdlr.press(e)
    def mouseReleaseEvent(self, e): self.mouse_hdlr.release(e)
    def mouseMoveEvent(self, e): self.mouse_hdlr.move(e)
    def mouseDoubleClickEvent(self, e): self.mouse_hdlr.dblclk(e)
    def wheelEvent(self, e):  self.mouse_hdlr.wheel(e); e.accept()


    def resizeEvent(self, e):
        if not self.is_running: return
        if self._is_normal_state():
            self._normal_geom = self.geometry()
        x, y = e.size().width(), e.size().height()
        self.zoomer.resize(x, y)
        self._rebuild_osd()

    def _rebuild_osd(self):
        """Repaint the OSD layers for the window's size and device pixel ratio."""
        txt = self.curr_txt
        if txt is not None: self.set_txt(txt)
        self.set_bt()
        if self.helper.is_on(): self.helper._on() # refresh
        if not self.is_fullscreen():
            self.set_bg(self._border_color())
        self.update()


    def event(self, e):
        if e.type() == QEvent.WindowDeactivate:
            self._set_focus(False)
        elif e.type() == QEvent.WindowActivate:
            self._set_focus(True)
        elif e.type() == _DPR_CHANGE and self.is_running:
            self._rebuild_osd()          # moved to a screen with another scale factor
        elif e.type() == QEvent.NativeGesture and e.gestureType() == Qt.ZoomNativeGesture:
            self.mouse_hdlr.pinch(e)     # macOS trackpad
            return True
        return super().event(e)

    def focusInEvent(self, e):
        self._set_focus(True)
        super().focusInEvent(e)

    def focusOutEvent(self, e):
        self._set_focus(False)
        super().focusOutEvent(e)

    def _on_app_state(self, state):
        self._set_focus(state == Qt.ApplicationActive)
    def _on_focus_window(self, win):
        self._set_focus(win is self)

    def _set_focus(self, on):
        if not self.is_running: return
        if self._focused == on: return
        self._focused = on
        self.set_bg(self._border_color())

    def closeEvent(self, e):
        super().closeEvent(e)
        self.cleanup()

    def cleanup(self):
        """Persist geometry/zoom. Safe to call twice (closeEvent and QGuiApplication.aboutToQuit)."""
        if self.is_running:
            self.is_running = False
            if self.helper.is_on(): self.helper.off()
            #self.core.stop() # check nova.py
            self._persist_timer.stop()
            self._screen_timer.stop()
            self._persist(force=True)

    def _state_strings(self):
        """('x,y,w,h', 'rx,ry,rw,rh') to keep on record. The geometry is the normal one also while the
        window is fullscreen or maximized, and the place the user chose while a monitor change holds it.
        Until the first frame the zoom is the remembered (or --zoom) one, never a guess: the decoder's
        frame size is 0 then, and again during every reconnect, which used to write 0,0,1,1 over the zoom."""
        if self._geom_hold and self._held_place:
            geom = self._held_place
        else:
            geom = _geom_str(self._normal_geometry())
        rx, ry, rw, rh = self._zoom_ratio()
        return geom, f'{rx},{ry},{rw},{rh}'

    def _persist(self, force=False):
        """Write geometry/zoom to QSettings when they changed since the last write (always with force)."""
        state = self._state_strings()
        if state == self._persisted and not force:
            return
        geom, zoom = state
        self.qset.setValue(f"geom_{self.url_hash}", geom)
        self.qset.setValue(f"zoom_{self.url_hash}", zoom)
        self.qset.sync()
        self._persisted = state

    def _persist_if_changed(self):
        if self.is_running:
            self._persist()

    def _is_normal_state(self):
        return not self.is_fullscreen() and not self._isMaximized()

    def _normal_geometry(self) -> QRect:
        """Geometry outside fullscreen / maximized: exiting in those states (nova stop, Ctrl-C, closing the
        window) used to write the screen-sized rectangle over the window's place."""
        if self._is_normal_state():
            return self.geometry()
        return QRect(self._normal_geom)

    def moveEvent(self, e):
        if self._is_normal_state():
            self._normal_geom = self.geometry()
        super().moveEvent(e)

    # ------------------------------------------------------------------ screens
    def _screen_rects(self):
        return [s.geometry() for s in QGuiApplication.screens()]

    def _primary_rect(self) -> QRect:
        return QGuiApplication.primaryScreen().availableGeometry()

    def _on_screen(self, r: QRect):
        """True when enough of r is on one screen to be seen and dragged."""
        need_w, need_h = min(r.width(), MIN_VISIBLE), min(r.height(), MIN_VISIBLE)
        for s in self._screen_rects():
            i = r.intersected(s)
            if i.width() >= need_w and i.height() >= need_h:
                return True
        return False

    def _fit_on_screen(self, x, y, w, h):
        """(x, y, w, h) unchanged when the window would be visible, else moved onto the primary screen
        (shrunk with its aspect ratio kept if it is larger). A remembered place on a monitor that is gone
        used to open the window where nobody can see or reach it."""
        if self._on_screen(QRect(x, y, w, h)):
            return x, y, w, h
        p = self._primary_rect()
        f = min(1.0, p.width() / w if w > 0 else 1.0, p.height() / h if h > 0 else 1.0)
        w, h = max(1, int(w * f)), max(1, int(h * f))
        x = max(p.left(), min(x, p.left() + p.width() - w))
        y = max(p.top(), min(y, p.top() + p.height() - h))
        return x, y, w, h

    def _on_screen_added(self, screen):
        screen.geometryChanged.connect(self._on_screens_changed)
        self._on_screens_changed()

    def _on_screens_changed(self, *_):
        """A monitor was added, removed or changed (also a DisplayPort monitor that drops out while it
        sleeps). The window manager may now move windows; keep the place the user chose on record."""
        if not self.is_running: return
        if not self._geom_hold:
            self._geom_hold = True
            self._held_place = _geom_str(self._normal_geometry())
            logging.debug(f"[Nova] screens changed: keeping the place {self._held_place} on record")
        self._screen_timer.start()          # settle SCREEN_SETTLE_MS after the last change

    def _screens_settled(self):
        """The monitors have been quiet for a while. If the remembered place is on a screen again, put the
        window back there (the window manager moved it away) and end the hold. If its monitor is still
        gone, keep the place on record and only make sure the window itself can be reached."""
        if not self.is_running or not self._geom_hold: return
        place = _parse_geom(self._held_place)
        normal = self._is_normal_state()
        if place is None or self._on_screen(place):
            if place is not None and normal and self.geometry() != place:
                logging.info(f"[Nova] screens changed: back to {self._held_place}")
                self.setGeometry(place)
            self._geom_hold = False
            self._held_place = None
        elif normal and not self._on_screen(self.geometry()):
            g = self.geometry()
            self.setGeometry(*self._fit_on_screen(g.x(), g.y(), g.width(), g.height()))

    def _placed_by_user(self):
        """The user moves or resizes the window: from now on its place is where it is."""
        self._geom_hold = False
        self._held_place = None



    def _screen_wh(self):
        s = QGuiApplication.screenAt(self.position()) or QGuiApplication.primaryScreen()
        g = s.geometry()
        return g.width(), g.height()

    def _virtual_rect(self) -> QRect:
        """Bounding rectangle of all monitors. Its origin is not (0,0) when a monitor sits
        left of / above the primary one."""
        return QGuiApplication.primaryScreen().virtualGeometry()

    def _widget_wh(self):
        return self.width(), self.height()

    def _curr_pos(self):
        g = self.geometry()   # global geometry
        return g.x(), g.y()

    def _isFullScreen(self):
        return bool(self.windowState() & Qt.WindowFullScreen)
    def _isMaximized(self):
        return bool(self.windowState() & Qt.WindowMaximized)
    def is_fullscreen(self):
        return self._isFullScreen() or self._fullscrn_flag


    def fullscreen(self):
        if self.is_fullscreen(): return
        if not self._isMaximized():
            self._normal_geom = self.geometry()
        self._fullscrn_flag = True
        self.set_bg("none")
        self.showFullScreen()

    def show_normal(self):
        if not self.is_fullscreen(): return
        if self._isFullScreen(): self.showNormal()
        self._fullscrn_flag = False
        self.set_bg(self._border_color())

    def max_screen(self):
        vw, vh = self._vid_wh()
        if vw <= 0 or vh <= 0: return
        self._placed_by_user()
        if self.is_fullscreen(): self.show_normal()

        screen = self.screen()
        top_left = screen.geometry().topLeft()
        x = top_left.x()
        y = top_left.y()

        sw, sh = self._screen_wh()
        vid_ratio = vw / vh
        sc_ratio = sw / sh
        if sc_ratio > vid_ratio: nh = sh; nw = int(nh * vid_ratio)
        else: nw = sw; nh = int(nw / vid_ratio)
        self._geom(x + (sw - nw) // 2, y + (sh - nh) // 2, nw, nh)

    def toggle_fullscreen(self):
        if self.is_fullscreen(): self.show_normal()
        else: self.fullscreen()

    def cut_widget(self):
        vw, vh = self._vid_wh()
        if vw <= 0 or vh <= 0: return
        self._placed_by_user()
        if self.is_fullscreen(): self.max_screen(); return

        r = self.zoomer.roi_ratio()
        ow, oh = self._widget_wh()
        if oh < ow / r: nw, nh = round(oh * r), oh
        else: nw, nh = ow, round(ow / r)
        x, y = self._curr_pos()
        nx, ny = x + (ow - nw) // 2, y + (oh - nh) // 2
        self._geom(nx, ny, nw, nh)

    def center_orig(self):
        vw, vh = self._vid_wh()
        if vw <= 0 or vh <= 0: return
        self._placed_by_user()
        if self.is_fullscreen(): self.show_normal()

        screen = self.screen()
        top_left = screen.geometry().topLeft()
        x, y = top_left.x(), top_left.y()

        sw, sh = self._screen_wh()
        if vw > sw or vh > sh: self.max_screen(); return
        self._geom(x + (sw - vw) // 2, y + (sh - vh) // 2, vw, vh)

    def align_grid(self):
        if self.is_fullscreen(): return
        if WSL: return
        G = self.grid
        vg = self._virtual_rect()
        ox, oy = vg.left(), vg.top()                 # grid origin = top-left of the virtual desktop
        sw, sh = vg.left() + vg.width(), vg.top() + vg.height()
        x, y = self._curr_pos()
        w, h = self._widget_wh()
        r = w / h if h > 0 else 1
        nh = snap_to_grid(h, G)
        nw = round(nh * r)
        snap_x = lambda v: snap_to_grid(v - ox, G) + ox
        snap_y = lambda v: snap_to_grid(v - oy, G) + oy
        if x + nw >= sw and y + nh  >= sh:
            pass
        elif x + nw  >= sw:
            y = snap_y(y)
        elif y + nh >= sh:
            x = snap_x(x)
        else:
            x = snap_x(x)
            y = snap_y(y)
        nx, ny = min(x, sw - nw), min(y, sh - nh)
        self._geom(nx, ny, nw, nh)

    def move_up(self):   self._move_d(0,-self.grid)
    def move_down(self): self._move_d(0, self.grid)
    def move_left(self): self._move_d(-self.grid, 0)
    def move_right(self):self._move_d( self.grid, 0)

    def expand_right(self): self._geom_d(  0, 0, self.grid, 0)
    def shrink_right(self): self._geom_d(  0, 0,-self.grid, 0)
    def expand_left(self):  self._geom_d(-self.grid, 0,+self.grid, 0)
    def shrink_left(self):  self._geom_d(+self.grid, 0,-self.grid, 0)

    def expand_down(self):  self._geom_d(0,  0, 0, +self.grid)
    def shrink_down(self):  self._geom_d(0,  0, 0, -self.grid)
    def expand_up(self):    self._geom_d(0,-self.grid, 0, +self.grid)
    def shrink_up(self):    self._geom_d(0,+self.grid, 0, -self.grid)

    def expand_right_bottom(self): self._resize_r(1.1)   # 11/10
    def shrink_right_bottom(self): self._resize_r(0.909) # 10/11

    def expand(self): self._resize_rc(1.1)
    def shrink(self): self._resize_rc(0.909)

    def _geom(self, x, y, w, h):
        vg = self._virtual_rect()
        mw, mh = self.minimumWidth(), self.minimumHeight()
        w, h = min(vg.width(), max(mw, w)), min(vg.height(), max(mh, h))
        x, y = max(vg.left(), x), max(vg.top(), y)
        self.setGeometry(int(x), int(y), int(w), int(h))

    def _geom_d(self, dx, dy, dw, dh): # +delta
        self._placed_by_user()
        x, y = self._curr_pos()
        w, h = self.width(), self.height()
        self._geom(x + dx, y + dy, w + dw, h + dh)

    def _move(self, x, y):
        self._placed_by_user()
        vg = self._virtual_rect()
        w, h = self._widget_wh()
        x = max(vg.left(), min(x, vg.left() + vg.width() - w))
        y = max(vg.top(), min(y, vg.top() + vg.height() - h))
        self.setPosition(int(x), int(y))

    def _move_d(self, dx, dy):
        x, y = self._curr_pos()
        self._move(x + dx, y + dy)

    def _resize(self, w, h):
        self._placed_by_user()
        self.resize(int(w), int(h))

    def _resize_r(self, f): # ratio
        ow, oh = self._widget_wh()
        w, h = int(ow * f), int(oh * f)
        self._resize(w, h)

    def _resize_rc(self, f): # ratio & center
        ow, oh = self._widget_wh()
        w, h = round(ow * f), round(oh * f)
        dw, dh = w - ow, h - oh
        dx, dy = int(-dw / 2), int(-dh / 2)
        self._geom_d(dx, dy, dw, dh)
