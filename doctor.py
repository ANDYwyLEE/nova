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
from util import mask_url, find_secrets, LINUX, WINDOWS, MAC, WSL, MSYS, IS_PI    # noqa: E402

NVIDIA_MIN_DRIVER = 570          # the PyPI wheel's NVENC needs this (string in its libavcodec)
LOW_DISK_GB = 20
GL_TIMEOUT = 30
# what to look for in FFmpeg's configure line
FFMPEG_FEATURES = ("nvenc", "nvdec", "libvpl", "amf", "vaapi", "videotoolbox", "libx264", "libx265",
                   "libsvtav1", "libaom", "libdav1d", "libopus", "libsrt", "gnutls", "openssl", "libfreetype")
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
    return ", ".join(parts)


def qt_line():
    try:
        import PySide6
        from PySide6.QtCore import qVersion
        return f"PySide6 {PySide6.__version__}, Qt {qVersion()}"
    except Exception as e:
        return f"PySide6 not importable ({e})"


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
    try:
        try:
            from OpenGL.GL import glGetString, GL_VENDOR, GL_RENDERER, GL_VERSION
            vendor, renderer, version = ((glGetString(x) or b"?").decode(errors="replace")
                                         for x in (GL_VENDOR, GL_RENDERER, GL_VERSION))
        except Exception as e:
            return f"{plat}: context {ctx.format().majorVersion()}.{ctx.format().minorVersion()}, PyOpenGL failed: {e}"
        got = f"{ctx.format().majorVersion()}.{ctx.format().minorVersion()}"
        es = " OpenGL ES" if ctx.isOpenGLES() else ""
        return f"{plat}: {vendor} / {renderer} / {version} (context {got}{es})"
    finally:
        ctx.doneCurrent()


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
    lines = [("PyAV", f"{av.__version__} ({'PyPI wheel' if wheel else 'built from source'}), FFmpeg {ffver}, "
                      f"libavcodec {'.'.join(str(x) for x in av.library_versions.get('libavcodec', ()))}"),
             ("FFmpeg", f"built with: {' '.join(on) or '?'}" + (f"; not in this build: {' '.join(off)}" if off else ""))]
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
def _can_import(python, name):
    """Can the camera interpreter import `name`?"""
    try:
        return subprocess.run([python or sys.executable, "-c", f"import {name}"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def checks(gl, ffmpeg, gpu, cfg, free, config_path, python=None):
    """[(mark, text)]: ✓ fine, ⚠ worth fixing, ℹ good to know."""
    out = []
    # PyAV version against requirements.txt
    try:
        import av
        pin = re.search(r"^av==(\S+)", open(os.path.join(HERE, "requirements.txt"), encoding="utf-8").read(), re.M)
        if pin and pin.group(1) != av.__version__:
            out.append(("⚠", f"PyAV {av.__version__} differs from the pinned {pin.group(1)} (pip install -r requirements.txt)"))
        else:
            out.append(("✓", f"PyAV {av.__version__} is the pinned version"))
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
    # OpenGL
    if re.search(r"llvmpipe|softpipe|swrast|Software Rasterizer", gl, re.I):
        out.append(("⚠", "OpenGL is rendered in software (no GPU driver in use): --opengl will cost CPU; check the driver"))
    elif gl.startswith("n/a") or "no OpenGL" in gl:
        out.append(("ℹ", "no OpenGL context here: Nova falls back to QPainter (--opengl has no effect)"))
    else:
        out.append(("✓", "OpenGL comes from a GPU driver"))
    # desktop
    if LINUX and (os.environ.get("XDG_SESSION_TYPE") == "wayland" or os.environ.get("WAYLAND_DISPLAY")):
        out.append(("ℹ", "Wayland session: windows run through XWayland unless QT_QPA_PLATFORM=wayland; placement differs "
                         "per compositor (docs/user-guide.md, Layouts)"))
    # cameras.ini
    if cfg is not None:
        if not WINDOWS and config_path and os.path.exists(config_path) and os.stat(config_path).st_mode & 0o077:
            out.append(("⚠", f"{os.path.basename(config_path)} holds passwords but other users can read it: chmod 600"))
        else:
            out.append(("✓", f"{os.path.basename(config_path)} is readable by you only" if not WINDOWS else
                        f"{os.path.basename(config_path)} found"))
        defaults, env, cs = cfg
        accels = set()
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
        xm = [c for c in cs if c.enabled and c.url.lower().startswith("xmeye://")]
        if xm:
            if _can_import(python, "dvrip"):
                out.append(("✓", f"python-dvr is installed for the {len(xm)} xmeye:// camera(s)"))
            else:
                out.append(("⚠", f"{len(xm)} xmeye:// camera(s) but python-dvr is not installed: "
                                 f"{python or sys.executable} -m pip install python-dvr"))
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
