#!/usr/bin/env python3
"""Render Nova's icon files (icons/*.png) from its three drawings.

    python tools/gen_icon.py            (from the repository root with the venv active)
    python tools/gen_icon.py --check    exit 1 when a PNG is missing or no longer matches its drawing

    icons/nova-small.svg   ->  nova-16.png  nova-24.png
    icons/nova-medium.svg  ->  nova-32.png  nova-48.png
    icons/nova.svg         ->  nova-64.png  nova-128.png  nova-256.png  nova-mac.png

Fewer pixels need a simpler drawing with heavier lines, hence three. nova-mac.png (512 px) has the
tile at 824/1024 of the picture, the margin macOS gives its own icons: the Dock shows the picture as
it is, and a tile to the edge would stand out larger than its neighbours.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QRectF                                   # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter        # noqa: E402
from PySide6.QtSvg import QSvgRenderer                              # noqa: E402

ICONS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icons")
SIZES = {"nova-small.svg": (16, 24), "nova-medium.svg": (32, 48), "nova.svg": (64, 128, 256)}
MAC, MAC_SIZE, MAC_TILE = "nova-mac.png", 512, 824 / 1024
TOLERANCE = 0.5         # mean difference per channel (0..255) allowed to a renderer on another platform


def render(drawing, size, tile=1.0):
    """The drawing as a size x size picture, the whole of it scaled to `tile` of the side and centred."""
    renderer = QSvgRenderer(os.path.join(ICONS, drawing))
    if not renderer.isValid():
        sys.exit(f"{drawing}: not a drawing QtSvg can read")
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    side = size * tile
    renderer.render(painter, QRectF((size - side) / 2, (size - side) / 2, side, side))
    painter.end()
    return image


def pictures():
    """(file name, picture) for every icon file."""
    for drawing, sizes in SIZES.items():
        for size in sizes:
            yield f"nova-{size}.png", render(drawing, size)
    yield MAC, render("nova.svg", MAC_SIZE, MAC_TILE)


def difference(a, b):
    """Mean difference per channel between two pictures, None when their sizes differ."""
    if a.size() != b.size():
        return None
    a, b = (i.convertToFormat(QImage.Format_RGBA8888) for i in (a, b))
    x, y = bytes(a.constBits()), bytes(b.constBits())
    return sum(abs(p - q) for p, q in zip(x, y)) / len(x)


def differences():
    """{file name: how far the file is from what its drawing renders to now (None: missing or another size)}."""
    out = {}
    for name, image in pictures():
        on_disk = QImage(os.path.join(ICONS, name))
        out[name] = None if on_disk.isNull() else difference(image, on_disk)
    return out


def stale():
    """The icon files that are missing or no longer what their drawing renders to."""
    return [name for name, d in differences().items() if d is None or d > TOLERANCE]


if __name__ == "__main__":
    app = QGuiApplication.instance() or QGuiApplication([sys.argv[0]])
    if "--check" in sys.argv[1:]:
        old = stale()
        if old:
            sys.exit("out of date, run tools/gen_icon.py: " + ", ".join(old))
        print("icons match their drawings")
    else:
        for name, image in pictures():
            image.save(os.path.join(ICONS, name))
            print(f"icons/{name}  {image.width()} px")
