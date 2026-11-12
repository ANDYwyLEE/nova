#!/usr/bin/env python3
"""doctor.py - `nova doctor`: what this machine runs Nova with, on one screen, for a bug report.

Nova, Python, Qt and PyAV versions and the FFmpeg build inside PyAV; the desktop session and the
OpenGL driver Nova's windows get; hardware decoders and encoders, GPUs, audio devices; a summary of
cameras.ini with passwords and stream keys masked; settings and recording folders with the space
left; the last warnings in logs/; and a few checks with a verdict.

    nova doctor            through the launcher (nova.bat on Windows)
    python doctor.py       the same without it
    python doctor.py --gl  only the OpenGL probe: run as a child process by the report, so that a
                           broken driver cannot take the report down with it

Everything printed passes through the secret masker once more before it leaves.
"""
import os
import re
import sys
import glob
import shutil
import platform
import datetime
import subprocess
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from util import mask_url, find_secrets, vaapi_node, LINUX, WINDOWS, MAC, WSL, MSYS, IS_PI    # noqa: E402

NVIDIA_MIN_DRIVER = 570          # the PyPI wheel's NVENC needs this (string in its libavcodec)
LOW_DISK_GB = 20
GL_TIMEOUT = 30
# what to look for in FFmpeg's configure line
FFMPEG_FEATURES = ("nvenc", "nvdec", "libvpl", "amf", "vaapi", "videotoolbox", "libx264", "libx265",
                   "libsvtav1", "libaom", "libdav1d", "libopus", "libsrt", "gnutls", "openssl", "libfreetype")
TLS_LIBRARIES = ("gnutls", "openssl")
HW_SUFFIXES = ("cuvid", "nvenc", "qsv", "amf", "vaapi", "videotoolbox", "v4l2m2m", "rkmpp", "mediacodec", "mf",
               "d3d12va", "vulkan")
ENV_SHOWN = ("NOVA_", "QT_", "LD_", "DYLD_", "PATH")      # [env] values printed; others are hidden


def _run(cmd, timeout=10):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (p.stdout or "").strip() if p.returncode == 0 else ""


def _gb(n):
    return f"{n / 1e9:.0f} GB"


def _tls_works():
    """Can this FFmpeg open TLS URLs (https, rtmps)? Asked of FFmpeg itself, for builds whose configure line names
    no TLS library: only "protocol not found" means no; the refused connection to this machine's discard port
    means the protocol is there."""
    import av
    try:
        av.open("https://127.0.0.1:9/", timeout=2).close()
    except av.error.ProtocolNotFoundError:
        return False
    except Exception:
        return True
    return True


class _QuietStderr:
    """PortAudio prints ALSA chatter on fd 2 while it starts; keep it out of the report."""
    def __enter__(self):
        try:
            self.old = os.dup(2)
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 2)
            os.close(devnull)
        except OSError:
            self.old = None
        return self

    def __exit__(self, *a):
        if self.old is not None:
            os.dup2(self.old, 2)
            os.close(self.old)


# ------------------------------------------------------------------ the lines
def nova_version():
    if os.path.exists(os.path.join(HERE, ".git")):
        v = _run(["git", "-C", HERE, "describe", "--tags", "--always", "--dirty"])
        b = _run(["git", "-C", HERE, "rev-parse", "--abbrev-ref", "HEAD"])
        if v:
            return f"{v} ({b})" if b else v
    return "unknown (not a git checkout)"


def os_line():
    if LINUX:
        try:
            name = platform.freedesktop_os_release().get("PRETTY_NAME", "Linux")
        except (OSError, AttributeError):
            name = "Linux"
        tags = [t for t, on in (("WSL", WSL), ("Raspberry Pi", IS_PI)) if on]
        return f"{name}, kernel {platform.release()}, {platform.machine()}" + (f" [{', '.join(tags)}]" if tags else "")
    if MAC:
        return f"macOS {platform.mac_ver()[0]}, {platform.machine()}"
    if WINDOWS:
        return f"Windows {platform.version()}, {platform.machine()}" + (" [MSYS2]" if MSYS else "")
    return platform.platform()


def desktop_line():
    if WINDOWS or MAC:
        return "-"
    e = os.environ
    session = e.get("XDG_SESSION_TYPE") or ("wayland" if e.get("WAYLAND_DISPLAY") else "x11" if e.get("DISPLAY") else "none")
    parts = [f"{session} session", e.get("XDG_CURRENT_DESKTOP") or e.get("DESKTOP_SESSION") or "unknown desktop",
             f"DISPLAY={e.get('DISPLAY') or '-'}", f"WAYLAND_DISPLAY={e.get('WAYLAND_DISPLAY') or '-'}",
             f"QT_QPA_PLATFORM={e.get('QT_QPA_PLATFORM') or '-'}"]
    if e.get("NOVA_QT_PLATFORM"):
        parts.append(f"NOVA_QT_PLATFORM={e['NOVA_QT_PLATFORM']}")
    return ", ".join(parts)


