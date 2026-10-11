import re
import traceback
import queue
import logging

from PySide6.QtCore import Qt, QTimer, Signal, Slot, QEvent, QSettings, QSize, QRect
from PySide6.QtGui import QGuiApplication, QImage, QPainter, QColor, QPen, QSurfaceFormat, QRegion

try: from PySide6.QtOpenGL import QOpenGLWindow
except ImportError:
    from PySide6.QtGui import QOpenGLWindow

from zoomer import Zoomer
from mousehandler import MouseHdlr
from keyhandler import KeyHdlr
from helper import Helper
from fonts import MONO
from util import WSL, MAC, WINDOWS

GS = 20 # grid size short variable

def snap_to_grid(x, grid=GS):
    #return round(x / grid) * grid
    if grid <= 0: return x
    r = x % grid
    return x - r if r < grid / 2 else x + (grid - r)


class Nova(QOpenGLWindow):
    def __init__(
        self,
        core,
        grid=20,
        border=False,
    ):
        super().__init__()


        self.core = core
        self.grid = GS = grid
        self.border = border
        self.show_help = False
        self.url_hash = self.core.hash()
        self.frame_q = core.get_frame_q()
        self.is_running = True
        self.curr_qimg = None
        self.curr_txt = None
        self._focused = True
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

        self.setMinimumSize(QSize(320/GS*6, 180/GS*6))
        self.setFlags(self.flags() | Qt.FramelessWindowHint)
        #self.setFlags(self.flags() | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)

        self.qset = QSettings()
        gk = f"geom_{self.url_hash}"
        g = self.qset.value(gk)
        if g:
            try:
                x, y, w, h = map(int, g.split(","))
                self._geom(x, y, w, h)
            except:
                self.resize(320, 180)
        else: self.resize(320, 180)

        self.pending_zoom_coord = False
        zk = f"zoom_{self.url_hash}"
        z = self.qset.value(zk)
        self.z_ratio = (0,0,1,1)
        if z:
            #  x, y, w, h = map(float, z.split(","))
            #self.z = QRect(x, y, w, h)
            rx, ry, rw, rh = map(float, z.split(",")) #*
            #rx = 0; ry = 0; rw = 1; rh = 1
            self.z_ratio = (rx, ry, rw, rh)           #*
            self.pending_zoom_coord = True

        self.core.img_sig.connect(self.update_img)
        self.core.stat_sig.connect(self.update_stat)
        self.core.sz_sig.connect(self.set_vid_sz)
        self.core.init_done_sig.connect(self.set_bt)
        self.core.bt_sig.connect(self.set_bt)
        self.helper.hlp_sig.connect(self.set_hlp)
        self.core.exit_sig.connect(lambda: QTimer.singleShot(1000, self.close))

        self.show()



    def initializeGL(self): pass
    def resizeGL(self, w: int, h: int): pass


    @Slot(int, int)
    def set_vid_sz(self, w, h):
        if not self.is_running: return
        ww, wh = self._widget_wh()
        self.zoomer = Zoomer(w, h, ww, wh)
        if self.pending_zoom_coord:
            self.pending_zoom_coord = False
            #self.zoomer.set_rect(self.z)
            rx, ry, rw, rh = self.z_ratio #*
            #print(f"rx:{rx}, ry:{ry}, rw:{rw}, rh:{rh}")
            real_z = QRect(int(rx * w), int(ry * h), int(rw * w), int(rh * h))
            self.zoomer.set_rect(real_z)
        self.setMinimumHeight(self.minimumWidth() / w * h)
        self.set_bt()

    @Slot(str)
    def update_stat(self, text):
        if not self.is_running: return
        self.curr_txt = text
        self.set_txt(text)
        self.update()

    @Slot()
    def update_img(self):
        if not self.is_running: return
        qimg = None
        try:
            qimg = self.frame_q.pop()
        except IndexError: return
        if qimg is None: return
        self.curr_qimg = qimg
        if not self.osd_txt.isNull():
            self.curr_txt = None
            self.osd_txt = QImage()
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
        if self.curr_qimg is not None:
            flag, img = self.curr_qimg
            if flag != "rgba":
                logging.error(f"[Nova: wrong image format!!")
                return
            if not img.isNull():
                ww, wh = self.width(), self.height()
                if self.zoomer.is_zoom(): img = img.copy(self.zoomer.toRect())
                r = self.zoomer.ratio() if self.zoomer.is_zoom() else self.core.ratio()
                if wh < ww / r: h = wh; w = round(h * r)
                else: w = ww; h = round(w / r)
                x, y = (ww - w) // 2, (wh - h) // 2
                img = img.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                p.drawImage(QRect(x, y, w, h), img)

        if not self.osd_txt.isNull(): p.drawImage(0, 0, self.osd_txt)
        if not self.osd_bt.isNull(): p.drawImage(0, 0, self.osd_bt)
        if not self.osd_hlp.isNull(): p.drawImage(0, 0, self.osd_hlp)
        if not self.osd_bg.isNull(): p.drawImage(0, 0, self.osd_bg)


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
        img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
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
        img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        bg_color = QColor(0, 0, 0, 160)
        p.fillRect(0, 0, w, h, bg_color)
        font.setPointSize(size)
        p.setFont(font)
        p.setPen(QColor(255, 255, 255))
        p.drawText(img.rect(), align, text)
        p.end()
        self.osd_txt = img

    def set_hlp(self, text=None):
        if not self.is_running: return
        if text is None:
            self.osd_hlp = QImage()
            return
        w, h = self._widget_wh()
        dim = min(w, h)
        _min, _max, _base = 6, 12, 80
        if MAC: _min, _max, _base = 5, 14, 50
        if WINDOWS: _min, _max, _base = 5, 14, 70
        fsize = min(_max, max(_min, int(dim / _base)))
        img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.fillRect(0, 0, w, h, QColor(0, 0, 0, 160))
        font = MONO
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
        r = self.zoomer.ratio() if self.zoomer.is_zoom() else self.core.ratio()
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

        img = QImage(ww, wh, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing, True)
        if self._m_flag:
            # This gives that "grayish tone" UI feel
            x1, y1, x2, y2 = self._m_area
            p.setBrush(QColor(30, 30, 30, 100)) # Dark gray with transparency
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(x1, y1, x2-x1, y2-y1, 10, 10)

        if self.core.is_streamer_on() or (self._m_flag and self._m_strm):
            icon = self.core.get_streamer_icon()
            icon = icon.scaled(sz, sz, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            _y1 = _y + (sz + gap) * 0
            p.drawImage(_x, _y1, icon)
        if self.core.is_recorder_on() or self._m_flag:
            icon = self.core.get_recorder_icon()
            icon = icon.scaled(sz, sz, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            _y1 = _y + (sz + gap) * 1
            p.drawImage(_x, _y1, icon)
        if self.core.is_audio_on() or self._m_flag:
            icon = self.core.get_audio_icon()
            icon = icon.scaled(sz, sz, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            _y1 = _y + (sz + gap) * 2
            p.drawImage(_x, _y1, icon)
        if self.helper.is_on() or self._m_flag:
            icon = self.helper.get_icon()
            icon = icon.scaled(sz, sz, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            _y1 = _y + (sz + gap) * 4
            p.drawImage(_x, _y1, icon)
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
        self.core.snapshot(self.curr_qimg)

    def print_info(self):
        print(self.core.get_url())

    def keyPressEvent(self, e): self.key_hdlr.press(e)
    def keyReleaseEvent(self, e): self.key_hdlr.release(e)

    def mousePressEvent(self, e): self.mouse_hdlr.press(e)
    def mouseReleaseEvent(self, e): self.mouse_hdlr.release(e)
    def mouseMoveEvent(self, e): self.mouse_hdlr.move(e)
    def mouseDoubleClickEvent(self, e): self.mouse_hdlr.dblclk(e)
    def wheelEvent(self, e):  self.mouse_hdlr.wheel(e); e.accept()


    def resizeEvent(self, e):
        if not self.is_running: return
        x, y = e.size().width(), e.size().height()
        self.zoomer.resize(x, y)
        txt = self.curr_txt
        if txt is not None: self.set_txt(txt)
        if self.show_help: self.display_help()
        self.set_bt()
        if self.helper.is_on(): self.helper._on() # refresh
        if not self.is_fullscreen():
            self.set_bg("orange" if self.border else "none")
        #print(self._m_area)


    def event(self, e):
        if e.type() == QEvent.WindowDeactivate:
            self._set_focus(False)
        elif e.type() == QEvent.WindowActivate:
            self._set_focus(True)
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
        if on: self.set_bg("orange" if self.border else "none")
        else:
            self.set_bg("green" if self.border else "none")

    def closeEvent(self, e):
        super().closeEvent(e)
        self.cleanup()

    def cleanup(self):
        if self.is_running:
            self.is_running = False
            if self.helper.is_on(): self.helper.off()
            #self.core.stop() # check nova.py

            gk = f"geom_{self.url_hash}"
            g = self.geometry()
            x, y, w, h = g.x(), g.y(), g.width(), g.height()
            self.qset.setValue(gk, f"{x},{y},{w},{h}")

            if self.pending_zoom_coord: return
            zk = f"zoom_{self.url_hash}"
            zx, zy, zw, zh = self.zoomer.getRect()
            vw, vh = self.core.width(), self.core.height()
            rx = zx / vw if vw > 0 else 0.0
            ry = zy / vh if vh > 0 else 0.0
            rw = zw / vw if vw > 0 else 1.0
            rh = zh / vh if vh > 0 else 1.0
            s = f'{rx},{ry},{rw},{rh}'
            self.qset.setValue(zk, s)



    def _screen_wh(self):
        s = QGuiApplication.screenAt(self.position()) or QGuiApplication.primaryScreen()
        #g = s.availableGeometry()
        g = s.geometry()
        #TODO: window-state machine bug + fullscreen detection heuristic bug
        #SAFE_MARGIN = 1  # avoid WM fullscreen heuristics
        SAFE_MARGIN = 0
        return g.width() - SAFE_MARGIN, g.height() - SAFE_MARGIN

    def _screen_wh_multi(self):
        s = QGuiApplication.primaryScreen()
        g = s.virtualGeometry()
        #TODO: window-state machine bug + fullscreen detection heuristic bug
        #SAFE_MARGIN = 1  # avoid WM fullscreen heuristics
        SAFE_MARGIN = 0
        return g.width() - SAFE_MARGIN, g.height() - SAFE_MARGIN

    def _widget_wh(self):
        return self.width(), self.height()

    def _widget_wh_scrn(self):
        sw, sh = self._screen_wh()
        return self.width(), self.height(), sw, sh

    def _widget_wh_scrn_multi(self):
        sw, sh = self._screen_wh_multi()
        return self.width(), self.height(), sw, sh

    def _curr_pos(self):
        g = self.geometry()   # global geometry
        return g.x(), g.y()
        #return self.position().x(), self.position().y()

    def _isFullScreen(self):
        return bool(self.windowState() & Qt.WindowFullScreen)
    def _isMaximized(self):
        return bool(self.windowState() & Qt.WindowMaximized)
    def is_fullscreen(self):
        return self._isFullScreen() or self._fullscrn_flag


    def fullscreen(self):
        vw, vh = self.core.width(), self.core.height()
        if self.is_fullscreen(): return
        self._fullscrn_flag = True
        self.set_bg("none")
        self.showFullScreen()
        #QTimer.singleShot(500, self.set_bt)

    def show_normal(self):
        if not self.is_fullscreen(): return
        if self._isFullScreen(): self.showNormal()
        self._fullscrn_flag = False
        self.set_bg("orange" if self.border else "none")

    def max_screen(self):
        vw, vh = self.core.width(), self.core.height()
        if vw <= 0 or vh <= 0: return
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
        w, h, sw, sh = self._widget_wh_scrn()
        #print(f'w:{w}, h:{h}, sw:{sw}, sh:{sh}');
        if self.is_fullscreen(): self.show_normal()
        else: self.fullscreen()

    def cut_widget(self):
        vw, vh = self.core.width(), self.core.height()
        if vw <= 0 or vh <= 0: return
        if self.is_fullscreen(): self.max_screen(); return

        r = self.zoomer.ratio()
        ow, oh = self._widget_wh()
        if oh < ow / r: nw, nh = round(oh * r), oh
        else: nw, nh = ow, round(ow / r)
        x, y = self._curr_pos()
        nx, ny = x + (ow - nw) // 2, y + (oh - nh) // 2
        #print(f'nx:{nx}, ny:{ny}, nw:{nw}, nh:{nh}')
        self._geom(nx, ny, nw, nh)

    def center_orig(self):
        vw, vh = self.core.width(), self.core.height()
        if vw <= 0 or vh <= 0: return
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
        x, y = self._curr_pos()
        w, h, sw, sh = self._widget_wh_scrn_multi()
        r = w / h if h > 0 else 1
        nh = snap_to_grid(h, GS)
        nw = round(nh * r)
        if x + nw >= sw and y + nh  >= sh:
            pass
        elif x + nw  >= sw:
            y = snap_to_grid(y, GS)
        elif y + nh >= sh:
            x = snap_to_grid(x, GS)
        else:
            x = snap_to_grid(x, GS)
            y = snap_to_grid(y, GS)
        nx, ny = min(x, sw - nw), min(y, sh - nh)
        self._geom(nx, ny, nw, nh)

    def move_up(self):   self._move_d(0,-GS)
    def move_down(self): self._move_d(0, GS)
    def move_left(self): self._move_d(-GS, 0)
    def move_right(self):self._move_d( GS, 0)

    def expand_right(self): self._geom_d(  0, 0, GS, 0)
    def shrink_right(self): self._geom_d(  0, 0,-GS, 0)
    def expand_left(self):  self._geom_d(-GS, 0,+GS, 0)
    def shrink_left(self):  self._geom_d(+GS, 0,-GS, 0)

    def expand_down(self):  self._geom_d(0,  0, 0, +GS)
    def shrink_down(self):  self._geom_d(0,  0, 0, -GS)
    def expand_up(self):    self._geom_d(0,-GS, 0, +GS)
    def shrink_up(self):    self._geom_d(0,+GS, 0, -GS)

    def expand_right_bottom(self): self._resize_r(1.1)   # 11/10
    def shrink_right_bottom(self): self._resize_r(0.909) # 10/11

    def expand(self): self._resize_rc(1.1)
    def shrink(self): self._resize_rc(0.909)

    def _geom(self, x, y, w, h):
        sw, sh = self._screen_wh_multi()
        mw, mh = self.minimumWidth(), self.minimumHeight()
        w, h = min(sw, max(mw, w)), min(sh, max(mh, h))
        x, y = max(0, x), max(0, y)
        self.setGeometry(x, y, w, h)
        #self.setPosition(x, y)
        #self.resize(w, h)

    def _geom_d(self, dx, dy, dw, dh): # +delta
        x, y = self._curr_pos()
        w, h = self.width(), self.height()
        self._geom(x + dx, y + dy, w + dw, h + dh)

    def _move(self, x, y):
        w, h, sw, sh = self._widget_wh_scrn_multi()
        x = max(0, min(x, sw - w))
        y = max(0, min(y, sh - h))
        self.setPosition(x, y)

    def _move_d(self, dx, dy):
        x, y = self._curr_pos()
        self._move(x + dx, y + dy)

    def _resize(self, w, h):
        self.resize(w, h)

    def _resize_d(self, dw, dh):
        x, y = self._curr_pos()
        self._resize(x + dw, y + dh)

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

