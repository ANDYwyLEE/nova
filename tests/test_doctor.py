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
    monkeypatch.setattr(doctor, "windows_service_session", lambda: False)
    out = doctor.report(config=str(tmp_path / "none.ini"))
    assert "--opengl cannot work here" in out
    assert "OpenGL comes from a GPU driver" not in out and "rendered in software" not in out
    assert "session 0" not in out


def test_opengl_in_windows_session_0_is_not_blamed_on_the_driver(monkeypatch, tmp_path):
    """nova doctor over ssh (Windows session 0, no GPU) on a PC whose AMD driver works: Qt falls back to
    opengl32sw.dll there, and the report told the user to install the GPU driver (2026-11-11)."""
    line = ("windows: VMware, Inc. / Gallium 0.4 on llvmpipe (LLVM 3.6, 128 bits) / 3.0 Mesa 11.2.2 "
            "(context 3.0, Qt's software opengl32sw.dll), PyOpenGL cannot use it (GLError 1282)")
    monkeypatch.setattr(doctor, "gl_line", lambda python=None: line)
    monkeypatch.setattr(doctor, "windows_service_session", lambda: True)
    out = doctor.report(config=str(tmp_path / "none.ini"))
    assert "session 0" in out and "from the desktop" in out
    assert "--opengl cannot work here" not in out and "install the GPU driver" not in out


def test_no_opengl_context_says_windows_need_one(monkeypatch, tmp_path):
    """Both renderers are a QOpenGLWindow: without a context no window opens (MSYS2's Qt on a Windows machine
    without a GPU driver crashed at the first window), so "falls back to QPainter" was wrong."""
    monkeypatch.setattr(doctor, "gl_line", lambda python=None: "windows: no OpenGL context at all")
    out = doctor.report(config=str(tmp_path / "none.ini"))
    assert "Nova windows need one with or without --opengl" in out and "falls back to QPainter" not in out


def test_pyav_newer_than_the_minimum_is_fine(monkeypatch, tmp_path):
    """requirements.txt names the oldest PyAV Nova supports (19.0.2: protocol options reach avio_open2, PyAV
    PR #2437); a newer one is no reason to warn, an older one is."""
    import av
    assert doctor.av_minimum() == "19.0.2"
    assert doctor._release("19.0.2") == (19, 0, 2) and doctor._release("20.1.0rc1") == (20, 1, 0)
    for version, mark in (("19.0.2", "✓"), ("19.1.0", "✓"), ("20.0.0", "✓"), ("19.0.1", "⚠"), ("18.2.0", "⚠")):
        monkeypatch.setattr(av, "__version__", version)
        marks = [m for m, t in doctor.checks("", "", "", None, None, str(tmp_path / "none.ini")) if t.startswith(f"PyAV {version} ")]
        assert marks == [mark], version


def test_tls_from_the_system_is_not_listed_as_missing(monkeypatch):
    """The macOS wheel has neither gnutls nor openssl: FFmpeg uses the system's TLS there, which its configure line
    does not name. The report said "not in this build: … gnutls openssl", which reads as no https and no rtmps."""
    import av
    import av._core as core
    monkeypatch.setattr(core, "library_meta", {"libavcodec": {"configuration": "--enable-videotoolbox --enable-libx264 --disable-gnutls"}})
    monkeypatch.setattr(doctor, "_tls_works", lambda: True)
    line = dict(doctor.ffmpeg_lines())["FFmpeg"]
    assert "TLS from the system" in line and "gnutls" not in line.replace("--disable-gnutls", "") and "openssl" not in line
    monkeypatch.setattr(doctor, "_tls_works", lambda: False)
    line = dict(doctor.ffmpeg_lines())["FFmpeg"]
    assert "TLS from the system" not in line and "gnutls openssl" in line
    monkeypatch.setattr(core, "library_meta", {"libavcodec": {"configuration": "--enable-gnutls --enable-libx264"}})
    monkeypatch.setattr(doctor, "_tls_works", lambda: 1 / 0)          # a TLS library is named: FFmpeg is not asked
    line = dict(doctor.ffmpeg_lines())["FFmpeg"]
    assert "TLS from the system" not in line and "built with: libx264 gnutls" in line
    monkeypatch.undo()

    def refused(*a, **k):
        raise ConnectionRefusedError(111, "Connection refused")

    def no_protocol(*a, **k):
        raise av.error.ProtocolNotFoundError(1, "Protocol not found")
    monkeypatch.setattr(av, "open", refused)
    assert doctor._tls_works() is True
    monkeypatch.setattr(av, "open", no_protocol)
    assert doctor._tls_works() is False
