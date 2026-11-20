"""Run by test_opengl_version.py in an environment whose OpenGL is held at a version: a NovaGL window is
given pictures of one colour, as yuv420p and as nv12, and says what colour its framebuffer holds.

    python tests/gl_pixels.py  ->  lines "FORMAT R G B" and
                                   "GL VERSION legacy=BOOL vaos=BOOL mipmaps=BOOL alpha=BITS swap=INTERVAL"

GL_PIXELS_NO_VAO=1: as on a driver whose OpenGL 2.x has no vertex array objects.
"""
import faulthandler
import os
import sys
import time

# a run that hangs says where, and ends (one did for 120 s without a line: an X server that was starting anew)
faulthandler.dump_traceback_later(60, exit=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import av                                                    # noqa: E402
import numpy as np                                           # noqa: E402
from PySide6.QtGui import QGuiApplication                    # noqa: E402

app = QGuiApplication(sys.argv[:1])
# as nova.py: PyOpenGL has to use the context API of the window Qt makes (it guesses EGL in a Wayland login,
# also for an XWayland window, whose context is GLX)
if app.platformName().startswith("wayland"):
    os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
elif app.platformName() == "xcb":
    os.environ.setdefault("PYOPENGL_PLATFORM", "egl" if os.environ.get("QT_XCB_GL_INTEGRATION", "") == "xcb_egl" else "glx")

import novagl                                                # noqa: E402
from dispframe import DisplayFrame                           # noqa: E402
from test_gui_window import FakeCore                         # noqa: E402

if os.environ.get("GL_PIXELS_NO_VAO"):                      # a 2.x driver without vertex array objects
    _extensions = novagl.glGetString
    novagl.glGetString = lambda name: _extensions(name).replace(b"GL_ARB_vertex_array_object", b"GL_x") \
        if name == novagl.GL_EXTENSIONS else _extensions(name)

have = novagl.opengl_offered()
if have is not None:
    sys.exit(f"no context for --opengl here: {have}")
class Probe(novagl.NovaGL):
    """The window, with the colour in the middle of what it has just drawn (the framebuffer can only be
    read inside paintGL: after the buffers are swapped the back buffer holds nothing in particular)."""
    seen = None

    def paintGL(self):
        super().paintGL()
        img = self.grabFramebuffer()
        c = img.pixelColor(img.width() // 2, img.height() // 2)
        self.seen = (c.red(), c.green(), c.blue())


core = FakeCore("gl_pixels")
core.w, core.h = 320, 180
w = Probe(core, geometry="0,0,320,180")
w.show()
for _ in range(20):
    app.processEvents()
RGB = (200, 60, 30)
for fmt in ("yuv420p", "nv12"):
    rgb = av.VideoFrame.from_ndarray(np.full((180, 320, 3), RGB, np.uint8), format="rgb24")
    frame = rgb.reformat(format=fmt, dst_colorspace="ITU601", dst_color_range="MPEG")
    w.frame_q.append(DisplayFrame.from_av_frame(frame))
    w.update_img()                                           # as the worker's signal does
    w.seen = None
    deadline = time.monotonic() + 5
    while w.seen is None and time.monotonic() < deadline:    # the paint comes with the next frame of the screen
        app.processEvents()
        time.sleep(0.01)
    if w.seen is None or w._gl_failures:
        sys.exit(f"{fmt}: nothing was drawn (exposed {w.isExposed()}, failures {w._gl_failures})")
    print(fmt, *w.seen, flush=True)
print("GL", ".".join(map(str, w.format().version())), f"legacy={w.legacy} vaos={w.vaos} mipmaps={w.mipmaps} "
      f"alpha={w.requestedFormat().alphaBufferSize()} swap={w.requestedFormat().swapInterval()}", flush=True)
w.is_running = False
os._exit(0)