def qt_line():
    try:
        import PySide6
        from PySide6.QtCore import qVersion
        return f"PySide6 {PySide6.__version__}, Qt {qVersion()}"
    except Exception as e:
        return f"PySide6 not importable ({e})"


QT_MODULES = ("QtGui", "QtOpenGL", "QtNetwork")       # what Nova imports besides QtCore


def qt_module_errors(modules=QT_MODULES):
    """[(module, why)] for the Qt modules that do not load: each one needs system libraries the PySide6 wheel
    does not bring (QtNetwork wants libgssapi_krb5, QtGui libEGL and libfontconfig, ...), and a minimal
    system lacks some.  QtNetwork is the launcher's control channel: without it `nova start` starts nothing."""
    import importlib
    out = []
    for m in modules:
        try:
            importlib.import_module("PySide6." + m)
        except Exception as e:
            out.append((m, " ".join(str(e).split())))
    return out


def qt_modules_check(errors):
    """Check lines for Qt modules that do not load; none when PySide6 itself is missing (the Qt line says so)."""
    out = []
    for m, why in errors:
        if "No module named 'PySide6'" in why:
            return []
        what = {"QtNetwork": "`nova start` and `nova record` cannot start cameras (their control channel)",
                "QtGui": "no camera can start", "QtOpenGL": "no window can open"}.get(m, "Nova cannot start")
        out.append(("⚠", f"PySide6.{m} does not load ({why[:110]}): {what}; a system library Qt needs is missing, "
                         f"the packages are in docs/install.md"))
    return out


def gl_probe():
    """The platform plugin and the OpenGL driver a Nova window would get, with the format nova.py
    asks for (OpenGL 3.3 core).  Run as a child process (python doctor.py --gl)."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or WINDOWS or MAC):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtGui import QGuiApplication, QOpenGLContext, QOffscreenSurface, QSurfaceFormat
    except Exception as e:
        return f"n/a (PySide6: {e})"
    app = QGuiApplication.instance() or QGuiApplication([sys.argv[0]])
    plat = app.platformName()
    fmt = QSurfaceFormat()
    fmt.setRenderableType(QSurfaceFormat.OpenGL)
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.CoreProfile)
    ctx = QOpenGLContext()
    ctx.setFormat(fmt)
    if not ctx.create():
        ctx = QOpenGLContext()                        # whatever the platform offers
        if not ctx.create():
            return f"{plat}: no OpenGL context at all"
    surf = QOffscreenSurface()
    surf.setFormat(ctx.format())
    surf.create()
    if not ctx.makeCurrent(surf):
        return f"{plat}: the context cannot be made current"
    got = f"{ctx.format().majorVersion()}.{ctx.format().minorVersion()}" + (" OpenGL ES" if ctx.isOpenGLES() else "")
    try:
        try:
            from OpenGL.GL import glGetString, GL_VENDOR, GL_RENDERER, GL_VERSION
            vendor, renderer, version = ((glGetString(x) or b"?").decode(errors="replace")
                                         for x in (GL_VENDOR, GL_RENDERER, GL_VERSION))
        except Exception as e:
            # Windows without a GPU driver: Qt falls back to its own opengl32sw.dll (Mesa), while
            # PyOpenGL calls the system opengl32.dll, which has no context current; every GL call of a
            # Nova window then fails. Qt can still name what it got. One line: the report shows the last.
            f = ctx.functions()
            qt = " / ".join(str(f.glGetString(x) or "?") for x in (0x1F00, 0x1F01, 0x1F02))
            why = type(e).__name__ + (f" {e.err}" if getattr(e, "err", None) is not None else "")
            return f"{plat}: {qt} (context {got}{_qt_software_gl()}), PyOpenGL cannot use it ({why})"
        return f"{plat}: {vendor} / {renderer} / {version} (context {got})"
    finally:
        ctx.doneCurrent()


def _qt_software_gl():
    """', Qt's software opengl32sw.dll' when this Windows process loaded it, else ''."""
    if not WINDOWS:
        return ""
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32")
        k32.GetModuleHandleW.restype = ctypes.c_void_p
        k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        return ", Qt's software opengl32sw.dll" if k32.GetModuleHandleW("opengl32sw.dll") else ""
    except (OSError, AttributeError):
        return ""


