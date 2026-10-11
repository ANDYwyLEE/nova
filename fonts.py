
from PySide6.QtGui import QFont
from util import LINUX, WINDOWS, MAC, WSL

def get_monospace_families():
    common_fallbacks = ["Courier New", "Courier", "monospace"]

    if WINDOWS:
        return [
            "Consolas",
            "Cascadia Code",
            "Lucida Console",
        ] + common_fallbacks

    elif MAC:
        return [
            "Menlo",
            "Monaco",
            "SF Mono",
            "Andale Mono",
        ] + common_fallbacks

    elif WSL:
        return [
            "Consolas",
            "Cascadia Code",
            "DejaVu Sans Mono",
            "Ubuntu Mono",
        ] + common_fallbacks

    elif LINUX:
        return [
            "DejaVu Sans Mono",
            "Liberation Mono",
            "Ubuntu Mono",
            "Noto Mono",
            "FreeMono",
        ] + common_fallbacks

    else:
        return [
            "Consolas",
            "Menlo",
            "DejaVu Sans Mono",
            "Liberation Mono",
            "Roboto Mono",
            "Ubuntu Mono",
            "Monaco",
        ] + common_fallbacks


MONO = QFont()
MONO.setFamilies(get_monospace_families())
MONO.setStyleHint(QFont.Monospace)
# MONO.setPointSize(12)
