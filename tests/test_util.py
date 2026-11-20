import os

import pytest

import util
from util import mask_url, display_url


@pytest.mark.parametrize("raw, masked", [
    ("rtsp://admin:s3cret@192.168.1.10:554/stream_0", "rtsp://admin:***@192.168.1.10:554/stream_0"),
    ("rtsp://192.168.1.24:554/user=andy&password=abc&channel=1", "rtsp://192.168.1.24:554/user=andy&password=***&channel=1"),
    # whole log lines, as the logging filter sees them
    ("open failed: 'rtsp://u:pw@h/x'; retry", "open failed: 'rtsp://u:***@h/x'; retry"),
    # stream keys
    ("rtmp://a.rtmp.youtube.com/live2/abcd-efgh-ijkl", "rtmp://a.rtmp.youtube.com/live2/***"),
    ("rtmp://b.rtmp.youtube.com/live2?backup=1/abcd-efgh", "rtmp://b.rtmp.youtube.com/live2?backup=1/***"),
    ("https://a.upload.youtube.com/http_upload_hls?cid=abcd-efgh&copy=0&file=temp.m3u8",
     "https://a.upload.youtube.com/http_upload_hls?cid=***&copy=0&file=temp.m3u8"),
    ("rtmp://live.twitch.tv/app/live_123_abc", "rtmp://live.twitch.tv/app/***"),
    # an '@' inside the password: FFmpeg splits at the last '@', so must the mask
    ("rtsp://admin:Admin@123@h:554/x", "rtsp://admin:***@h:554/x"),
    ("rtsp://me@mail.com:pw@h/x", "rtsp://me@mail.com:***@h/x"),
    ("rtsp://:pw@h/", "rtsp://:***@h/"),
    ("rtsp://admin:pa/ss@h:554/x", "rtsp://admin:***@h:554/x"),          # '/' in the password, unencoded
    ("rtsp://admin:a/b@c@h/x", "rtsp://admin:***@h/x"),
    ("http://h:8080/snap?email=a@b.c", "http://h:***@b.c"),              # over-masks, never under-masks
    # XM paths separate with '_', other key names, SRT secrets
    ("rtsp://h/user=admin_password=Xy7_channel=1_stream=0.sdp", "rtsp://h/user=admin_password=***_channel=1_stream=0.sdp"),
    ("rtsp://h/user=a&psw=abc&ch=1", "rtsp://h/user=a&psw=***&ch=1"),
    ("srt://h:9000?streamid=#!::r=live/K,m=publish&passphrase=longsecret1&latency=200",
     "srt://h:9000?streamid=***&passphrase=***&latency=200"),
    # dict / argv forms in debug lines (--ffmpeg "-passphrase x")
    ("{'password': 'x1', 'b': 1}", "{'password': '***', 'b': 1}"),
    ("['-passphrase', 'x2', '-pbkeylen', '32']", "['-passphrase', '***', '-pbkeylen', '32']"),
    ('--ffmpeg "-passphrase x3 -pbkeylen 32"', '--ffmpeg "-passphrase *** -pbkeylen 32"'),
    # any rtmp(s) stream key: the last path element after the app
    ("rtmps://live-api-s.facebook.com:443/rtmp/FB-123-0-abc", "rtmps://live-api-s.facebook.com:443/rtmp/***"),
    ("rtmp://live.restream.io/live/re_123?x=1", "rtmp://live.restream.io/live/***?x=1"),
    ("rtsp://u:p@h/x and rtsp://v:q@k/y", "rtsp://u:***@h/x and rtsp://v:***@k/y"),
    # nothing to hide
    ("rtmp://h/app", "rtmp://h/app"),
    ("rtsp://user@h:554/x", "rtsp://user@h:554/x"),
    ("compass=1 bypass=2", "compass=1 bypass=2"),
    ("rtsp://192.168.1.10:554/stream_0", "rtsp://192.168.1.10:554/stream_0"),
    ("http://h/app/other", "http://h/app/other"),
    ("0", "0"),
    ("", ""),
])
def test_mask_url(raw, masked):
    assert mask_url(raw) == masked


