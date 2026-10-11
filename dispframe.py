# dispframe.py
# A decoded frame handed from VideoWorker to the GUI thread.
import numpy as np
from PySide6.QtGui import QImage

# Formats the OpenGL renderer can upload directly (no CPU colour conversion).
GL_DIRECT_FORMATS = ("nv12", "yuv420p", "yuvj420p")


class DisplayFrame:
    """kind == 'rgba': qimg is a QImage (RGBA8888) backed by `buf`.
       kind == 'yuv' : frame is an av.VideoFrame in one of GL_DIRECT_FORMATS."""
    __slots__ = ("kind", "qimg", "frame", "width", "height", "buf", "_cached_qimg")

    def __init__(self, kind, width, height, qimg=None, frame=None, buf=None):
        self.kind = kind
        self.width = width
        self.height = height
        self.qimg = qimg
        self.frame = frame
        self.buf = buf
        self._cached_qimg = None

    @property
    def pix_fmt(self):
        return self.frame.format.name if self.frame is not None else "rgba"

    def to_qimage(self) -> QImage:
        """RGBA QImage of the whole frame (used by snapshots and the QPainter fallback)."""
        if self.kind == "rgba":
            return self.qimg
        if self._cached_qimg is None:
            rgba = np.ascontiguousarray(self.frame.to_ndarray(format="rgba"))
            h, w = rgba.shape[:2]
            self._cached_qimg = QImage(rgba.data, w, h, w * 4, QImage.Format_RGBA8888).copy()
        return self._cached_qimg

    @staticmethod
    def from_rgba_array(rgba: np.ndarray) -> "DisplayFrame":
        rgba = np.ascontiguousarray(rgba)
        h, w = rgba.shape[:2]
        qimg = QImage(rgba.data, w, h, w * 4, QImage.Format_RGBA8888)
        # No .copy(): the ndarray is kept alive in `buf` for as long as the QImage lives.
        return DisplayFrame("rgba", w, h, qimg=qimg, buf=rgba)

    @staticmethod
    def from_av_frame(frame) -> "DisplayFrame":
        return DisplayFrame("yuv", frame.width, frame.height, frame=frame)