def windows_service_session():
    """True in Windows session 0, where services and ssh logins run: it has no display and no GPU, so Qt
    falls back to its software opengl32sw.dll there whatever the driver; windows on the desktop are not
    affected. (Seen 2026-11-11: doctor over ssh blamed a missing GPU driver on a PC with a working one.)"""
    if not WINDOWS:
        return False
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32")
        sid = ctypes.c_ulong()
        return bool(k32.ProcessIdToSessionId(k32.GetCurrentProcessId(), ctypes.byref(sid))) and sid.value == 0
    except (OSError, AttributeError):
        return False


def gl_line(python=None):
    """gl_probe() in a child process: a crashing driver or a hung display connection must not
    take the report with it."""
    try:
        p = subprocess.run([python or sys.executable, os.path.abspath(__file__), "--gl"], capture_output=True,
                           text=True, timeout=GL_TIMEOUT, env=dict(os.environ, PYTHONPATH=HERE))
    except subprocess.TimeoutExpired:
        return f"n/a (the OpenGL probe did not answer within {GL_TIMEOUT} s)"
    except OSError as e:
        return f"n/a ({e})"
    out = (p.stdout or "").strip().splitlines()
    if p.returncode != 0 or not out:
        err = (p.stderr or "").strip().splitlines()
        return f"n/a (the probe failed: {err[-1] if err else f'exit {p.returncode}'})"
    return out[-1]


def ffmpeg_lines():
    """[(label, text)] for PyAV and the FFmpeg inside it."""
    try:
        import av
        import av._core as core
    except Exception as e:
        return [("PyAV", f"not importable ({e})")]
    meta = getattr(core, "library_meta", None) or {}
    conf = (meta.get("libavcodec") or {}).get("configuration", "") or ""
    ffver = getattr(core, "ffmpeg_version_info", None) or ".".join(str(x) for x in av.library_versions.get("libavcodec", ()))
    pkg = os.path.dirname(av.__file__)
    wheel = (os.path.isdir(os.path.join(os.path.dirname(pkg), "av.libs")) or os.path.isdir(os.path.join(pkg, ".dylibs"))
             or "--prefix=/tmp/vendor" in conf)
    flags = set(re.findall(r"--enable-([\w-]+)", conf))
    on = [f for f in FFMPEG_FEATURES if f in flags]
    off = [f for f in FFMPEG_FEATURES if f not in flags]
    tls = ""
    if conf and not flags & set(TLS_LIBRARIES) and _tls_works():
        # the macOS wheel: TLS (https, rtmps) from the system, which FFmpeg detects without a configure switch
        off = [f for f in off if f not in TLS_LIBRARIES]
        tls = f"; TLS from the system ({'SecureTransport' if MAC else 'Schannel' if WINDOWS else 'not named in the configure line'})"
    lines = [("PyAV", f"{av.__version__} ({'PyPI wheel' if wheel else 'built from source'}), FFmpeg {ffver}, "
                      f"libavcodec {'.'.join(str(x) for x in av.library_versions.get('libavcodec', ()))}"),
             ("FFmpeg", f"built with: {' '.join(on) or '?'}{tls}" + (f"; not in this build: {' '.join(off)}" if off else ""))]
    if not conf:
        lines[-1] = ("FFmpeg", "configure line not available from this PyAV")
    try:
        from av.codec.hwaccel import hwdevices_available
        lines.append(("HW devices", ", ".join(str(x) for x in hwdevices_available()) or "none"))
    except Exception as e:
        lines.append(("HW devices", f"n/a ({e})"))
    try:
        names = sorted(n for n in av.codecs_available if any(n.endswith("_" + s) for s in HW_SUFFIXES))

        def has(n, mode):
            try:
                av.Codec(n, mode); return True
            except Exception:
                return False
        dec = [n for n in names if has(n, "r")]
        enc = [n for n in names if has(n, "w")]
        lines.append(("HW decoders", " ".join(dec) or "none"))
        lines.append(("HW encoders", " ".join(enc) or "none"))
    except Exception as e:
        lines.append(("HW codecs", f"n/a ({e})"))
    return lines


DRM_VENDORS = {"0x8086": "Intel", "0x1002": "AMD", "0x10de": "NVIDIA"}


def drm_gpus(root="/sys/class/drm"):
    """[(vendor, 'vvvv:dddd', kernel driver, render node)] of the GPUs the Linux kernel has: the integrated
    one next to the NVIDIA cards too, which nvidia-smi does not list."""
    out = []
    for card in sorted(glob.glob(os.path.join(root, "card[0-9]*"))):
        if "-" in os.path.basename(card):                 # card1-HDMI-A-1: a connector, not a GPU
            continue
        dev = os.path.join(card, "device")
        try:
            with open(os.path.join(dev, "vendor")) as f: vendor = f.read().strip()
            with open(os.path.join(dev, "device")) as f: device = f.read().strip()
        except OSError:
            continue
        try:
            driver = os.path.basename(os.readlink(os.path.join(dev, "driver")))
        except OSError:
            driver = "no driver"
        node = next((os.path.basename(n) for n in sorted(glob.glob(os.path.join(dev, "drm", "renderD*")))), "no render node")
        out.append((DRM_VENDORS.get(vendor, vendor), f"{vendor[2:]}:{device[2:]}", driver, node))
    return out


