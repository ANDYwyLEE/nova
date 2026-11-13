"""An --opengl window where the GPU's driver stops below OpenGL 3.3 (a Raspberry Pi 5: 3.1; Intel graphics of
2011 and before).  The shaders are '#version 330 core': the window closed at start, or the process ended
with a segmentation fault at the first OpenGL call, and `nova doctor` said the OpenGL was fine."""
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
    """None where the 3.3 context is there, and where no context can be made at all (the offscreen platform
    of a runner): never an exception before the window."""
    from novagl import opengl_offered
    have = opengl_offered()
    assert have is None or have.startswith("OpenGL "), have


@pytest.fixture
def mesa_3_1(tmp_path):
    """The environment of an X display whose OpenGL is Mesa's software renderer held at 3.1."""
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
                   MESA_GL_VERSION_OVERRIDE="3.1", MESA_GLSL_VERSION_OVERRIDE="140")
        probe = subprocess.run([sys.executable, os.path.join(ROOT, "doctor.py"), "--gl"], env=env, cwd=ROOT,
                               capture_output=True, text=True, timeout=60)
        if "(context 3.1)" not in probe.stdout:
            pytest.skip(f"no Mesa OpenGL on an X display here: {(probe.stdout or probe.stderr).strip()[-200:]}")
        yield env
    finally:
        x.terminate()
        x.wait(timeout=10)


def test_an_opengl_window_on_opengl_3_1_is_drawn_without_it(mesa_3_1, test_clip, tmp_path):
    log = tmp_path / "nova.log"
    cmd = [sys.executable, "-u", os.path.join(ROOT, "nova.py"), "-i", str(test_clip), "-n", "gl31", "--opengl",
           "-l", "INFO"]
    with open(log, "w") as f:
        p = subprocess.Popen(cmd, cwd=tmp_path, env=mesa_3_1, stdout=f, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and "First frame" not in log.read_text(errors="replace"):
                assert p.poll() is None, f"nova.py ended with {p.returncode}:\n" + log.read_text(errors="replace")
                time.sleep(0.2)
            time.sleep(1.0)                      # a second of drawing
            assert p.poll() is None, f"nova.py ended with {p.returncode}:\n" + log.read_text(errors="replace")
        finally:
            if p.poll() is None:
                p.send_signal(signal.SIGINT)
                try:
                    p.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    p.kill()
    said = log.read_text(errors="replace")
    assert "--opengl needs OpenGL 3.3, and the GPU's driver here offers OpenGL 3.1: this window is drawn without " \
           "--opengl" in said, said
    assert "Standard (CPU) mode" in said and "First frame" in said and "OpenGL could not be initialised" not in said
