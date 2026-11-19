"""An --opengl window where the GPU's driver stops below OpenGL 3.3.  The shaders are '#version 330 core':
the window closed at start, or the process ended with a segmentation fault at the first OpenGL call, and
`nova doctor` said the OpenGL was fine.  At OpenGL 3.1 (a Raspberry Pi 4 and 5) the window draws with the
same shaders in GLSL 1.40, and from OpenGL 2.1 on (a Raspberry Pi 3, Intel graphics of 2010) in GLSL 1.20
with luminance textures; below that the camera is shown in the window that needs no shader."""
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


def test_the_shaders_in_glsl_1_20_are_the_same_shaders():
    """OpenGL 2.1 (a Raspberry Pi 3's GPU): no `in`/`out`, no texture(), no output variable, and no
    two-channel texture, so NV12's chroma is read from luminance and alpha."""
    import novagl
    vertex, fragment = novagl.glsl_120(novagl.VERTEX_SHADER), novagl.glsl_120(novagl.FRAGMENT_SHADER)
    for older in (vertex, fragment):
        assert older.strip().startswith("#version 120") and "layout" not in older
        assert not [line for line in older.splitlines() if line.startswith(("in ", "out "))]
    assert all(f"attribute vec{n} {name};" in vertex for n, name in zip((3, 2), novagl.ATTRIBUTES))
    assert "varying vec2 TexCoord;" in vertex and "varying vec2 TexCoord;" in fragment
    assert "texture(" not in fragment and fragment.count("texture2D(") == novagl.FRAGMENT_SHADER.count("texture(")
    assert "out vec4" not in fragment and fragment.count("gl_FragColor") == 2 and " FragColor" not in fragment
    assert "texture2D(tex1, TexCoord).ra" in fragment and ".rg" not in fragment
    same = lambda text: text.split("\n", 2)[2].replace(" ", "")
    assert same(fragment).replace("gl_FragColor", "FragColor").replace("texture2D(", "texture(").replace(
        ".ra;", ".rg;").replace("varying", "in") == same(novagl.glsl_140(novagl.FRAGMENT_SHADER)).replace(
        "outvec4FragColor;\n", "")


def _mesa(tmp_path, version, glsl):
    """The environment of an X display whose OpenGL is Mesa's software renderer held at `version`."""
    if not LINUX or not shutil.which("Xvfb"):
        pytest.skip("needs Linux with Xvfb")
    # -noreset: an X server starts anew when its last client leaves, and a client that connects in that moment
    # waits for an answer that never comes (openSUSE Leap 16.0: the second program of a test, every time)
    x = subprocess.Popen(["Xvfb", "-displayfd", "1", "-screen", "0", "640x480x24", "-nolisten", "tcp", "-noreset"],
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


@pytest.fixture
def mesa_2_1(tmp_path):
    yield from _mesa(tmp_path, "2.1", "120")


@pytest.fixture
def mesa_2_0(tmp_path):
    yield from _mesa(tmp_path, "2.0", "110")


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


def _draws(said, version):
    assert "OpenGL (GPU) mode" in said and f"initializeGL done. GL {version}" in said and "First frame" in said, said
    assert "--opengl needs" not in said and "Standard (CPU) mode" not in said, said
    for bad in ("Shader Error", "initializeGL Error", "OpenGL could not be initialised", "Rendering failed",
                "Render Error", "[NovaGL] ❌", "Traceback"):
        assert bad not in said, said


def test_an_opengl_window_draws_on_opengl_2_1(mesa_2_1, test_clip, tmp_path):
    """A Raspberry Pi 3 B+ (VC4, OpenGL 2.1): a 1080p camera at 10 fps took 68 % of a core in the window
    without --opengl, and --opengl was answered with that window."""
    _draws(_a_window(mesa_2_1, test_clip, tmp_path), "2.1")


def test_an_opengl_window_draws_on_opengl_3_0(mesa_3_0, test_clip, tmp_path):
    """OpenGL 3.0 has GLSL 1.30, which reads the shaders as GLSL 1.40 does."""
    _draws(_a_window(mesa_3_0, test_clip, tmp_path), "3.0")


def test_an_opengl_window_below_opengl_2_1_is_drawn_without_it(mesa_2_0, test_clip, tmp_path):
    said = _a_window(mesa_2_0, test_clip, tmp_path)
    assert "--opengl needs OpenGL 2.1, and the GPU's driver here offers OpenGL 2.0: this window is drawn without " \
           "--opengl" in said, said
    assert "Standard (CPU) mode" in said and "First frame" in said and "OpenGL could not be initialised" not in said


def _colours(env, **more):
    """What tests/gl_pixels.py reads back from a NovaGL window that was given pictures of (200, 60, 30)."""
    out = subprocess.run([sys.executable, os.path.join(ROOT, "tests", "gl_pixels.py")], env=dict(env, **more), cwd=ROOT,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr
    lines = dict(line.split(" ", 1) for line in out.stdout.strip().splitlines())
    for fmt in ("yuv420p", "nv12"):
        r, g, b = map(int, lines[fmt].split())
        assert abs(r - 200) <= 3 and abs(g - 60) <= 3 and abs(b - 30) <= 3, out.stdout
    return lines["GL"]


def test_the_colours_are_right_in_glsl_1_40_and_1_30(mesa_3_1, mesa_3_0):
    assert "legacy=False" in _colours(mesa_3_1) and "legacy=False" in _colours(mesa_3_0)
    assert "alpha=8" in _colours(mesa_3_1)                     # the window as it has always been
    assert "alpha=0" in _colours(mesa_3_1, WLR_RENDERER="pixman")     # ... unless the screen is composited on the CPU


def test_the_colours_are_right_on_opengl_2_1(mesa_2_1):
    """Luminance textures for the planes, and luminance-alpha for NV12's two chroma bytes: a wrong channel
    there is a picture in the wrong colours, with nothing in the log."""
    said = _colours(mesa_2_1)
    assert "legacy=True vaos=True" in said
    # a GPU that stops at OpenGL 2.x: the window asks for no alpha channel (util.opaque_windows)
    assert "alpha=0" in said
    assert "legacy=True vaos=False" in _colours(mesa_2_1, GL_PIXELS_NO_VAO="1")       # and without vertex array objects