INTEL_PACKAGES = (        # package manager -> Intel's VA driver (with its encoders) and Quick Sync runtime
    ("apt-get", "sudo apt install intel-media-va-driver-non-free libmfx-gen1.2"),
)


def intel_packages_hint():
    """What to install for Quick Sync, in this distribution's words where they have been tried."""
    for tool, line in INTEL_PACKAGES:
        if shutil.which(tool):
            return f"{line} (docs/install.md)"
    return "install Intel's media driver (iHD, the build with encoders) and its Quick Sync runtime (docs/install.md)"


def node_access(gpus, can=os.access):
    """[(vendor, ids, node, group)] of the Intel/AMD GPUs whose render node this user cannot open.  A desktop
    login is given the access (an ACL of the seat); over ssh, in a service or in a container it takes the
    node's group, and without it VAAPI says only `No VA display found` and Quick Sync `Unknown error`."""
    out = []
    for vendor, ids, _driver, node in gpus:
        path = f"/dev/dri/{node}"
        if vendor == "NVIDIA" or not node.startswith("renderD") or can(path, os.R_OK | os.W_OK):
            continue
        try:
            import grp
            group = grp.getgrgid(os.stat(path).st_gid).gr_name
        except (ImportError, KeyError, OSError):
            group = "render"
        out.append((vendor, ids, node, group))
    return out


def node_access_check(blocked, accels, user=None):
    """Check lines for render nodes that are closed to the user: ⚠ when --accel asks for that GPU."""
    if user is None:
        try:
            import getpass
            user = getpass.getuser()
        except Exception:
            user = "you"
    mark = "⚠" if accels & {"vaapi", "qsv"} else "ℹ"
    return [(mark, f"/dev/dri/{node} ({vendor} {ids}) is closed to {user}: --accel vaapi and qsv cannot use that GPU "
                   f"and decode in software. A desktop login is given the access; over ssh or in a service: "
                   f"sudo usermod -aG {group} {user}, then log in again")
            for vendor, ids, node, group in blocked]


def nvidia_index_check(indexes, count, accels):
    """A check line when --gpu / NOVA_GPU_DEVICES name an NVIDIA card this machine does not have (a
    cameras.ini carried over from a machine with more cards): those cameras are moved to GPU 0."""
    beyond = sorted(i for i in indexes if i is not None and i >= count)
    if not count or not beyond or not accels & {"cuda", "nvdec"}:
        return []
    return [("⚠", f"--gpu / NOVA_GPU_DEVICES name GPU {', '.join(map(str, beyond))}, but this machine has {count} NVIDIA "
                  f"card{'s' if count != 1 else ''} (0{'-' + str(count - 1) if count > 1 else ''}): those cameras use "
                  f"GPU 0 and say so in their log")]


def quick_sync_error():
    """None when h264_qsv opens on this machine, else why not ("" when the FFmpeg build has no h264_qsv).
    Opening it starts a Quick Sync session, which needs the Intel runtime and driver, not just FFmpeg."""
    try:
        import av
        if "h264_qsv" not in av.codecs_available:
            return ""
        import logsync
        from avwriter import EncoderManager
        os.environ.setdefault("LIBVA_MESSAGING_LEVEL", "0")       # libva prints to stderr by itself
        with logsync.av_quiet():
            return EncoderManager._try_open("h264_qsv", {})
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def quick_sync_check(accels, gpus, error):
    """The check line for Quick Sync, or None when it is of no interest here: asked for with --accel qsv,
    or an Intel GPU is in the machine (Linux, where the runtime is two packages away)."""
    wanted = "qsv" in accels
    intel = [g for g in gpus if g[0] == "Intel"]
    if not wanted and not intel:
        return None
    if error == "":
        return ("⚠", "--accel qsv is in cameras.ini but this FFmpeg build has no h264_qsv") if wanted else None
    if error is None:
        return ("✓", "Quick Sync starts on this machine: --accel qsv decodes and records on the Intel GPU")
    fix = (intel_packages_hint() if LINUX else "update the Intel graphics driver")
    short = " ".join(str(error).split("last error log:")[-1].split())[:90]      # FFmpeg's own words, when it has any
    if wanted:
        return ("⚠", f"--accel qsv is in cameras.ini but Quick Sync does not start ({short}): software decoding; {fix}")
    return ("ℹ", f"the Intel GPU could decode and record with --accel qsv, but Quick Sync does not start ({short}): {fix}")


