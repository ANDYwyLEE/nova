import os
import math
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

WHEEL_NOTCH = 120   # QWheelEvent.angleDelta() of one wheel notch; hi-res wheels and touchpads send fractions
# Pushing the wheel away zooms in, pulling it toward you zooms out, as maps, image viewers and games do.
# NOVA_WHEEL_INVERT=1 ([env] in cameras.ini) for the other way round.
WHEEL_INVERT = os.environ.get("NOVA_WHEEL_INVERT", "").strip().lower() in ("1", "true", "yes", "on")

class MouseHdlr:
    def __init__(self, parent, key_modifier):
        self.p = parent
        self.km = key_modifier
        self.moving = False
        self.resizing = False
        self._dblclk = False    # the release that ends a double-click is next
        self._drag = None       # (zone, global press position, geometry) while moving/resizing by hand
        self._pan = None        # last pointer position while Ctrl-dragging the zoomed region
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
        if e.button() == Qt.MiddleButton:
            self.p.zoomer.fit_widget(); self.p.set_bt(); self.p.update()
            return
        if e.button() != Qt.LeftButton: return
        # Qt delivers a double-click as press, release, press, double-click, release. The second press is
        # left to dblclk(): toggling an icon again would undo the first click, and a window move started here
        # keeps the window manager's pointer grab across the switch to fullscreen (the next click is lost).
        if e.flags() & Qt.MouseEventCreatedDoubleClick: return
        self._dblclk = False
        self._drag = None
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
        if e.modifiers() & Qt.ControlModifier and self.p.zoomer.is_zoom():
            self._pan = pos                     # Ctrl-drag moves the zoomed region, not the window
            self.p.setCursor(Qt.ClosedHandCursor)
            return
        if self.p.is_fullscreen(): return
        z = self._zone(pos)
        edge = self._edge(z)
        self.resizing = edge is not None
        self.moving = edge is None
        self.p._placed_by_user()
        started = self.p.startSystemResize(edge) if edge else self.p.startSystemMove()
        if not started:
            # The platform cannot do it (no startSystemResize on macOS, X11 window managers without
            # _NET_WM_MOVERESIZE): move / resize from the mouse moves until the button is released.
            self._drag = (z, e.globalPosition().toPoint(), self.p.geometry())

    def _drag_to(self, gpos):
        zone, start, g = self._drag
        dx, dy = gpos.x() - start.x(), gpos.y() - start.y()
        if zone == 'MOVE':
            self.p.setPosition(g.x() + dx, g.y() + dy)
            return
        x, y, w, h = g.x(), g.y(), g.width(), g.height()
        mw, mh = self.p.minimumWidth(), self.p.minimumHeight()
        if 'W' in zone:
            nw = max(mw, w - dx); x += w - nw; w = nw
        elif 'E' in zone:
            w = max(mw, w + dx)
        if 'N' in zone:
            nh = max(mh, h - dy); y += h - nh; h = nh
        elif 'S' in zone:
            h = max(mh, h + dy)
        self.p.setGeometry(x, y, w, h)

    def move(self, e):
        if self._pan is not None:
            if e.buttons() & Qt.LeftButton:
                pos = e.position().toPoint()
                d = pos - self._pan
                self._pan = pos
                self.p.zoomer.pan(d.x(), d.y())
                self.p.set_bt()
                self.p.update()
                return
            self._pan = None
        if self._drag is not None:
            if e.buttons() & Qt.LeftButton:
                self._drag_to(e.globalPosition().toPoint())
                return
            self._drag = None
        pos = e.position().toPoint()
        if self.p.in_bt_zone(pos):
            self.p.setCursor(Qt.PointingHandCursor)
            return
        if e.modifiers() & Qt.ControlModifier and self.p.zoomer.is_zoom():
            self.p.setCursor(Qt.OpenHandCursor)
            return
        if self.p.is_fullscreen():
            self.p.setCursor(Qt.ArrowCursor)
            return
        z = self._zone(pos)
        self.p.setCursor(self._cursor(z))

    def release(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = None
            if self._pan is not None:
                self._pan = None                # the region moved, not the window: nothing to snap
            elif self._dblclk:
                # After a double-click that left fullscreen, geometry() can still be the fullscreen rectangle
                # (the window manager restores the normal one when it gets to it): snapping that would make
                # the window screen-sized.
                self._dblclk = False
            else:
                self.p.align_grid()
        self.moving = self.resizing = False
        self.p.setCursor(Qt.ArrowCursor)

    def dblclk(self, e):
        if e.button() != Qt.LeftButton: return
        self._dblclk = True
        self._drag = None
        self.moving = self.resizing = False
        if self.p.in_bt_zone(e.position().toPoint()):
            return          # on the control strip: the first click has toggled the icon, nothing more
        self.p.toggle_fullscreen()

    def wheel(self, e):
        steps = e.angleDelta().y() / WHEEL_NOTCH         # > 0: the wheel was pushed away
        if not steps: return
        # macOS "natural scrolling" reports the wheel the other way round (inverted()); for zooming the
        # hand's movement is what counts, so follow the wheel as Qt recommends for non-scrolling uses
        if e.inverted(): steps = -steps
        if WHEEL_INVERT: steps = -steps
        pos = self.p.mapFromGlobal(e.globalPosition().toPoint())
        self.p.zoomer.zoom(pos, steps)
        self.p.set_bt()
        self.p.update()

    def pinch(self, e):
        """macOS trackpad pinch (QNativeGestureEvent: value() is the relative size change, e.g. 0.05 for
        5 % bigger): zoom around the fingers by exactly that."""
        v = e.value()
        if not v or v <= -1: return
        self.p.zoomer.zoom(e.position().toPoint(), math.log(1 + v) / math.log(11 / 10))   # a notch is 10 %
        self.p.set_bt()
        self.p.update()