def test_mask_url_none():
    assert mask_url(None) == ""


def test_display_url_drops_credentials():
    assert display_url("rtsp://u:pw@h:554/x") == "h:554/x"
    assert display_url("0") == "camera 0"


def test_window_platform_is_novas_choice_on_a_wayland_session(monkeypatch):
    """startxfce4 --wayland exports QT_QPA_PLATFORM=wayland for every application: Nova's windows opened native
    and labwc stacked them top left. On a Wayland session with XWayland the platform is XWayland unless
    NOVA_QT_PLATFORM (cameras.ini [env] QT_QPA_PLATFORM, through the launcher) asks for another."""
    import util
    monkeypatch.setattr(util, "LINUX", True)
    wl = {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}
    assert util.window_platform(wl)[0] == "xcb"
    p, note = util.window_platform(dict(wl, QT_QPA_PLATFORM="wayland"))
    assert p == "xcb" and "QT_QPA_PLATFORM=wayland is for other applications" in note
    assert util.window_platform(dict(wl, QT_QPA_PLATFORM="wayland", NOVA_QT_PLATFORM="wayland"))[0] == "wayland"
    assert util.window_platform({"WAYLAND_DISPLAY": "wayland-0", "QT_QPA_PLATFORM": "wayland"}) == (None, None)  # no XWayland
    assert util.window_platform({"DISPLAY": ":0", "QT_QPA_PLATFORM": "wayland"}) == (None, None)       # X11 session
    monkeypatch.setattr(util, "LINUX", False)
    assert util.window_platform(wl) == (None, None)


def test_utf8_stdio_into_a_file_or_pipe():
    """Windows writes a redirected stdout in the ANSI code page (cp1252, cp949): under `nova start`, whose
    stdout is logs/ID.log, every log line with an emoji was lost behind a "--- Logging error ---"
    traceback. PYTHONIOENCODING=cp1252 stands in for that code page on every platform."""
    import os
    import subprocess
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = ("import logging, sys, util; util.utf8_stdio(); "
            "logging.basicConfig(stream=sys.stdout, format='%(message)s'); logging.warning('\\u26a1 \\U0001f680 ok')")
    env = dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONPATH=root)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, cwd=root, timeout=60)
    assert b"Logging error" not in r.stderr, r.stderr
    assert r.stdout.decode("utf-8").strip() == "⚡ \U0001f680 ok"


def _render_nodes(root, nodes):
    for name, vendor in nodes:
        (root / name / "device").mkdir(parents=True)
        (root / name / "device" / "vendor").write_text(vendor + "\n")


def test_vaapi_node_is_the_first_one_that_is_not_an_nvidia_card(tmp_path):
    """--accel vaapi without --gpu opened renderD128 whatever was there: on a machine whose first node is
    an NVIDIA card (an RTX 4060 before the Ryzen's own GPU) that meant software decoding until someone
    found --gpu 1."""
    import util
    _render_nodes(tmp_path, [("renderD128", "0x10de"), ("renderD129", "0x1002"), ("renderD130", "0x8086")])
    (tmp_path / "card0").mkdir(); (tmp_path / "renderD131").mkdir()        # no vendor file: skipped
    assert util.vaapi_node(None, str(tmp_path)) == "/dev/dri/renderD129"
    assert util.vaapi_node(0, str(tmp_path)) == "/dev/dri/renderD128"       # an index given is taken at its word
    assert util.vaapi_node("2", str(tmp_path)) == "/dev/dri/renderD130"
    assert util.vaapi_node("x", str(tmp_path)) == "/dev/dri/renderD129"     # not a number: as if not given
    only_nvidia = tmp_path / "n"
    _render_nodes(only_nvidia, [("renderD128", "0x10de")])
    assert util.vaapi_node(None, str(only_nvidia)) == "/dev/dri/renderD128"
    assert util.vaapi_node(None, str(tmp_path / "nothing")) == "/dev/dri/renderD128"