def vaapi_error(node):
    """None when a VAAPI device opens on this DRM render node, else why not ("" when the FFmpeg build has
    no VAAPI at all)."""
    try:
        import av
        from av.codec.hwaccel import HWAccel, hwdevices_available
        if "vaapi" not in [str(d) for d in hwdevices_available()]:
            return ""
        import logsync
        os.environ.setdefault("LIBVA_MESSAGING_LEVEL", "0")
        with logsync.av_quiet():
            av.CodecContext.create("h264", "r", hwaccel=HWAccel(device_type="vaapi", device=node,
                                                               allow_software_fallback=False))
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def config_gpus(defaults, env, cameras):
    """The GPU indexes the cameras are given: NOVA_GPU_DEVICES and every --gpu in an opts line.  None is
    among them when cameras get no index at all (no NOVA_GPU_DEVICES, no --gpu in [defaults]): VAAPI then
    looks for its node (util.vaapi_node) and cuda takes GPU 0."""
    found = set()
    for text in [env.get("NOVA_GPU_DEVICES", "")] + [m for t in [defaults.get("opts", "")] + [c.opts for c in cameras if c.enabled]
                                                     for m in re.findall(r"--gpu[= ]+([\d,]+)", t)]:
        found |= {int(x) for x in re.findall(r"\d+", text)}
    if not env.get("NOVA_GPU_DEVICES") and not re.search(r"--gpu[= ]+[\d,]+", defaults.get("opts", "")):
        found.add(None)
    return found


def vaapi_check(indexes, gpus, probe, auto=None):
    """Check lines for --accel vaapi: --gpu N means /dev/dri/renderD(128+N), no --gpu (None among the
    indexes) the first node that is not an NVIDIA card (`auto`), and on a machine with several GPUs not
    every node has VAAPI. A camera on a node without it decodes in software and says so only in its own log."""
    name = {g[3]: f"{g[0]} {g[1]}" for g in gpus}
    auto = auto or os.path.basename(vaapi_node(None))
    how = lambda i: "found without --gpu" if i is None else f"--gpu {i}"
    tried = {}
    for i in sorted(indexes, key=lambda i: (i is not None, i or 0)):
        n = auto if i is None else f"renderD{128 + i}"
        tried[i] = (n, probe(f"/dev/dri/{n}"))
    if any(err == "" for _, err in tried.values()):
        return []                                         # a build without VAAPI: the --accel check says so
    good = [(i, n) for i, (n, err) in tried.items() if err is None]
    out = []
    if good:
        out.append(("✓", "VAAPI opens on " + ", ".join(f"{n} ({name.get(n, 'GPU')}, {how(i)})" for i, n in good)
                    + ": --accel vaapi decodes and records there"))
    bad = [(i, n, err) for i, (n, err) in tried.items() if err is not None]
    if bad:
        elsewhere = [f"{g[3]} (--gpu {int(g[3][7:]) - 128})" for g in gpus
                     if g[3].startswith("renderD") and g[3] not in {n for n, _ in tried.values()}
                     and probe(f"/dev/dri/{g[3]}") is None]
        works = [f"{n} ({how(i)})" for i, n in good] + elsewhere
        for i, n, err in bad:
            short = " ".join(str(err).split("last error log:")[-1].split())[:70]
            which = f"GPU {i} is" if i is not None else "without --gpu the node is"
            out.append(("⚠", f"--accel vaapi: {which} /dev/dri/{n} ({name.get(n, 'no GPU there')}), where VAAPI does not "
                             f"start ({short}): cameras given that GPU decode in software"
                             + (f"; VAAPI answers on {', '.join(works)}" if works else "")))
    return out


def gpu_lines():
    lines = []
    if shutil.which("nvidia-smi"):
        out = _run(["nvidia-smi", "--query-gpu=index,name,driver_version,memory.total,memory.used", "--format=csv,noheader"])
        if out:
            gpus = [[c.strip() for c in row.split(",")] for row in out.splitlines()]
            drv = gpus[0][2] if gpus and len(gpus[0]) > 2 else "?"
            lines.append(("GPU", f"NVIDIA driver {drv}: " + "; ".join(f"{g[0]} {g[1]}, {g[3]} ({g[4]} used)" for g in gpus if len(g) >= 5)))
        else:
            lines.append(("GPU", "nvidia-smi present but gave no answer"))
    if LINUX:
        for vendor, ids, driver, node in drm_gpus():
            if vendor != "NVIDIA" or not lines:           # the NVIDIA cards are listed above, with their memory
                lines.append(("GPU", f"{vendor} {ids} ({driver}), {node}"))
        nodes = sorted(os.path.basename(p) for p in glob.glob("/dev/dri/renderD*"))
        lines.append(("DRM nodes", " ".join(nodes) if nodes else "none (/dev/dri: no render node, VAAPI impossible)"))
    if not lines:
        lines.append(("GPU", "no nvidia-smi; the platform decoder (VideoToolbox, D3D11VA, QSV) uses the system GPU"))
    return lines


