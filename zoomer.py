"""Zoom math: the zoomed region in frame pixels, and what the window shows of it."""
import logging
from PySide6.QtCore import QRectF, QPointF

class Zoomer(QRectF):
    """The zoomed region of the picture, in frame pixels (the rectangle itself). It is what the user chose
    and what is remembered; what the window shows is view_rect(): the region widened to the window's
    shape, so resizing the window keeps the region and reveals the picture around it, and narrowing the
    window back shows exactly the region again. The wheel and Ctrl-drag act on what is shown."""
    def __init__(self, w_video: int, h_video: int, w_widget: int, h_widget: int):
        super().__init__(0, 0, w_video, h_video)

        self.w_video = w_video
        self.h_video = h_video
        self.w_widget = w_widget
        self.h_widget = h_widget
        self._scale_cache = 1.0
        self._calc_scale()
        logging.trace(
            f"[Zoomer] __init__: {self.getRect()}, "
            f"video:({self.w_video}x{self.h_video}), "
            f"widget:({self.w_widget}x{self.h_widget})"
        )


    #def toRect(self): return self

    def set_rect(self, r):
        x1, y1 = max(0, r.x()), max(0, r.y())
        x2 = min(self.w_video, r.x() + r.width())
        y2 = min(self.h_video, r.y() + r.height())
        w, h = max(1, x2 - x1), max(1, y2 - y1)
        self.setRect(x1, y1, w, h)

        self._calc_scale()
        logging.debug(
            f"[Zoomer] set_rect: {self.getRect()}, "
            f"video:({self.w_video}x{self.h_video}), "
            f"widget:({self.w_widget}x{self.h_widget})"
        )

    def reset(self):
        self.setRect(0,0, self.w_video, self.h_video)
        self._calc_scale()
        logging.debug(
            f"[Zoomer] reset: {self.getRect()}, "
            f"video:({self.w_video}x{self.h_video}), "
            f"widget:({self.w_widget}x{self.h_widget})"
        )

    def resize(self, w, h):
        """The window changed size: the region stays, the scale follows (see view_rect)."""
        self.w_widget = w
        self.h_widget = h
        self.zoom(None, 0)

    def _set(self, r: QRectF):
        self.setRect(r.x(), r.y(), r.width(), r.height())

    def view_rect(self) -> QRectF:
        """What the window shows: the region widened about its centre to the window's shape, so the
        space a resize adds shows the picture around the region instead of black bars. It stops at the
        frame's edges (shifted, not cut) and only the part no picture can fill is letterboxed. The
        region itself is always shown whole."""
        vw, vh, ww, wh = self.w_video, self.h_video, self.w_widget, self.h_widget
        if not self.is_zoom() or ww <= 0 or wh <= 0 or vw <= 0 or vh <= 0:
            return QRectF(0, 0, vw, vh)
        x, y, w, h = self.x(), self.y(), self.width(), self.height()
        wr = ww / wh
        if w / h < wr: nw, nh = min(vw, h * wr), h      # window wider than the region: more at the sides
        else: nw, nh = w, min(vh, w / wr)                # taller: more above and below
        nx = min(max(0.0, x + (w - nw) / 2), vw - nw)
        ny = min(max(0.0, y + (h - nh) / 2), vh - nh)
        return QRectF(nx, ny, nw, nh)

    def scale(self):
        return self._scale_cache

    def _calc_scale(self):
        ww, wh = self.w_widget, self.h_widget
        if ww <= 0 or wh <= 0:
            self._scale_cache = 1.0; return
        v = self.view_rect()
        vw, vh = v.width(), v.height()
        if vw <= 0 or vh <= 0:
            self._scale_cache = 1.0; return
        fr = vw / vh; wr = ww / wh
        if fr < wr: h = float(wh); w = h * fr
        else: w = float(ww); h = w / fr
        self._scale_cache =  w / vw # or h / vh
        #print(self)

    def is_zoom(self):
        x, y, w, h = self.getRect()
        vw, vh = self.w_video, self.h_video
        return x != 0 or y != 0 or w != vw  or h != vh

    def ratio(self):
        """Aspect ratio of what is shown (the region widened to the window: view_rect)."""
        v = self.view_rect()
        return v.width() / v.height() if v.height() > 0 else 1

    def roi_ratio(self):
        """Aspect ratio of the region itself (right click sizes the window to it)."""
        w, h = self.width(), self.height()
        return w / h if h > 0 else 1

    def pan(self, dx, dy):
        """Ctrl-drag: move the region by a pointer movement of (dx, dy) window pixels, the picture
        following the pointer; it stops at the frame's edges."""
        if not self.is_zoom(): return
        self._set(self.view_rect())              # what is shown is what moves
        self._calc_scale()
        s = self._scale_cache or 1.0
        w, h = self.width(), self.height()
        x = min(max(0.0, self.x() - dx / s), self.w_video - w)
        y = min(max(0.0, self.y() - dy / s), self.h_video - h)
        self.setRect(x, y, w, h)
        self._calc_scale()

    def fit_widget(self):
        """Middle click. Zoomed: the region becomes what is shown, the picture a resize revealed included.
        Unzoomed: the picture cut to the window's shape (no bars)."""
        if self.is_zoom():
            self.set_rect(self.view_rect())
            return
        ww, wh = self.w_widget, self.h_widget
        x, y, w, h = self.getRect()
        w_ratio = ww / wh
        v_ratio = w / h
        if w_ratio > v_ratio: nw = w; nh = nw / w_ratio
        else: nh = h; nw = nh * w_ratio
        x1, y1 = x + (w - nw) // 2, y + (h - nh) // 2
        self.setRect(x1, y1 , nw, nh)
        self._calc_scale()

    def zoom(self, pos, step):
        if step != 0 and self.is_zoom():
            self._set(self.view_rect())          # zoom from what is shown, not from the region alone
        if pos == None:
            pos = QPointF(self.w_widget / 2, self.h_widget / 2)
        else:
            pos = QPointF(pos)

        # transform from widget to frame coordinate
        f_ratio = self.width() / self.height()
        w_ratio = self.w_widget / self.h_widget
        if w_ratio < f_ratio:
            ratio_fw = self.width() / self.w_widget
        else:
            ratio_fw = self.height() / self.h_widget
        pos = pos * ratio_fw + self.topLeft()
        diff_x = (self.w_widget * ratio_fw - self.width()) / 2
        diff_y = (self.h_widget * ratio_fw - self.height()) / 2
        pos.setX(pos.x() - diff_x)
        pos.setY(pos.y() - diff_y)

        #cut outside of frame area if  x < 0 or x > h or w
        pos.setX(pos.x() if pos.x() >= self.left() else self.left())
        pos.setY(pos.y() if pos.y() >= self.top() else self.top())
        pos.setX(pos.x() if pos.x() <= self.right() else self.right())
        pos.setY(pos.y() if pos.y() <= self.bottom() else self.bottom())

        # zooming calculate: 10 % per step (a key press, a wheel notch); a hi-res wheel or touchpad sends
        # fractions of a notch and zooms by that fraction
        K = (10 / 11) ** step if step > 0 else (11 / 10) ** -step
        x1 = pos.x() - (pos.x() - self.left()) * K
        x2 = pos.x() + (self.right() - pos.x()) * K
        y1 = pos.y() - (pos.y() - self.top()) * K
        y2 = pos.y() + (self.bottom() - pos.y()) * K

        # zomming out
        if step > 0:
            if x2 - x1 < 50 or y2 - y1 < 30:
                self._calc_scale()
                return

            if w_ratio < f_ratio:
                y1 = self.top()
                y2 = self.bottom()
                f_ratio = (x2 - x1) / (y2 - y1)
                if w_ratio > f_ratio:
                    h = y2 - y1
                    h1 = pos.y() - y1
                    h2 = y2 - pos.y()
                    target_h =  (x2 - x1)  / w_ratio
                    df = h - target_h
                    df1 = df * h1 / h
                    df2 = df * h2 / h
                    y1 = y1 + df1
                    y2 = y2 - df2

            elif w_ratio > f_ratio:
                x1 = self.left()
                x2 = self.right()
                f_ratio = (x2 - x1) / (y2 - y1)
                if w_ratio < f_ratio:
                    w = x2 - x1
                    w1 = pos.x() - x1
                    w2 = x2 - pos.x()
                    target_w =  (y2 - y1)  * w_ratio
                    df = w - target_w
                    df1 = df * w1 / w
                    df2 = df * w2 / w
                    x1 = x1 + df1
                    x2 = x2 - df2

        # zomming in
        elif step <= 0:
            if x2 - x1 > self.w_video:
                x1 = 0
                x2 = self.w_video
            if y2 - y1 > self.h_video:
                y1 = 0
                y2 = self.h_video


        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(self.w_video, x2), min(self.h_video, y2)
        w, h = max(1, x2 - x1), max(1, y2 - y1)
        self.setRect(x1, y1, w, h)
        self._calc_scale()