def _pci(root, slot, cls, vendor, device, driver=None):
    d = root / slot
    d.mkdir(parents=True)
    (d / "class").write_text(cls + "\n")
    (d / "vendor").write_text(vendor + "\n")
    (d / "device").write_text(device + "\n")
    if driver:
        (root.parent / "drivers" / driver).mkdir(parents=True, exist_ok=True)
        os.symlink(root.parent / "drivers" / driver, d / "driver")


@pytest.mark.skipif(os.name == "nt", reason="sysfs names have colons, and symlinks: Linux's")
def test_the_display_controllers_on_the_pci_bus(tmp_path):
    root = tmp_path / "devices"
    _pci(root, "0000:00:02.0", "0x030000", "0x8086", "0xa780", "i915")
    _pci(root, "0000:01:00.0", "0x030000", "0x10de", "0x0a64", "nouveau")
    _pci(root, "0000:02:00.0", "0x030200", "0x10de", "0x2684")              # a card no driver took
    _pci(root, "0000:03:00.0", "0x020000", "0x10ec", "0x8168", "r8169")     # the network card is not one
    assert util.pci_gpus(str(root)) == [("Intel", "8086:a780", "i915"), ("NVIDIA", "10de:0a64", "nouveau"),
                                        ("NVIDIA", "10de:2684", "no driver")]
    assert util.pci_gpus(str(tmp_path / "nothing")) == []


def test_what_stands_between_an_accel_and_the_machine():
    """An old NVIDIA card on nouveau: `--accel cuda` was answered with `Operation not permitted` and tried
    again every few minutes; `--accel qsv` on a machine without an Intel GPU failed thirty packets first."""
    nouveau = [("NVIDIA", "10de:0a64", "nouveau")]
    assert "nouveau driver" in util.accel_obstacle("cuda", nouveau) and "NVIDIA's own driver" in util.accel_obstacle("nvdec", nouveau)
    assert util.accel_obstacle("qsv", nouveau) == "this machine has no Intel GPU, and Quick Sync is Intel's"
    assert util.accel_obstacle("vaapi", nouveau) is None and util.accel_obstacle("vdpau", nouveau) is None
    amd = [("AMD", "1002:1638", "amdgpu")]
    assert util.accel_obstacle("cuda", amd) == "this machine has no NVIDIA GPU"
    both = [("Intel", "8086:a780", "i915"), ("NVIDIA", "10de:2684", "nvidia"), ("NVIDIA", "10de:0a64", "nouveau")]
    assert util.accel_obstacle("cuda", both) is None and util.accel_obstacle("qsv", both) is None
    # a card no driver took may still be NVIDIA's own driver's (it binds late): not ours to rule out
    assert util.accel_obstacle("cuda", [("NVIDIA", "10de:2684", "no driver")]) is None
    assert util.accel_obstacle("cuda", []) is None and util.accel_obstacle("", amd) is None    # nothing to go by


def test_a_hardware_error_keeps_ffmpeg_s_own_last_line():
    cuvid = Exception("[Errno 1] Operation not permitted: 'avcodec_open2(\"h264_cuvid\", {})'; last error log: [h264_cuvid] Failed loading nvcuvid.")
    assert util.hw_err(cuvid) == ("Operation not permitted: Failed loading nvcuvid "
                                  "(NVIDIA's driver is not installed, or is too old for this FFmpeg)")
    cuda = Exception("[Errno 1] Operation not permitted; last error log: [CUDA] Could not dynamically load CUDA")
    assert util.hw_err(cuda).startswith("Operation not permitted: Could not dynamically load CUDA (NVIDIA's driver")
    v4l2 = Exception("[Errno 22] Invalid argument: 'avcodec_open2(\"h264_v4l2m2m\", {})' returned 22; last error log: [h264_v4l2m2m] can't configure decoder")
    assert util.hw_err(v4l2) == "Invalid argument: can't configure decoder"
    assert util.hw_err(Exception("[Errno 5] Input/output error")) == "Input/output error"
    assert util.hw_err(Exception("no decoder")) == "no decoder"


