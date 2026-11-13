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
