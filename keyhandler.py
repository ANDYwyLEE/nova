from PySide6.QtCore import Qt


class Modifier:
    def __init__(self):
        self.ctrl = False
        self.alt = False
        self.shift = False
        self.space = False

class KeyHdlr:
    def __init__(self, parent):
        self.p = parent # parent is Nova class
        self.m = Modifier()

    def get_modifier(self):
        return self.m

    def press(self, event):
        k = event.key()

        # Modifiers
        if k in (Qt.Key_Control, Qt.Key_Meta): self.m.ctrl = True
        if k == Qt.Key_Alt: self.m.alt = True
        if k == Qt.Key_Shift: self.m.shift = True
        if k == Qt.Key_Space: self.m.space = True

        grid = self.p.grid

        # Actions
        if k ==  Qt.Key_C and self.m.ctrl:
            self.p.close()

        elif k == Qt.Key_Escape:
            if self.p.is_fullscreen(): self.p.show_normal()
            self.p.zoomer.reset()

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

        # Grid Move
        elif k == Qt.Key_Up:
            if self.m.ctrl:  self.p.expand_up()
            elif self.m.alt: self.p.shrink_down()
            else:          self.p.move_up()
        elif k == Qt.Key_Down:
            if self.m.ctrl:  self.p.expand_down()
            elif self.m.alt: self.p.shrink_up()
            else:          self.p.move_down()
        elif k == Qt.Key_Left:
            if self.m.ctrl:  self.p.expand_left()
            elif self.m.alt: self.p.shrink_right()
            else:          self.p.move_left()
        elif k == Qt.Key_Right:
            if self.m.ctrl:  self.p.expand_right()
            elif self.m.alt: self.p.shrink_left()
            else:          self.p.move_right()

        # Size change
        elif k == Qt.Key_PageDown: self.p.expand_right_bottom()
        elif k == Qt.Key_PageUp:   self.p.shrink_right_bottom()

        elif k == Qt.Key_Home:
            self.p.center_orig()

        # Window Size + Zoom
        elif k in [Qt.Key_Plus, Qt.Key_Equal, Qt.Key_I]:
            if self.m.ctrl: self.p.expand()
            else: self.p.zoomer.zoom(None, 1)

        elif k in [Qt.Key_Minus, Qt.Key_Underscore, Qt.Key_O]:
            if self.m.ctrl: self.p.shrink()
            else: self.p.zoomer.zoom(None, -1)

        elif k == Qt.Key_B:
            self.p.border = not self.p.border
            self.p.set_bg(border='orange' if self.p.border else 'none')

    def release(self, event):
        k = event.key()
        if k in (Qt.Key_Control, Qt.Key_Meta): self.m.ctrl = False
        if k == Qt.Key_Alt: self.m.alt = False
        if k == Qt.Key_Shift: self.m.shift = False
        if k == Qt.Key_Space: self.m.space = False

