import logging
from PySide6.QtCore import QRectF, QPointF

class Zoomer(QRectF):
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
        self.w_widget = w
        self.h_widget = h
        #TODO
        #self.reset()
        self.zoom(None, 0)

    def scale(self):
        return self._scale_cache

    def _calc_scale(self):
        ww, wh = self.w_widget, self.h_widget
        if ww <= 0 or wh <= 0: return 1.0
        vw, vh = self.w_video, self.h_video
        if self.is_zoom(): vw, vh = self.width(), self.height()
        if vw <= 0 or vh <= 0: return 1.0
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
        w, h = self.width(), self.height()
        return w / h if h > 0 else 1

    def fit_widget(self):
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

        # zooming calculate
        K = 10 / 11 if step > 0 else 11 / 10
        if step == 0: K = 1
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




