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


def _drm_tree(root, cards):
    """A /sys/class/drm look-alike: cards = [(name, vendor, device, driver, render node)]."""
    for name, vendor, device, driver, node in cards:
        dev = root / name / "device"
        (dev / "drm" / node).mkdir(parents=True)
        (dev / "vendor").write_text(vendor + "\n")
        (dev / "device").write_text(device + "\n")
        (root / "drivers" / driver).mkdir(parents=True, exist_ok=True)
        os.symlink(root / "drivers" / driver, dev / "driver")
    (root / "card1-HDMI-A-1").mkdir()                     # a connector, not a GPU


def test_the_integrated_gpu_next_to_nvidia_cards_is_listed(tmp_path):
    _drm_tree(tmp_path, [("card0", "0x10de", "0x2684", "nvidia", "renderD130"),
                         ("card1", "0x8086", "0xa780", "i915", "renderD128"),
                         ("card2", "0x10de", "0x2684", "nvidia", "renderD129")])
    assert doctor.drm_gpus(str(tmp_path)) == [("NVIDIA", "10de:2684", "nvidia", "renderD130"),
                                              ("Intel", "8086:a780", "i915", "renderD128"),
                                              ("NVIDIA", "10de:2684", "nvidia", "renderD129")]
    assert doctor.drm_gpus(str(tmp_path / "nothing")) == []


def test_quick_sync_check_says_what_is_missing(monkeypatch):
    monkeypatch.setattr(doctor, "LINUX", True)
    intel, nvidia = ("Intel", "8086:a780", "i915", "renderD128"), ("NVIDIA", "10de:2684", "nvidia", "renderD129")
    err = "[Errno 1313558101] Unknown error occurred; last error log: [h264_qsv] Error creating a MFX session: -9."
    check = doctor.quick_sync_check
    monkeypatch.setattr(doctor.shutil, "which", lambda tool: "/usr/bin/" + tool)       # a system with apt
    assert check(set(), [nvidia], err) is None                        # no Intel GPU, not asked for: no line
    assert check({"cuda"}, [nvidia, intel], None)[0] == "✓"
    mark, text = check({"cuda"}, [nvidia, intel], err)                # an unused Intel GPU: good to know
    assert mark == "ℹ" and "intel-media-va-driver-non-free libmfx-gen1.2" in text and "MFX session: -9" in text
    mark, text = check({"qsv"}, [intel], err)                         # asked for and not working: worth fixing
    assert mark == "⚠" and "software decoding" in text and "apt install" in text
    monkeypatch.setattr(doctor.shutil, "which", lambda tool: None)    # no package manager Nova knows
    assert "apt" not in check({"qsv"}, [intel], err)[1] and "Quick Sync runtime" in check({"qsv"}, [intel], err)[1]
    for tool, word in (("dnf", "RPM Fusion"), ("zypper", "libmfx-gen1_2"), ("pacman", "vpl-gpu-rt")):
        monkeypatch.setattr(doctor.shutil, "which", lambda t, tool=tool: "/usr/bin/" + t if t == tool else None)
        assert word in check({"qsv"}, [intel], err)[1] and "apt install" not in check({"qsv"}, [intel], err)[1]
    monkeypatch.setattr(doctor.shutil, "which", lambda tool: "/usr/bin/" + tool)
    assert check({"qsv"}, [], "")[0] == "⚠" and check(set(), [intel], "") is None      # a build without h264_qsv
    monkeypatch.setattr(doctor, "LINUX", False)
    assert "Intel graphics driver" in check({"qsv"}, [], err)[1]


def test_vaapi_check_names_the_gpu_without_vaapi():
    """--gpu N is /dev/dri/renderD(128+N) for VAAPI. With NOVA_GPU_DEVICES = 0,1 on a machine whose second
    node is an NVIDIA card, every other camera decoded in software and only its own log said so."""
    gpus = [("NVIDIA", "10de:2684", "nvidia", "renderD130"), ("Intel", "8086:a780", "i915", "renderD128"),
            ("NVIDIA", "10de:2684", "nvidia", "renderD129")]
    probe = lambda node: None if node.endswith("renderD128") else "OSError: Input/output error; last error log: Failed to initialise VAAPI connection: -1"
    lines = doctor.vaapi_check({0, 1}, gpus, probe)
    assert [m for m, _ in lines] == ["✓", "⚠"]
    assert "renderD128 (Intel 8086:a780, --gpu 0)" in lines[0][1]
    assert "GPU 1 is /dev/dri/renderD129 (NVIDIA 10de:2684)" in lines[1][1] and "Failed to initialise VAAPI" in lines[1][1]
    assert "VAAPI answers on renderD128 (--gpu 0)" in lines[1][1]
    only_bad = doctor.vaapi_check({1}, gpus, probe)                    # nothing configured works: say where it would
    assert [m for m, _ in only_bad] == ["⚠"] and "VAAPI answers on renderD128 (--gpu 0)" in only_bad[0][1]
    assert doctor.vaapi_check({0}, gpus, probe)[0][0] == "✓" and len(doctor.vaapi_check({0}, gpus, probe)) == 1
    assert doctor.vaapi_check({0}, gpus, lambda node: "") == []        # a build without VAAPI
    # no --gpu anywhere: the node Nova looks for itself (here an AMD GPU behind an NVIDIA card)
    gpus = [("NVIDIA", "10de:2882", "nvidia", "renderD128"), ("AMD", "1002:13c0", "amdgpu", "renderD129")]
    probe = lambda node: None if node.endswith("renderD129") else "OSError: Failed to initialise VAAPI connection"
    lines = doctor.vaapi_check({None}, gpus, probe, auto="renderD129")
    assert [m for m, _ in lines] == ["✓"] and "renderD129 (AMD 1002:13c0, found without --gpu)" in lines[0][1]
    lines = doctor.vaapi_check({None, 0}, gpus, probe, auto="renderD129")
    assert [m for m, _ in lines] == ["✓", "⚠"] and "GPU 0 is /dev/dri/renderD128 (NVIDIA 10de:2882)" in lines[1][1]
    assert "VAAPI answers on renderD129 (found without --gpu)" in lines[1][1]
    lines = doctor.vaapi_check({None}, gpus, lambda node: "the node is closed to this user", auto="renderD129")
    assert lines[0][0] == "⚠" and "without --gpu the node is /dev/dri/renderD129" in lines[0][1]


