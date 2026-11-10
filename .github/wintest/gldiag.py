"""Which OpenGL a Nova window gets on this Windows machine, as Qt sees it and as PyOpenGL sees it.

Nova's --opengl draws through PyOpenGL, which calls the system opengl32.dll. Without a GPU driver
Qt may fall back to its software opengl32sw.dll (Mesa llvmpipe); PyOpenGL then talks to a DLL that
has no current context. Prints one block per QT_OPENGL setting given on the command line.

    python .github/wintest/gldiag.py [default|desktop|software ...]
"""
import ctypes
import os
import subprocess
import sys

GL_VENDOR, GL_RENDERER, GL_VERSION, GL_DRAW_BUFFER = 0x1F00, 0x1F01, 0x1F02, 0x0C01


def probe():
    import PySide6
    from PySide6.QtGui import QGuiApplication, QOffscreenSurface, QOpenGLContext, QSurfaceFormat
    from PySide6.QtWidgets import QApplication
    print("PySide6", PySide6.__version__, "at", os.path.dirname(PySide6.__file__))
    print("opengl32sw.dll shipped:", os.path.exists(os.path.join(os.path.dirname(PySide6.__file__), "opengl32sw.dll")))
    app = QApplication([sys.argv[0]])
    print("platform", app.platformName(), "| QT_OPENGL =", os.environ.get("QT_OPENGL", "(unset)"))
    fmt = QSurfaceFormat()
    fmt.setRenderableType(QSurfaceFormat.OpenGL)
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.CoreProfile)
    ctx = QOpenGLContext()
    ctx.setFormat(fmt)
    print("3.3 core context created:", ctx.create())
    if not ctx.isValid():
        ctx = QOpenGLContext()
        print("any context created:", ctx.create())
    surf = QOffscreenSurface()
    surf.setFormat(ctx.format())
    surf.create()
    print("makeCurrent:", ctx.makeCurrent(surf), "| got", f"{ctx.format().majorVersion()}.{ctx.format().minorVersion()}",
          "ES" if ctx.isOpenGLES() else "desktop", "| module type", QOpenGLContext.openGLModuleType())
    f = ctx.functions()
    for name, e in (("vendor", GL_VENDOR), ("renderer", GL_RENDERER), ("version", GL_VERSION)):
        try:
            print(f"  Qt       {name:8}: {f.glGetString(e)!r}")
        except Exception as ex:                                   # noqa: BLE001
            print(f"  Qt       {name:8}: failed {ex!r}")
    if os.name == "nt":
        k32 = ctypes.WinDLL("kernel32")
        k32.GetModuleHandleW.restype = ctypes.c_void_p
        k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        for dll in ("opengl32.dll", "opengl32sw.dll", "libGL.dll", "libEGL.dll", "libGLESv2.dll"):
            print(f"  loaded {dll:15}: {bool(k32.GetModuleHandleW(dll))}")
    try:
        from OpenGL.GL import glGetIntegerv, glGetString
        for name, e in (("vendor", GL_VENDOR), ("renderer", GL_RENDERER), ("version", GL_VERSION)):
            print(f"  PyOpenGL {name:8}: {glGetString(e)!r}")
        print("  PyOpenGL GL_DRAW_BUFFER:", glGetIntegerv(GL_DRAW_BUFFER))
    except Exception as ex:                                       # noqa: BLE001
        print("  PyOpenGL failed:", type(ex).__name__, str(ex).splitlines()[0] if str(ex) else "")
    ctx.doneCurrent()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--probe":
        probe()
        sys.exit(0)
    for mode in sys.argv[1:] or ["default", "desktop", "software"]:
        env = dict(os.environ)
        env.pop("QT_QPA_PLATFORM", None)
        if mode != "default":
            env["QT_OPENGL"] = mode
        print(f"===== QT_OPENGL={mode}", flush=True)
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "--probe"], env=env,
                           capture_output=True, text=True, timeout=60)
        print(r.stdout + r.stderr[-2000:], f"(exit {r.returncode})", flush=True)