@pytest.mark.skipif(not util.LINUX, reason="GLX is Linux's")
def test_pyopengl_is_given_a_driver_s_own_libgl(tmp_path):
    """NVIDIA's legacy 340 driver brings a libGL.so.1 of its own, from before libglvnd; PyOpenGL took
    libglvnd's libGLX.so.0 beside it, and no window opened (`Could not initialize GLX`, `no valid context`)."""
    import subprocess, sys
    code = r"""
import builtins, io, sys
import util
glvnd = util.driver_own_libgl()                     # this machine's own libraries: says what it finds
import OpenGL.platform as P
real = builtins.open
builtins.open = lambda p, *a, **k: io.StringIO("no dispatch library here\n") if p == "/proc/self/maps" else real(p, *a, **k)
took = util.driver_own_libgl()                      # as if libGL.so.1 were a driver's own
print(glvnd, took, P.PLATFORM.GL is P.PLATFORM.GLX, P.GetCurrentContext is P.PLATFORM.GetCurrentContext,
      P.GetCurrentContext() is None)
"""
    env = dict(os.environ, PYOPENGL_PLATFORM="glx", PYTHONPATH=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
    if "libGL.so.1" in r.stderr and "cannot open" in r.stderr:
        pytest.skip("no libGL.so.1 on this machine")
    out = r.stdout.split()
    assert r.returncode == 0 and len(out) == 5, r.stderr[-800:]
    if out[1] == "False":
        pytest.skip("libGL.so.1 does not load here")
    # PyOpenGL now has one library for OpenGL and GLX, asks it for the context, and there is none yet
    assert out[1:] == ["True", "True", "True", "True"], r.stdout


ATOM_FLAGS = "processor\t: 0\nmodel name\t: Intel(R) Atom(TM) CPU D525   @ 1.80GHz\nflags\t\t: fpu sse sse2 ht pni ssse3 cx16 movbe lahf_lm\n"
CORE_FLAGS = "processor\t: 0\nflags\t\t: fpu sse sse2 pni ssse3 cx16 sse4_1 sse4_2 movbe popcnt avx avx2\n"


@pytest.mark.skipif(not (util.LINUX and __import__("platform").machine() in ("x86_64", "AMD64")), reason="x86 Linux reads /proc/cpuinfo")
def test_wheels_built_for_a_newer_cpu_are_found_without_importing_them(tmp_path):
    """On a CPU without SSE4.2, PySide6 from 6.10 on and numpy from 2.4 on end the process at import
    (seen on an Atom D525): `python nova.py --help`, `nova doctor` and `nova start` all died without a word."""
    atom, core = tmp_path / "atom", tmp_path / "core"
    atom.write_text(ATOM_FLAGS)
    core.write_text(CORE_FLAGS)
    assert not util.cpu_has_sse42(str(atom)) and util.cpu_has_sse42(str(core))
    assert util.cpu_has_sse42(str(tmp_path / "none"))                     # nothing known: nothing claimed
    pinned = {"PySide6": "6.12.0", "numpy": "2.5.4"}.get
    assert util.wheels_too_new_for_cpu(str(core), pinned) == []
    wheels = util.wheels_too_new_for_cpu(str(atom), pinned)
    assert wheels == [("PySide6", "6.12.0", "6.9.3"), ("numpy", "2.5.4", "2.3.5")]
    said = util.cpu_message(wheels)
    assert "this CPU has no SSE4.2, and PySide6 6.12.0 and numpy 2.5.4 are built for it" in said
    assert "-m pip install PySide6_Essentials==6.9.3 numpy==2.3.5" in said and "docs/install.md" in said
    # requirements.txt names PySide6_Essentials: that is the distribution an installation has
    essentials = {"PySide6_Essentials": "6.12.0", "numpy": "2.3.5"}.get
    assert util.wheels_too_new_for_cpu(str(atom), essentials) == [("PySide6", "6.12.0", "6.9.3")]
    assert util.wheels_too_new_for_cpu(str(atom), {"PySide6": "6.9.3", "numpy": "2.3.5"}.get) == []
    only = util.wheels_too_new_for_cpu(str(atom), {"PySide6": "6.9.3", "numpy": "2.4.0"}.get)
    assert only == [("numpy", "2.4.0", "2.3.5")] and "numpy 2.4.0 is built for it" in util.cpu_message(only)
    assert util.wheels_too_new_for_cpu(str(atom), lambda name: None) == [] and util.cpu_message([]) is None


def test_an_av_that_is_installed_and_does_not_load_is_not_told_to_pip_install():
    """After a release upgrade and `apt autoremove`, a self-built FFmpeg had lost libx264.so.160: every
    camera ended with `Error: av is required. Run 'pip install -r requirements.txt'`, which changes nothing."""
    missing = ModuleNotFoundError("No module named 'av'", name="av")
    assert util.av_import_help(missing) == "av (PyAV) is required. Run 'pip install -r requirements.txt'"
    lost = ImportError("libsrt-gnutls.so.1.4: cannot open shared object file: No such file or directory")
    said = util.av_import_help(lost)
    assert "is installed but cannot load libsrt-gnutls.so.1.4" in said and "distribution upgrade" in said
    assert "docs/custom-ffmpeg.md" in said and "pip install --force-reinstall av" in said and "requirements.txt" not in said
    dll = ImportError("DLL load failed while importing _core: The specified module could not be found.")
    assert "is installed but does not load (DLL load failed while importing _core" in util.av_import_help(dll)


POLICY = "DLL load failed while importing QtCore: An Application Control policy has blocked this file."


def test_a_module_that_windows_application_control_refused_is_called_that():
    """A fresh Windows 11 switched Smart App Control from evaluation to on nine days after its install: the
    MSYS2 install ended at its first import, and the advice was to build PyAV again."""
    assert util.policy_block(ImportError(POLICY)) and util.policy_block(POLICY)
    assert not util.policy_block(ImportError("DLL load failed while importing _core: The specified module could not be found."))
    assert not util.policy_block(ModuleNotFoundError("No module named 'av'", name="av"), local="")
    # a Windows in another language words error 4551 its own way
    assert util.policy_block("DLL load failed while importing QtCore: Eine Richtlinie hat diese Datei blockiert.",
                             local="Eine Richtlinie hat diese Datei blockiert.\r\n")
    said = util.av_import_help(ImportError(POLICY.replace("QtCore", "_core")))
    assert "is installed but does not load (DLL load failed while importing _core" in said
    assert "Smart App Control" in said and "Option A" in said and "build it again" not in said


def test_smart_app_control_is_read_from_the_registry():
    assert [util.smart_app_control(lambda v=v: v) for v in (0, 1, 2, 7)] == ["off", "on", "evaluation", None]

    def absent():
        raise FileNotFoundError("no such value")
    assert util.smart_app_control(absent) is None                  # Windows 10, a server
    if not util.WINDOWS:
        assert util.smart_app_control() is None


def test_a_camera_whose_qt_windows_refused_says_so_in_one_line(tmp_path):
    """`nova start` showed the last lines of the camera's log: a traceback ending in the policy sentence."""
    import subprocess
    import sys
    fake = tmp_path / "PySide6"
    fake.mkdir()
    (fake / "__init__.py").write_text("")
    (fake / "QtCore.py").write_text(f"raise ImportError({POLICY!r})\n")
    nova = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nova.py")
    r = subprocess.run([sys.executable, nova, "-i", "rtsp://192.0.2.1/x"], capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, PYTHONPATH=str(tmp_path)))
    assert r.returncode == 1 and "Traceback" not in r.stderr, r.stderr
    assert r.stderr.startswith("nova: DLL load failed while importing QtCore") and "docs/install.md, Option A" in r.stderr
    (fake / "QtCore.py").write_text("raise ImportError('libEGL.so.1: cannot open shared object file')\n")
    r = subprocess.run([sys.executable, nova, "-i", "rtsp://192.0.2.1/x"], capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, PYTHONPATH=str(tmp_path)))
    assert r.returncode == 1 and "Traceback" in r.stderr and "libEGL.so.1" in r.stderr      # anything else: as it was