def audio_line():
    try:
        import pyaudio
    except ImportError:
        return "PyAudio not installed: no audio playback"
    try:
        with _QuietStderr():
            pa = pyaudio.PyAudio()
    except Exception as e:
        return f"PyAudio {pyaudio.__version__}: PortAudio failed to start ({e})"
    try:
        n = pa.get_device_count()
        try:
            d = pa.get_default_output_device_info()
            dflt = f'default output "{d["name"]}" ({d["maxOutputChannels"]} ch, {int(d["defaultSampleRate"])} Hz)'
        except Exception:
            dflt = "no default output device"
        return f"PyAudio {pyaudio.__version__}, {pyaudio.get_portaudio_version_text()}, {n} devices, {dflt}"
    finally:
        pa.terminate()


def config_lines(config):
    """[(label, text)], plus the parsed (defaults, env, cams) or None, with every value masked."""
    import cams
    path = config or cams.INI
    base = os.path.basename(path)
    if not os.path.exists(path):
        return [("Config", f"{base}: not found (single-camera use, or another file with -c)")], None
    mode = "-" if WINDOWS else oct(os.stat(path).st_mode & 0o777)[2:]
    try:
        defaults, env, cs = cams.load(path, quiet=True)
    except SystemExit as e:
        return [("Config", f"{base}: {mask_url(str(e))}")], None
    by = Counter(("device" if c.url.isdigit() else c.url.split("://", 1)[0].lower() if "://" in c.url else "other") for c in cs)
    lines = [("Config", f"{base}: {len(cs)} cameras ({sum(c.enabled for c in cs)} enabled), mode {mode}: "
                        + ", ".join(f"{k}×{v}" for k, v in sorted(by.items())))]
    if defaults.get("opts"):
        lines.append(("", f"[defaults] opts = {mask_url(defaults['opts'])}"))
    if defaults.get("python"):
        lines.append(("", f"[defaults] python = {defaults['python']}"))
    if env:
        shown = [f"{k}={v if k.startswith(ENV_SHOWN) or k in ENV_SHOWN else '…'}" for k, v in env.items()]
        lines.append(("", "[env] " + "  ".join(shown)))
    opts = [c for c in cs if c.opts]
    for c in opts[:20]:
        lines.append(("", f"[{c.id}] {mask_url(c.opts)}" + ("" if c.enabled else " (disabled)")))
    if len(opts) > 20:
        lines.append(("", f"… and {len(opts) - 20} more cameras with options"))
    try:
        pids = cams.read_pids()
        running = sum(1 for st in pids.values() if cams.alive(int(st.get("pid", 0))))
        lines.append(("Launched", f"{running} camera(s) running now, {len(pids)} in the pid file"))
    except SystemExit as e:
        lines.append(("Launched", str(e)))
    return lines, (defaults, env, cs)


def settings_lines():
    lines = []
    try:
        import cams
        qs = cams.window_settings()
        if qs is not None:
            n = len(qs.hashes())
            lines.append(("Settings", f"{qs.dir}: {n} window file(s)" if n else f"{qs.dir}: none written yet"))
        lines.append(("Layouts", f"{len(cams.layout_names())} in {os.path.relpath(cams.LAYOUTS, HERE)}/"))
    except Exception as e:
        lines.append(("Settings", f"n/a ({e})"))
    return lines


def _tree_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.stat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def storage_line():
    videos = os.path.join(HERE, "videos")
    target = videos if os.path.isdir(videos) else HERE
    try:
        u = shutil.disk_usage(target)
    except OSError as e:
        return f"n/a ({e})", None
    used = f"videos/ {_gb(_tree_size(videos))}, " if os.path.isdir(videos) else ""
    return f"{used}{_gb(u.free)} free of {_gb(u.total)} on the volume of {os.path.relpath(target, HERE) or '.'}", u.free


