"""Player: what the recorder, the streamer and the audio toggle have in common: on / off state, an
icon for the window's status strip, Qt signals."""
from abc import abstractmethod, ABCMeta
import queue
import numpy as np
from PySide6.QtCore import QObject
from PySide6.QtGui import QImage

class QABCMeta(type(QObject), ABCMeta):
    pass

# Declare an abstract base class Player
class Player(QObject, metaclass=QABCMeta):
    def __init__(self, name, iconname):
        super().__init__()
        self.name = name
        self.icon = QImage(iconname)
        self.icon_gray = self._make_gray(self.icon)

    # Define the required abstract methods

    @abstractmethod
    def available(self):
        return True

    @abstractmethod
    def is_on(self):
        return False

    @abstractmethod
    def is_off(self):
        return not self.is_on()

    @abstractmethod
    def on(self): pass

    @abstractmethod
    def off(self): pass

    @abstractmethod
    def close(self): pass

    # parent default methods
    def get_icon(self):
        return self.icon if self.is_on() else self.icon_gray

    @staticmethod
    def _drain(q: queue.Queue):
        """Drop stale items so a freshly started consumer begins with live data."""
        try:
            while True: q.get_nowait()
        except queue.Empty:
            pass

    def _make_gray(self, image):
        """Half-transparent grayscale copy of the icon (vectorised with numpy)."""
        img = image.convertToFormat(QImage.Format_RGBA8888)
        if img.isNull():
            return img
        w, h = img.width(), img.height()
        raw = np.frombuffer(img.constBits(), dtype=np.uint8)
        px = raw.reshape(h, img.bytesPerLine())[:, :w * 4].reshape(h, w, 4)
        rgb = px[..., :3].astype(np.float32)
        gray = (0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]).astype(np.uint8)
        out = np.empty((h, w, 4), dtype=np.uint8)
        out[..., 0] = out[..., 1] = out[..., 2] = gray
        out[..., 3] = px[..., 3] // 2
        return QImage(out.data, w, h, w * 4, QImage.Format_RGBA8888).copy()