def test_opengl_shortfall_names_a_context_that_is_too_old():
    """An --opengl window draws from OpenGL 2.1 on: a Raspberry Pi 4 and 5 offer 3.1 and get the shaders in
    GLSL 1.40, a Raspberry Pi 3 and Intel graphics of 2010 offer 2.1 and get them in GLSL 1.20."""
    assert util.opengl_shortfall(3, 3) is None and util.opengl_shortfall(4, 6) is None
    assert util.opengl_shortfall(3, 1) is None and util.opengl_shortfall(3, 2) is None
    assert util.opengl_shortfall(3, 0) is None and util.opengl_shortfall(2, 1) is None
    assert util.opengl_shortfall(2, 0) == "OpenGL 2.0" and util.opengl_shortfall(1, 4) == "OpenGL 1.4"
    assert util.opengl_shortfall(3, 2, es=True) == "OpenGL ES 3.2"          # ES of any version: other shaders
    assert util.opengl_needs("OpenGL 2.0") == "--opengl needs OpenGL 2.1, and the GPU's driver here offers OpenGL 2.0"


def test_windows_are_opaque_where_the_screen_is_composited_on_the_cpu():
    """A Raspberry Pi 3 B+: Raspberry Pi OS starts its compositor with WLR_RENDERER=pixman, and a window
    with an alpha channel cost it 97 % of a core for one camera at full screen (under 2 % without)."""
    assert util.opaque_windows(environ={"WLR_RENDERER": "pixman"}) and util.opaque_windows(environ={"WLR_RENDERER": " Pixman "})
    assert not util.opaque_windows(environ={}) and not util.opaque_windows(environ={"WLR_RENDERER": "gles2"})
    assert util.opaque_windows((2, 1), environ={}) and util.opaque_windows((2, 0), environ={})      # OpenGL 2.x GPUs
    assert not util.opaque_windows((3, 0), environ={}) and not util.opaque_windows((3, 3), environ={})
    assert util.opaque_windows((4, 6), environ={"WLR_RENDERER": "pixman"})


