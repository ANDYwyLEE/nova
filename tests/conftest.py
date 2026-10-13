"""Shared fixtures. Tests run against the checkout directly (no package install)."""
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")     # no display on CI runners

# Window settings (geometry/zoom per camera) must never reach the user's real Nova.conf: on Linux
# QSettings follows XDG_CONFIG_HOME, elsewhere the ini format below is pointed at a temp dir.
SETTINGS_DIR = tempfile.mkdtemp(prefix="nova-test-settings-")
os.environ["XDG_CONFIG_HOME"] = SETTINGS_DIR
try:
    from PySide6.QtCore import QSettings
    QSettings.setDefaultFormat(QSettings.IniFormat)
    for fmt in (QSettings.IniFormat, QSettings.NativeFormat):
        QSettings.setPath(fmt, QSettings.UserScope, SETTINGS_DIR)
except ImportError:                                       # pragma: no cover
    pass


@pytest.fixture(scope="session")
def test_clip(tmp_path_factory):
    """A 3 s, 320x180, 25 fps H.264 clip written with PyAV, so no ffmpeg binary is needed."""
    import av
    import numpy as np

    path = tmp_path_factory.mktemp("media") / "clip.mp4"
    with av.open(str(path), "w") as out:
        v = out.add_stream("libx264", rate=25)
        v.width, v.height, v.pix_fmt = 320, 180, "yuv420p"
        v.codec_context.options = {"preset": "ultrafast", "tune": "zerolatency"}
        for i in range(75):
            img = np.zeros((180, 320, 3), dtype=np.uint8)
            img[:, :, 0] = np.linspace(0, 255, 320, dtype=np.uint8)[None, :]
            img[:, :, 1] = (i * 3) % 256
            img[:, (i * 4) % 320:(i * 4) % 320 + 8, :] = 255          # a moving bar
            frame = av.VideoFrame.from_ndarray(img, format="rgb24")
            frame.pts = i
            for pkt in v.encode(frame):
                out.mux(pkt)
        for pkt in v.encode(None):
            out.mux(pkt)
    return path
