"""An --opengl window where the GPU's driver stops below OpenGL 3.3.  The shaders are '#version 330 core':
the window closed at start, or the process ended with a segmentation fault at the first OpenGL call, and
`nova doctor` said the OpenGL was fine.  At OpenGL 3.1 (a Raspberry Pi 4 and 5) the window draws with the
same shaders in GLSL 1.40; below that (a Raspberry Pi 3, Intel graphics of 2010 and before) the camera is
shown in the window that needs no shader."""
import os
import shutil
import signal
import subprocess
import sys
import time

import pytest
from PySide6.QtGui import QGuiApplication

from util import LINUX

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def qapp():
    return QGuiApplication.instance() or QGuiApplication([])


def test_the_question_has_an_answer_on_every_machine(qapp):
    """None where a context for the shaders is there, and where no context can be made at all (the offscreen
    platform of a runner): never an exception before the window."""
    from novagl import opengl_offered
    have = opengl_offered()
    assert have is None or have.startswith("OpenGL "), have


def test_the_shaders_in_glsl_1_40_are_the_same_shaders():
    import novagl
    for source in (novagl.VERTEX_SHADER, novagl.FRAGMENT_SHADER):
        older = novagl.glsl_140(source)
        assert older.strip().startswith("#version 140") and "layout" not in older and "330" not in older
        assert older.split("\n", 2)[2].replace(" ", "") == source.replace("layout (location = 0) ", "").replace(
            "layout (location = 1) ", "").split("\n", 2)[2].replace(" ", "")
    vertex = novagl.glsl_140(novagl.VERTEX_SHADER)
    assert all(f"in vec{n} {name};" in vertex for n, name in zip((3, 2), novagl.ATTRIBUTES))    # bound by name


def _mesa(tmp_path, version, glsl):
    """The environment of an X display whose OpenGL is Mesa's software renderer held at `version`."""
    if not LINUX or not shutil.which("Xvfb"):
        pytest.skip("needs Linux with Xvfb")
    x = subprocess.Popen(["Xvfb", "-displayfd", "1", "-screen", "0", "640x480x24", "-nolisten", "tcp"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        number = x.stdout.readline().strip()
        if not number:
            pytest.skip("Xvfb did not start")
        env = {k: v for k, v in os.environ.items() if k not in ("WAYLAND_DISPLAY", "XDG_SESSION_TYPE")}
        env.update(DISPLAY=f":{number}", QT_QPA_PLATFORM="xcb", XDG_CONFIG_HOME=str(tmp_path / "config"),
                   LIBGL_ALWAYS_SOFTWARE="1", __GLX_VENDOR_LIBRARY_NAME="mesa",
                   MESA_GL_VERSION_OVERRIDE=version, MESA_GLSL_VERSION_OVERRIDE=glsl)
        probe = subprocess.run([sys.executable, os.path.join(ROOT, "doctor.py"), "--gl"], env=env, cwd=ROOT,
                               capture_output=True, text=True, timeout=60)
        if f"(context {version})" not in probe.stdout:
            pytest.skip(f"no Mesa OpenGL on an X display here: {(probe.stdout or probe.stderr).strip()[-200:]}")
        yield env
    finally:
        x.terminate()
        x.wait(timeout=10)


@pytest.fixture
def mesa_3_1(tmp_path):
    yield from _mesa(tmp_path, "3.1", "140")


@pytest.fixture
def mesa_3_0(tmp_path):
    yield from _mesa(tmp_path, "3.0", "130")


def _a_window(env, test_clip, tmp_path):
    """What nova.py --opengl says while it shows the clip for a second or two in that environment."""
    log = tmp_path / "nova.log"
    cmd = [sys.executable, "-u", os.path.join(ROOT, "nova.py"), "-i", str(test_clip), "-n", "gl", "--opengl",
           "-l", "DEBUG"]
    with open(log, "w") as f:
        p = subprocess.Popen(cmd, cwd=tmp_path, env=env, stdout=f, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and "First frame" not in log.read_text(errors="replace"):
                assert p.poll() is None, f"nova.py ended with {p.returncode}:\n" + log.read_text(errors="replace")
                time.sleep(0.2)
            time.sleep(1.5)                      # a second of drawing
            assert p.poll() is None, f"nova.py ended with {p.returncode}:\n" + log.read_text(errors="replace")
        finally:
            if p.poll() is None:
                p.send_signal(signal.SIGINT)
                try:
                    p.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    p.kill()
    return log.read_text(errors="replace")


def test_an_opengl_window_draws_on_opengl_3_1(mesa_3_1, test_clip, tmp_path):
    """Measured on a Raspberry Pi 5 (V3D, OpenGL 3.1): a 1080p camera at 10 fps took 19 % of a core in the
    window without --opengl, where the CPU converts and scales every picture, and 9 % with it."""
    said = _a_window(mesa_3_1, test_clip, tmp_path)
    assert "OpenGL (GPU) mode" in said and "initializeGL done. GL 3.1" in said and "First frame" in said, said
    assert "--opengl needs" not in said and "Standard (CPU) mode" not in said, said
    for bad in ("Shader Error", "initializeGL Error", "OpenGL could not be initialised", "Rendering failed",
                "[NovaGL] ❌", "Traceback"):
        assert bad not in said, said


def test_an_opengl_window_below_opengl_3_1_is_drawn_without_it(mesa_3_0, test_clip, tmp_path):
    said = _a_window(mesa_3_0, test_clip, tmp_path)
    assert "--opengl needs OpenGL 3.1, and the GPU's driver here offers OpenGL 3.0: this window is drawn without " \
           "--opengl" in said, said
    assert "Standard (CPU) mode" in said and "First frame" in said and "OpenGL could not be initialised" not in said