def test_cpu_idle_is_the_idle_time_of_every_core_together(tmp_path, monkeypatch):
    stat = tmp_path / "stat"
    stat.write_text("cpu  5000 10 2000 80000 300 0 40 0 0 0\ncpu0 1200 2 500 20000 80 0 10 0 0 0\n")
    monkeypatch.setattr(util, "LINUX", True)
    monkeypatch.setattr(util.os, "sysconf", lambda name: 100, raising=False)
    assert util.cpu_idle(str(stat)) == pytest.approx(803.0)            # idle and iowait, in seconds
    assert util.cpu_idle(str(tmp_path / "none")) is None
    stat.write_text("cpu\n")
    assert util.cpu_idle(str(stat)) is None
    monkeypatch.setattr(util, "LINUX", False)
    assert util.cpu_idle() is None


class _V4L2:
    """The V4L2 devices of a board, as the two questions util.v4l2_decoders asks of them answer."""
    M2M_MPLANE, CAPTURE_MPLANE, OUTPUT_MPLANE, DEVICE_CAPS = 0x4000, 0x1000, 0x2000, 0x80000000

    def __init__(self, monkeypatch, devices, denied=()):
        import fcntl
        self.devices, self.denied, self.fds = devices, denied, {}
        monkeypatch.setattr(util, "LINUX", True)
        monkeypatch.setattr(util.glob, "glob", lambda pattern: list(devices) + list(denied))
        monkeypatch.setattr(util.os, "open", self.open)
        monkeypatch.setattr(util.os, "close", lambda fd: self.fds.pop(fd))
        monkeypatch.setattr(fcntl, "ioctl", self.ioctl)

    def open(self, path, flags):
        if path in self.denied:
            raise PermissionError(13, "Permission denied", path)
        fd = 1000 + len(self.fds)
        self.fds[fd] = self.devices[path]
        return fd

    def ioctl(self, fd, request, buf):
        import struct
        caps, formats = self.fds[fd]
        if request == util._VIDIOC_QUERYCAP:
            struct.pack_into("II", buf, 84, caps | self.DEVICE_CAPS, caps)
            return 0
        index, queue = struct.unpack_from("II", buf, 0)
        names = formats.get(queue, [])
        if index >= len(names):
            raise OSError(22, "Invalid argument")
        buf[44:48] = names[index].encode()
        return 0


