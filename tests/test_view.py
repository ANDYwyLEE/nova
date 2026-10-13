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
    assert any(img.pixelColor(x, 90).lightness() > 200 for x in range(0, 320, 2)), "no text pixels drawn"
    assert img.pixelColor(2, 2).alpha() in range(120, 200)          # the dimming layer, not opaque
