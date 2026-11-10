"""Nova's application icon: the files, what the window system is handed, and that the files are still
what their drawings render to."""
import importlib.util
import os
import shutil
import sys

import pytest
from PySide6.QtGui import QColor, QGuiApplication, QImage

import nova

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORANGE, GREEN, TILE = "#F0851A", "#1FA83A", "#15171C"


@pytest.fixture(scope="module")
def app():
    return QGuiApplication.instance() or QGuiApplication([sys.argv[0]])


@pytest.fixture(scope="module")
def gen_icon(app):
    spec = importlib.util.spec_from_file_location("gen_icon", os.path.join(ROOT, "tools", "gen_icon.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def is_colour(image, x, y, name):
    got, want = image.pixelColor(x, y), QColor(name)
    return got.alpha() == 255 and all(abs(a - b) <= 3 for a, b in zip(got.getRgb()[:3], want.getRgb()[:3]))


def test_every_size_is_a_square_picture_with_the_tile_to_its_edge(app):
    for size in nova.ICON_SIZES:
        image = QImage(os.path.join(nova.ICONS, f"nova-{size}.png"))
        assert (image.width(), image.height()) == (size, size), size
        assert image.pixelColor(0, 0).alpha() == 0, size             # outside the tile's round corner
        assert is_colour(image, size // 2, 0, TILE), size


def test_the_smallest_size_is_drawn_on_its_own_pixels(app):
    """The large drawing scaled to 16 px has lines thinner than a pixel and turns to mud on a panel. Its own
    drawing has the camera's line on two whole pixels and the line of the window behind it on one."""
    image = QImage(os.path.join(nova.ICONS, "nova-16.png"))
    assert is_colour(image, 8, 7, ORANGE) and is_colour(image, 8, 8, ORANGE)       # top of the camera body
    assert is_colour(image, 8, 9, TILE)                                             # and its inside
    assert is_colour(image, 5, 2, GREEN) and is_colour(image, 5, 3, TILE)           # top of the window


def test_the_mac_picture_has_the_margin_of_a_dock_icon(app):
    image = QImage(os.path.join(nova.ICONS, "nova-mac.png"))
    assert image.width() == image.height() == 512
    margin = int(512 * (1 - 824 / 1024) / 2)                    # 50 px on each side
    assert image.pixelColor(256, margin - 2).alpha() == 0
    assert is_colour(image, 256, margin + 2, TILE)


def test_the_application_icon_offers_each_size_and_macos_gets_its_own_picture(app, monkeypatch):
    monkeypatch.setattr(nova, "MAC", False)
    assert sorted(s.width() for s in nova.app_icon().availableSizes()) == list(nova.ICON_SIZES)
    monkeypatch.setattr(nova, "MAC", True)
    assert [s.width() for s in nova.app_icon().availableSizes()] == [512]


def test_the_icon_files_are_what_their_drawings_render_to(gen_icon):
    assert sorted(s for sizes in gen_icon.SIZES.values() for s in sizes) == list(nova.ICON_SIZES)
    assert gen_icon.stale() == [], gen_icon.differences()


def test_a_changed_drawing_is_noticed(gen_icon, tmp_path, monkeypatch):
    """The check behind `tools/gen_icon.py --check`: a line moved by one pixel of the 16 px grid makes the
    files of that drawing stale, the files of the other drawings stay as they are."""
    icons = tmp_path / "icons"
    shutil.copytree(nova.ICONS, icons)
    drawing = icons / "nova-small.svg"
    text = drawing.read_text(encoding="utf-8")
    assert text.count('y="80"') == 1
    drawing.write_text(text.replace('y="80"', 'y="112"'), encoding="utf-8")
    monkeypatch.setattr(gen_icon, "ICONS", str(icons))
    assert gen_icon.stale() == ["nova-16.png", "nova-24.png"]
    os.remove(icons / "nova-mac.png")
    assert gen_icon.stale() == ["nova-16.png", "nova-24.png", "nova-mac.png"]