@pytest.mark.skipif(not hasattr(os, "O_NONBLOCK"), reason="V4L2 is Linux's")
def test_v4l2_decoders_are_the_devices_that_take_a_stream(monkeypatch):
    """--accel v4l2m2m on a Raspberry Pi 5 failed at every camera and was tried again every five minutes:
    the board has no H.264 decoder, and its HEVC decoder takes slices ('S265'), not the stream."""
    pytest.importorskip("fcntl")
    v = _V4L2
    pi5 = {"/dev/video19": (v.M2M_MPLANE, {10: ["S265"]}),
           "/dev/video20": (v.CAPTURE_MPLANE | v.OUTPUT_MPLANE, {10: ["YU12", "NV12"]})}
    fake = _V4L2(monkeypatch, pi5)
    assert util.v4l2_decoders() == [] and not fake.fds                 # every device closed again
    said = util.v4l2_obstacle([], "Raspberry Pi 5 Model B Rev 1.1")
    assert said.startswith("this machine has no V4L2 decoder that takes a camera's stream (a Raspberry Pi 5 has no H.264")
    assert util.v4l2_obstacle([], "") == "this machine has no V4L2 decoder that takes a camera's stream"
    monkeypatch.setattr(util, "board_model", lambda: "Raspberry Pi 5 Model B Rev 1.1")
    assert util.accel_obstacle("v4l2m2m", []) == said                  # no PCI GPU is no reason to say nothing
    pi4 = {"/dev/video10": (v.M2M_MPLANE, {10: ["H264", "MPG4", "H263", "MPG2", "VC1G"]}),
           "/dev/video11": (v.M2M_MPLANE, {10: ["YU12", "NV12"]}),      # the encoder: pictures go in
           "/dev/video19": (v.M2M_MPLANE, {10: ["S265"]})}
    _V4L2(monkeypatch, pi4)
    assert util.v4l2_decoders() == ["h263", "h264", "mpeg2video", "mpeg4", "vc1"]
    assert util.accel_obstacle("v4l2m2m", []) is None and util.v4l2_obstacle(["h264"], "Raspberry Pi 4") is None
    _V4L2(monkeypatch, pi5, denied=["/dev/video99"])                    # not in the video group: nothing is known
    assert util.v4l2_decoders() is None and util.accel_obstacle("v4l2m2m", []) is None
    monkeypatch.setattr(util, "LINUX", False)
    assert util.v4l2_decoders() is None