def log_lines(limit=5):
    logdir = os.path.join(HERE, "logs")
    files = sorted(glob.glob(os.path.join(logdir, "*.log")), key=os.path.getmtime, reverse=True)
    if not files:
        return [("Logs", "logs/: none")]
    found = []
    for f in files[:3]:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                tail = fh.readlines()[-2000:]
        except OSError:
            continue
        for line in tail:
            if re.search(r" - (WARN|ERROR|CRIT)", line):
                found.append((os.path.basename(f), line.rstrip()))
    lines = [("Logs", f"logs/: {len(files)} files, newest {os.path.basename(files[0])}; last warnings:")]
    for name, line in found[-limit:]:
        lines.append(("", f"{name}: {line[:160]}"))
    if not found:
        lines.append(("", "no WARN/ERROR lines in the three newest logs"))
    return lines


# ------------------------------------------------------------------ the checks
def av_minimum():
    """The oldest PyAV requirements.txt accepts (`av>=X`), or None."""
    try:
        m = re.search(r"^av\s*>=\s*([\w.]+)", open(os.path.join(HERE, "requirements.txt"), encoding="utf-8").read(), re.M)
    except OSError:
        return None
    return m.group(1) if m else None


def _release(v):
    """'19.0.2' -> (19, 0, 2); pre-release and local suffixes are ignored."""
    return tuple(int(x) for x in re.findall(r"\d+", v.split("+")[0])[:3])


