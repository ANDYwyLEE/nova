"""View logic that can be checked without a window (QT_QPA_PLATFORM=offscreen from conftest)."""
import os
import sys
from types import SimpleNamespace

from PySide6.QtGui import QGuiApplication, QImage

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _stub():
    from view import Nova
    repaints = []
    stub = SimpleNamespace(is_running=True, osd_hlp=QImage(8, 8, QImage.Format_RGBA8888_Premultiplied),
                           _widget_wh=lambda: (320, 180), update=lambda: repaints.append(1),
                           devicePixelRatio=lambda: 1.0)
    stub._osd_image = lambda: Nova._osd_image(stub)
    return stub, repaints


def test_help_off_clears_the_dimming_layer():
    # Helper.off() emits None through a Signal(str); PySide6 delivers "" to the slot. Both must mean "off",
    # or the dark layer is rebuilt without any text and the window just looks darker (seen on a camera wall).
    from view import Nova
    for off in ("", None):
        stub, repaints = _stub()
        Nova.set_hlp(stub, off)
        assert stub.osd_hlp.isNull() and repaints


def test_help_on_draws_text_over_a_dimming_layer():
    app = QGuiApplication.instance() or QGuiApplication([])
    from view import Nova
    stub, _ = _stub()
    Nova.set_hlp(stub, "H : help\nS : snapshot")
    img = stub.osd_hlp
    assert not img.isNull() and (img.width(), img.height()) == (320, 180)
    # two lines of small text around the middle (5 pt on Windows: a single row may fall between them, and
    # MSYS2's Qt antialiases the strokes thinner than the PyPI wheel): every pixel, against the black layer
    lightest = max(img.pixelColor(x, y).lightness() for y in range(img.height()) for x in range(img.width()))
    assert lightest > 100, f"no text pixels drawn (lightest {lightest})"
    assert img.pixelColor(2, 2).alpha() in range(120, 200)          # the dimming layer, not opaque


def test_overlay_text_shrinks_until_it_fits():
    """The help used a point size from the window's size alone: on Windows (Consolas 14 pt) its last seven
    lines fell below a 1920x1080 window, on Linux below 1280x720; status text was cut at both sides of a
    320x180 window. _fit_font steps the size down until the wrapped text fits."""
    app = QGuiApplication.instance() or QGuiApplication([])
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QPainter
    import helper
    from fonts import MONO
    from view import _fit_font
    text = helper.generate_help_text(helper.ARG_DESCRIPTIONS, helper.control_descriptions())
    flags = Qt.AlignLeft | Qt.TextWordWrap
    for w, h in ((1920, 1080), (1280, 720), (960, 540)):
        img = QImage(w, h, QImage.Format_ARGB32)
        p = QPainter(img)
        br = _fit_font(p, MONO, 14, 4, int(w * 0.8), h, flags, text)
        size = p.font().pointSize()
        p.end()
        assert br.height() <= h and br.width() <= int(w * 0.8), (w, h, size, br)
    img = QImage(320, 180, QImage.Format_ARGB32)
    p = QPainter(img)
    msg = "Unreachable: 192.168.1.26:554 does not answer"
    br = _fit_font(p, MONO, 14, 5, 304, 180, Qt.AlignCenter | Qt.TextWordWrap, msg)
    assert br.width() <= 304 and br.height() <= 180
    br = _fit_font(p, MONO, 14, 5, 3000, 1000, Qt.AlignCenter | Qt.TextWordWrap, "Connecting...")
    assert p.font().pointSize() == 14                    # what fits keeps the size asked for
    p.end()
