"""`nova doctor`: a machine summary for bug reports, with every secret masked."""
import os
import subprocess
import sys

import pytest

import cams
import doctor
from util import find_secrets

INI = """\
[defaults]
opts = -t rtmp://a.rtmp.youtube.com/live2/abcd-efgh-ijkl-mnop --accel cuda

[env]
NOVA_TIMEOUT = 5000000
SECRET_TOKEN = hunter2

[front]
url = rtsp://admin:S3cretA@10.0.0.1:554/s
opts = -r

[yard]
url = rtsp://10.0.0.2:554/user=x&password=S3cretB&channel=1
enabled = no
"""


@pytest.fixture
def ini(tmp_path):
    p = tmp_path / "cameras.ini"
    p.write_text(INI)
    if os.name != "nt":
        os.chmod(p, 0o600)
    return str(p)


def test_report_masks_every_secret(ini):
    out = doctor.report(config=ini, gl=False)
    for s in ("S3cretA", "S3cretB", "abcd-efgh-ijkl-mnop", "hunter2"):
        assert s not in out
    assert all(s == "***" for s in find_secrets(out))      # the finder matches the mask itself
    for label in ("Nova", "Python", "OS", "Qt", "PyAV", "FFmpeg", "HW devices", "Audio", "Config", "Settings",
                  "Storage", "Checks"):
        assert label in out, label
    assert "2 cameras (1 enabled)" in out
    assert "[front] -r" in out                               # per-camera options, masked
    assert "live2/***" in out                                # the stream key in [defaults]
    assert "NOVA_TIMEOUT=5000000" in out and "SECRET_TOKEN=" in out


def test_report_without_cameras_ini(tmp_path):
    out = doctor.report(config=str(tmp_path / "none.ini"), gl=False)
    assert "not found" in out and "Checks" in out


def test_gl_probe_runs_in_a_child():
    """The OpenGL probe is a separate process: it must exit 0 and say something on any platform plugin."""
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    p = subprocess.run([sys.executable, doctor.__file__, "--gl"], capture_output=True, text=True, timeout=120, env=env)
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip()


def test_launcher_verb(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cams, "INI", str(tmp_path / "cameras.ini"))
    monkeypatch.setattr(doctor, "gl_line", lambda python=None: "n/a (test)")
    assert (cams.main(["doctor"]) or 0) == 0
    out = capsys.readouterr().out
    assert "Nova doctor" in out and "not found" in out and "n/a (test)" in out


def test_xmeye_cameras_need_python_dvr(tmp_path, monkeypatch):
    p = tmp_path / "cameras.ini"
    p.write_text(INI + "\n[gate]\nurl = xmeye://admin:S3cretC@10.0.0.3\n")
    if os.name != "nt":
        os.chmod(p, 0o600)
    monkeypatch.setattr(doctor, "_can_import", lambda python, name: False)
    out = doctor.report(config=str(p), gl=False)
    assert "⚠ 1 xmeye:// camera(s) but python-dvr is not installed" in out and "S3cretC" not in out
    monkeypatch.setattr(doctor, "_can_import", lambda python, name: True)
    assert "✓ python-dvr is installed for the 1 xmeye:// camera(s)" in doctor.report(config=str(p), gl=False)


def test_opengl_pyopengl_cannot_reach(monkeypatch, tmp_path):
    """A Windows machine without a GPU driver (a CI runner, a VM): Qt falls back to its opengl32sw.dll and
    PyOpenGL, on the system opengl32.dll, has no context. The report said "OpenGL )" (the last line of a
    multi-line GLError) and "✓ OpenGL comes from a GPU driver", and every --opengl window closed at start."""
    line = ("windows: VMware, Inc. / Gallium 0.4 on llvmpipe (LLVM 3.6, 128 bits) / 3.0 Mesa 11.2.2 "
            "(context 3.0, Qt's software opengl32sw.dll), PyOpenGL cannot use it (GLError 1282)")
    monkeypatch.setattr(doctor, "gl_line", lambda python=None: line)
    out = doctor.report(config=str(tmp_path / "none.ini"))
    assert "--opengl cannot work here" in out
    assert "OpenGL comes from a GPU driver" not in out and "rendered in software" not in out


def test_no_opengl_context_says_windows_need_one(monkeypatch, tmp_path):
    """Both renderers are a QOpenGLWindow: without a context no window opens (MSYS2's Qt on a Windows machine
    without a GPU driver crashed at the first window), so "falls back to QPainter" was wrong."""
    monkeypatch.setattr(doctor, "gl_line", lambda python=None: "windows: no OpenGL context at all")
    out = doctor.report(config=str(tmp_path / "none.ini"))
    assert "Nova windows need one with or without --opengl" in out and "falls back to QPainter" not in out
