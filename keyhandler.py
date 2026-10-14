"""Keyboard handling of the camera window: the key map (zoom, pan, record, stream, audio, snapshot,
window placement) and the modifier state."""
from PySide6.QtCore import Qt


class Modifier:
    def __init__(self):
        self.ctrl = False
        self.alt = False
        self.shift = False
        self.space = False

# Keys that act again while held down (auto-repeat): moving, resizing and zooming. Everything else acts once
# per press: a held R toggled recording on and off (each off() blocks while the file is closed), S wrote a
# snapshot per repeat, F made the window flicker between fullscreen and normal.
REPEATING = {
    Qt.Key_Up, Qt.Key_Down, Qt.Key_Left, Qt.Key_Right, Qt.Key_PageUp, Qt.Key_PageDown,
    Qt.Key_Plus, Qt.Key_Equal, Qt.Key_I, Qt.Key_Minus, Qt.Key_Underscore, Qt.Key_O,
}

PAN_STEP = 0.1      # Shift+Arrow moves the zoomed region by this fraction of what is shown


class KeyHdlr:
    def __init__(self, parent):
        self.p = parent # parent is Nova class
        self.m = Modifier()

    def get_modifier(self):
        return self.m

    def _sync_modifiers(self, event):
        # Qt's modifier state is authoritative; key press/release tracking alone gets stuck
        # when focus is lost while a modifier is held.
        mods = event.modifiers()
        self.m.ctrl = bool(mods & (Qt.ControlModifier | Qt.MetaModifier))
        self.m.alt = bool(mods & Qt.AltModifier)
        self.m.shift = bool(mods & Qt.ShiftModifier)

    def press(self, event):
        k = event.key()
        self._sync_modifiers(event)

        # Modifiers
        if k in (Qt.Key_Control, Qt.Key_Meta): self.m.ctrl = True
        if k == Qt.Key_Alt: self.m.alt = True
        if k == Qt.Key_Shift: self.m.shift = True
        if k == Qt.Key_Space: self.m.space = True

        if event.isAutoRepeat() and k not in REPEATING: return

        # Actions
        if k ==  Qt.Key_C and self.m.ctrl:
            self.p.close()

        elif k == Qt.Key_Escape:
            if self.p.is_fullscreen(): self.p.show_normal()
            self.p.reset_zoom()

        elif k == Qt.Key_F or k == Qt.Key_F11:
            if self.p.is_fullscreen(): self.p.show_normal()
            else: self.p.fullscreen()

        elif k == Qt.Key_S:
                self.p.snapshot()

        elif k == Qt.Key_R or k == Qt.Key_F7:
            self.p.toggle_recorder()

        elif k == Qt.Key_T or k == Qt.Key_F8:
            self.p.toggle_streamer()

        elif k == Qt.Key_A or k == Qt.Key_F5:
            self.p.toggle_audio()

        elif k == Qt.Key_H or k == Qt.Key_F1:
            self.p.toggle_help()

        elif k == Qt.Key_P:
            self.p.print_info()

        # Arrows: the window moves (plain), grows (Ctrl) or shrinks (Alt) on that edge; the zoomed region
        # moves inside the window (Shift), a tenth of what is shown per step
        elif k == Qt.Key_Up:
            if self.m.ctrl:    self.p.expand_up()
            elif self.m.alt:   self.p.shrink_down()
            elif self.m.shift: self.p.pan_region(0, -PAN_STEP)
            else:              self.p.move_up()
        elif k == Qt.Key_Down:
            if self.m.ctrl:    self.p.expand_down()
            elif self.m.alt:   self.p.shrink_up()
            elif self.m.shift: self.p.pan_region(0, PAN_STEP)
            else:              self.p.move_down()
        elif k == Qt.Key_Left:
            if self.m.ctrl:    self.p.expand_left()
            elif self.m.alt:   self.p.shrink_right()
            elif self.m.shift: self.p.pan_region(-PAN_STEP, 0)
            else:              self.p.move_left()
        elif k == Qt.Key_Right:
            if self.m.ctrl:    self.p.expand_right()
            elif self.m.alt:   self.p.shrink_left()
            elif self.m.shift: self.p.pan_region(PAN_STEP, 0)
            else:              self.p.move_right()

        # Size change
        elif k == Qt.Key_PageDown: self.p.expand_right_bottom()
        elif k == Qt.Key_PageUp:   self.p.shrink_right_bottom()

        elif k == Qt.Key_Home:
            self.p.center_orig()

        # Window Size + Zoom
        elif k in [Qt.Key_Plus, Qt.Key_Equal, Qt.Key_I]:
            if self.m.ctrl: self.p.expand()
            else: self.p.zoomer.zoom(None, 1); self.p.set_bt(); self.p.update()

        elif k in [Qt.Key_Minus, Qt.Key_Underscore, Qt.Key_O]:
            if self.m.ctrl: self.p.shrink()
            else: self.p.zoomer.zoom(None, -1); self.p.set_bt(); self.p.update()

        elif k == Qt.Key_B:
            self.p.border = not self.p.border
            self.p.set_bg(self.p._border_color())

    def release(self, event):
        k = event.key()
        if k in (Qt.Key_Control, Qt.Key_Meta): self.m.ctrl = False
        if k == Qt.Key_Alt: self.m.alt = False
        if k == Qt.Key_Shift: self.m.shift = False
        if k == Qt.Key_Space: self.m.space = False