def test_the_gpu_indexes_of_a_configuration():
    from types import SimpleNamespace as C
    cams = [C(opts="--gpu 2", enabled=True), C(opts="--gpu 5", enabled=False), C(opts="-r", enabled=True)]
    assert doctor.config_gpus({"opts": "--opengl --accel vaapi"}, {"NOVA_GPU_DEVICES": "0,1"}, cams) == {0, 1, 2}
    assert doctor.config_gpus({"opts": "--accel vaapi --gpu=3"}, {}, []) == {3}
    assert doctor.config_gpus({}, {}, []) == {None}                    # nothing named: VAAPI looks, cuda takes 0
    assert doctor.config_gpus({"opts": "--accel vaapi"}, {}, cams) == {None, 2}


def test_a_render_node_closed_to_the_user_is_said(monkeypatch):
    """Over ssh on a machine without a desktop login the user had no access to /dev/dri/renderD129 (group
    render): --accel vaapi answered "No VA display found" and decoded in software, doctor said nothing."""
    gpus = [("NVIDIA", "10de:2882", "nvidia", "renderD128"), ("AMD", "1002:13c0", "amdgpu", "renderD129"),
            ("Intel", "8086:a780", "i915", "no render node")]
    assert doctor.node_access(gpus, can=lambda path, mode: True) == []
    blocked = doctor.node_access(gpus, can=lambda path, mode: False)
    assert [(b[0], b[2]) for b in blocked] == [("AMD", "renderD129")]         # NVIDIA does not go through the node
    blocked = [("AMD", "1002:13c0", "renderD129", "render")]
    (mark, text), = doctor.node_access_check(blocked, {"vaapi"}, user="cam")
    assert mark == "⚠" and "/dev/dri/renderD129 (AMD 1002:13c0) is closed to cam" in text
    assert "sudo usermod -aG render cam" in text
    assert doctor.node_access_check(blocked, {"cuda"}, user="cam")[0][0] == "ℹ"
    assert doctor.node_access_check([], {"vaapi"}) == []


def test_an_nvidia_index_beyond_the_cards_is_said():
    check = doctor.nvidia_index_check
    (mark, text), = check({0, 1}, 1, {"cuda"})
    assert mark == "⚠" and "name GPU 1, but this machine has 1 NVIDIA card (0)" in text and "GPU 0" in text
    assert "2 NVIDIA cards (0-1)" in check({0, 3}, 2, {"cuda"})[0][1]
    assert check({0, 1}, 2, {"cuda"}) == [] and check({None, 0}, 1, {"cuda"}) == []
    assert check({1}, 1, {"vaapi"}) == [] and check({1}, 0, {"cuda"}) == []    # render nodes; no NVIDIA card at all


def test_qt_modules_that_do_not_load_are_named():
    """openSUSE's minimal system has no libgssapi_krb5: QtNetwork did not load, and `nova start` started
    cameras that died at once; nova.py by hand ran fine, so nothing pointed at the library."""
    assert doctor.qt_module_errors(("QtCore",)) == []
    (m, why), = doctor.qt_module_errors(("QtNoSuchModule",))
    assert m == "QtNoSuchModule" and "QtNoSuchModule" in why
    (mark, text), = doctor.qt_modules_check([("QtNetwork", "libgssapi_krb5.so.2: cannot open shared object file")])
    assert mark == "⚠" and "PySide6.QtNetwork does not load (libgssapi_krb5.so.2" in text and "nova start" in text
    assert doctor.qt_modules_check([("QtGui", "No module named 'PySide6'")]) == []     # the Qt line says that
    assert doctor.qt_modules_check([]) == []