def _can_import(python, name):
    """Can the camera interpreter import `name`?"""
    try:
        return subprocess.run([python or sys.executable, "-c", f"import {name}"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def checks(gl, ffmpeg, gpu, cfg, free, config_path, python=None):
    """[(mark, text)]: ✓ fine, ⚠ worth fixing, ℹ good to know."""
    out = []
    # PyAV against the oldest release requirements.txt accepts (newer ones are welcome)
    try:
        import av
        need = av_minimum()
        if need and _release(av.__version__) < _release(need):
            out.append(("⚠", f"PyAV {av.__version__} is older than {need}, the oldest Nova supports: RTMP streams cannot hand "
                             f"rtmp_flush_interval to the protocol (pip install -r requirements.txt)"))
        elif need:
            out.append(("✓", f"PyAV {av.__version__} ({need} or newer is needed)"))
    except Exception:
        pass
    # NVIDIA driver for the wheel's NVENC
    m = re.search(r"NVIDIA driver (\d+)", gpu)
    if m:
        d = int(m.group(1))
        if "PyPI wheel" in ffmpeg and d < NVIDIA_MIN_DRIVER:
            out.append(("⚠", f"NVIDIA driver {d} is older than {NVIDIA_MIN_DRIVER}, the first the wheel's NVENC works with: "
                             f"encoding falls back to libx264"))
        else:
            out.append(("✓", f"NVIDIA driver {d} is recent enough for NVENC"))
    out.extend(qt_modules_check(qt_module_errors()))
    # OpenGL
    if windows_service_session() and ("PyOpenGL cannot use it" in gl or re.search(r"llvmpipe|softpipe", gl, re.I)):
        out.append(("ℹ", "this runs in Windows session 0 (ssh or a service), which has no GPU: the OpenGL above is not "
                         "what windows on the desktop get; run nova doctor from the desktop to check --opengl"))
    elif "PyOpenGL cannot use it" in gl:
        out.append(("⚠", "--opengl cannot work here: Nova draws through PyOpenGL, which cannot reach this OpenGL "
                         "(on Windows: no GPU driver, Qt fell back to its software opengl32sw.dll), and an --opengl "
                         "window closes at start; leave --opengl out of opts, or install the GPU driver"))
    elif re.search(r"llvmpipe|softpipe|swrast|Software Rasterizer", gl, re.I):
        out.append(("⚠", "OpenGL is rendered in software (no GPU driver in use): --opengl will cost CPU; check the driver"))
    elif gl.startswith("n/a") or "no OpenGL" in gl:
        # both renderers draw into a QOpenGLWindow (QPainter on OpenGL without --opengl)
        out.append(("ℹ", "no OpenGL context here: Nova windows need one with or without --opengl; "
                         "`nova record` (no window) still works"))
    else:
        out.append(("✓", "OpenGL comes from a GPU driver"))
    # desktop
    if LINUX and (os.environ.get("XDG_SESSION_TYPE") == "wayland" or os.environ.get("WAYLAND_DISPLAY")):
        out.append(("ℹ", "Wayland session: windows run through XWayland, whatever QT_QPA_PLATFORM the desktop exports "
                         "(QT_QPA_PLATFORM = wayland in [env] for native windows); placement differs per compositor "
                         "(docs/user-guide.md, Layouts)"))
    gpus = drm_gpus() if LINUX else []
    blocked = node_access(gpus) if LINUX else []
    closed = {b[2] for b in blocked}
    accels = set()
    # cameras.ini
    if cfg is not None:
        if not WINDOWS and config_path and os.path.exists(config_path) and os.stat(config_path).st_mode & 0o077:
            out.append(("⚠", f"{os.path.basename(config_path)} holds passwords but other users can read it: chmod 600"))
        else:
            out.append(("✓", f"{os.path.basename(config_path)} is readable by you only" if not WINDOWS else
                        f"{os.path.basename(config_path)} found"))
        defaults, env, cs = cfg
        for text in [defaults.get("opts", "")] + [c.opts for c in cs if c.enabled]:
            for m in re.finditer(r"--accel[= ]+(\S+)", text):
                accels.add(m.group(1).lower())
        if accels:
            try:
                from videoworker import accel_supported
                for a in sorted(accels):
                    if accel_supported(a):
                        out.append(("✓", f"--accel {a} has a decoder in this FFmpeg build"))
                    else:
                        out.append(("⚠", f"--accel {a} is in cameras.ini but this FFmpeg build has no {a} decoder: "
                                         f"software decoding (docs/install.md)"))
            except Exception as e:
                out.append(("ℹ", f"--accel {' '.join(sorted(accels))}: could not check ({e})"))
        indexes = config_gpus(defaults, env, cs)
        out.extend(nvidia_index_check(indexes, len(re.findall(r"\d+ [^,;]+, \d+ MiB", gpu)), accels))
        if LINUX and "vaapi" in accels:
            out.extend(vaapi_check(indexes, gpus, lambda node: "the node is closed to this user"
                                   if os.path.basename(node) in closed else vaapi_error(node)))
        if ("qsv" in accels or any(g[0] == "Intel" for g in gpus)) and not any(b[0] == "Intel" for b in blocked):
            line = quick_sync_check(accels, gpus, quick_sync_error())
            if line:
                out.append(line)
        xm = [c for c in cs if c.enabled and c.url.lower().startswith("xmeye://")]
        if xm:
            if _can_import(python, "dvrip"):
                out.append(("✓", f"python-dvr is installed for the {len(xm)} xmeye:// camera(s)"))
            else:
                out.append(("⚠", f"{len(xm)} xmeye:// camera(s) but python-dvr is not installed: "
                                 f"{python or sys.executable} -m pip install python-dvr"))
    out.extend(node_access_check(blocked, accels))
    # disk
    if free is not None:
        if free < LOW_DISK_GB * 1e9:
            out.append(("⚠", f"only {_gb(free)} free for recordings (below {LOW_DISK_GB} GB)"))
        else:
            out.append(("✓", f"{_gb(free)} free for recordings"))
    return out


# ------------------------------------------------------------------ the report
def report(config=None, gl=True, python=None):
    rows = []
    rows.append(("Nova", f"{nova_version()}, {HERE}"))
    rows.append(("Python", f"{platform.python_version()} ({sys.executable})"))
    rows.append(("OS", os_line()))
    rows.append(("Desktop", desktop_line()))
    rows.append(("Qt", qt_line()))
    gl_text = gl_line(python) if gl else "skipped"
    rows.append(("OpenGL", gl_text))
    ff = ffmpeg_lines()
    rows.extend(ff)
    gpu = gpu_lines()
    rows.extend(gpu)
    rows.append(("Audio", audio_line()))
    cfg_rows, cfg = config_lines(config)
    rows.extend(cfg_rows)
    rows.extend(settings_lines())
    storage, free = storage_line()
    rows.append(("Storage", storage))
    rows.extend(log_lines())
    import cams
    config_path = config or cams.INI
    verdicts = checks(gl_text, " ".join(t for _, t in ff), " ".join(t for _, t in gpu), cfg, free, config_path, python)

    width = 11
    lines = [f"Nova doctor, {datetime.datetime.now():%Y-%m-%d %H:%M} (paste this into a bug report; passwords and stream keys are masked)", ""]
    for label, text in rows:
        lines.append(f"{label:<{width}} {text}" if label else f"{'':<{width}} {text}")
    lines += ["", "Checks"]
    for mark, text in verdicts:
        lines.append(f"  {mark} {text}")
    text = "\n".join(lines)
    text = mask_url(text)                      # once more, for anything a line assembled itself
    for s in find_secrets(text):               # the finder matches the masker's own '***' too
        if s != "***":
            text = text.replace(s, "***")
    return text + "\n"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--gl"]:
        print(gl_probe())
        return 0
    config = None
    while argv and argv[0] in ("-c", "--config"):
        if len(argv) < 2:
            sys.exit("-c needs a file")
        config = argv[1]
        argv = argv[2:]
    if argv:
        sys.exit("usage: python doctor.py [-c cameras.ini] [--gl]")
    sys.stdout.write(report(config))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
