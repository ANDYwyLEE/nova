from PySide6.QtCore import Qt, QPoint

BORDER = 6

ZONE_EDGE = {
    'N':  Qt.TopEdge,
    'S':  Qt.BottomEdge,
    'W':  Qt.LeftEdge,
    'E':  Qt.RightEdge,
    'NW': Qt.TopEdge    | Qt.LeftEdge,
    'NE': Qt.TopEdge    | Qt.RightEdge,
    'SW': Qt.BottomEdge | Qt.LeftEdge,
    'SE': Qt.BottomEdge | Qt.RightEdge,
}

ZONE_CURSOR = {
    'NW': Qt.SizeFDiagCursor,
    'SE': Qt.SizeFDiagCursor,
    'NE': Qt.SizeBDiagCursor,
    'SW': Qt.SizeBDiagCursor,
    'N':  Qt.SizeVerCursor,
    'S':  Qt.SizeVerCursor,
    'W':  Qt.SizeHorCursor,
    'E':  Qt.SizeHorCursor,
}

class MouseHdlr:
    def __init__(self, parent, key_modifier):
        self.p = parent
        self.km = key_modifier
        self.moving = False
        self.resizing = False
        #self.zone = 'NONE'

    def _zone(self, pos: QPoint):
        w, h = self.p.width(), self.p.height()
        x, y = pos.x(), pos.y()

        l = x < BORDER
        r = x > w - BORDER
        t = y < BORDER
        b = y > h - BORDER

        if t and l: return 'NW'
        if t and r: return 'NE'
        if b and l: return 'SW'
        if b and r: return 'SE'
        if t: return 'N'
        if b: return 'S'
        if l: return 'W'
        if r: return 'E'
        return 'MOVE'

    def _edge(self, zone):
        return ZONE_EDGE.get(zone)

    def _cursor(self, zone):
        return ZONE_CURSOR.get(zone, Qt.ArrowCursor)

    def press(self, e):
        if e.button() == Qt.RightButton: self.p.cut_widget(); return
        if e.button() == Qt.MiddleButton: self.p.zoomer.fit_widget(); return
        if e.button() != Qt.LeftButton: return
        pos = e.position().toPoint()
        if self.p.in_bt_zone(pos):
            if self.p.in_bt(pos, 0):
                self.p.toggle_streamer()
            elif self.p.in_bt(pos, 1):
                self.p.toggle_recorder()
            elif self.p.in_bt(pos, 2):
                self.p.toggle_audio()
            elif self.p.in_bt(pos, 4):
                self.p.toggle_help()
            return
        if self.p.is_fullscreen(): return
        z = self._zone(pos)
        edge = self._edge(z)
        self.resizing = edge is not None
        self.moving = edge is None
        self.p._placed_by_user()
        if edge: self.p.startSystemResize(edge)
        else: self.p.startSystemMove()

    def move(self, e):
        pos = e.position().toPoint()
        if self.p.in_bt_zone(pos):
            self.p.setCursor(Qt.PointingHandCursor)
            return
        if self.p.is_fullscreen():
            self.p.setCursor(Qt.ArrowCursor)
            return
        z = self._zone(pos)
        self.p.setCursor(self._cursor(z))

    def release(self, e):
        if e.button() == Qt.LeftButton:
            self.p.align_grid()
        self.moving = self.resizing = False
        self.p.setCursor(Qt.ArrowCursor)

    def dblclk(self, e):
        if e.button() == Qt.LeftButton:
            self.p.toggle_fullscreen()
            self.moving = self.resizing = False

    def wheel(self, e):
        pos = self.p.mapFromGlobal(e.globalPosition().toPoint())
        ALPHA = 0.0083
        self.p.zoomer.zoom(pos, -e.angleDelta().y() * ALPHA)
        self.p.set_bt()

