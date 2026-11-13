"""Platform checks, password and stream-key masking for logs and terminals, small parsers shared by the
launcher and the camera (geometry, zoom, intervals), and short forms of FFmpeg's error texts."""
import glob
import os
import re
import sys
import platform


_system = platform.system().lower()
_machine = platform.machine().lower()

LINUX = (_system == "linux")
WINDOWS = (_system == "windows")
MAC = (_system == "darwin")
SOLARIS = (_system in ["sunos", "solaris"])
AIX = (_system == "aix")
HPUX = (_system == "hp-ux")

def utf8_stdio():
    """stdout and stderr in UTF-8 when they go to a file or a pipe (and on Windows always). Windows
    writes those in the ANSI code page (cp1252, cp949), which has no emoji: under `nova start` every
    log line with one was lost behind a "--- Logging error ---" traceback in logs/ID.log, and
    `nova doctor > report.txt` died at its first check mark. A Windows console is UTF-8 already."""
    for s in (sys.stdout, sys.stderr):
        if s is None or not hasattr(s, "reconfigure"):
            continue
        enc = (s.encoding or "").lower().replace("-", "").replace("_", "")
        try:
            if enc != "utf8" and (os.name == "nt" or not s.isatty()):
                s.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            pass


def is_wsl():
    if "WSL_DISTRO_NAME" in os.environ or "WSL_INTEROP" in os.environ: return True
    try:
        if os.path.exists("/proc/sys/kernel/osrelease"):
            with open("/proc/sys/kernel/osrelease") as f:
                if "microsoft" in f.read().lower(): return True
    except: pass
    try:
        if os.path.exists("/proc/version"):
            with open("/proc/version") as f:
                if "microsoft" in f.read().lower(): return True
    except: pass
    return False
WSL = is_wsl()

# The app id of a native Wayland window. Distinct from the XWayland window's class ("nova") so that
# window rules from `nova layout export NAME kwin` place native windows only: XWayland windows place
# themselves (their remembered place, or the layout's with --layout) and a rule would override that.
NATIVE_APP_ID = "nova.wayland"


def window_platform(env):
    """(the Qt platform for a camera window, a line for the log) on a Linux Wayland session that has XWayland;
    (None, None) anywhere else, where QT_QPA_PLATFORM is left as it is.

    A Wayland compositor does not let a window place itself; under XWayland GNOME, KDE and labwc (Xfce) put it
    where Nova asks, and on sway and Hyprland Nova places it through their IPC. So the platform is Nova's
    choice: XWayland. A QT_QPA_PLATFORM the desktop exports for every application (startxfce4 --wayland sets
    "wayland") is ignored, or every camera opened native, wherever the compositor liked. Another platform is
    asked for with NOVA_QT_PLATFORM, which the launcher sets from QT_QPA_PLATFORM in cameras.ini [env]."""
    if not LINUX or not env.get("WAYLAND_DISPLAY") or not env.get("DISPLAY"):
        return None, None
    want = env.get("NOVA_QT_PLATFORM", "").strip()
    if want:
        return want, f"Wayland session: Qt platform {want} (NOVA_QT_PLATFORM)"
    session = env.get("QT_QPA_PLATFORM", "").strip()
    note = (f"; the desktop's QT_QPA_PLATFORM={session} is for other applications" if session and session != "xcb"
            else "")
    return "xcb", f"Wayland session: running under XWayland{note} (QT_QPA_PLATFORM = wayland in [env] for a native window)"

def no_display(env):
    """True when a camera window cannot open in this environment: Linux with neither DISPLAY nor
    WAYLAND_DISPLAY (an ssh login, a service, cron) and no Qt platform asked for that draws without them
    (eglfs, linuxfb, vnc, offscreen). Qt then aborts with advice about libxcb-cursor0 and reinstalling."""
    if not LINUX or env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"):
        return False
    platform = (env.get("NOVA_QT_PLATFORM") or env.get("QT_QPA_PLATFORM") or "").split(":")[0].strip()
    return platform in ("", "xcb", "wayland", "wayland-egl")


NO_DISPLAY = ("no display in this session (DISPLAY and WAYLAND_DISPLAY are not set): a camera window cannot open. "
              "`nova record` (nova.py --headless) runs cameras without windows; a window on the machine's own "
              "screen is started from its desktop session, or with that session's DISPLAY")


def is_msys():
    # MSYS2, Cygwin, Git Bash 등을 탐지
    term = os.environ.get("MSYSTEM", "").lower()
    if term: return True
    if "cygwin" in _system or "msys" in _system: return True
    return False
MSYS = is_msys()

def is_pi_series():
    if not LINUX: return False
    try:
        with open("/proc/device-tree/model", "r") as f:
            model = f.read().lower()
            return any(p in model for p in ["raspberry pi", "orange pi", "banana pi", "rockchip", "sunxi"])
    except OSError:
        pass
    return False          # not every ARM Linux is a Pi: Jetson boards and ARM servers have other encoders

IS_PI = is_pi_series()


def get_os_type():
    if IS_PI: return "SBC(Pi/Rockchip)"
    if WSL: return "Linux(WSL)"
    if MSYS: return "Windows(MSYS2)"
    if WINDOWS: return "Windows"
    if MAC: return "macOS"
    if LINUX: return "Linux"
    if SOLARIS: return "Solaris"
    if AIX: return "AIX"
    if HPUX: return "HP-UX"
    return "Unknown"


# --record-interval, Recording segment length (e.g. 10m, 1h, 3600s; default: 30m)
def parse_time(value):
    if isinstance(value, int): return value
    if not isinstance(value, str):
        raise ValueError(f"invalid interval: {value}")
    s = value.strip().lower()
    if s.isdigit(): return int(s)
    m = re.fullmatch(r"(\d+)([smh])", s)
    if not m:
        raise ValueError(f"invalid interval format: {value}")
    n, unit = int(m.group(1)), m.group(2)
    if unit == "s": return n
    if unit == "m": return n * 60
    if unit == "h": return n * 3600
    raise ValueError(f"unsupported interval unit: {unit}")


_ERR_HEAD = re.compile(r'^\[[^\]]+\]\s*([^:]+)')

def short_err(e) -> str:
    """The first clause of an FFmpeg / PyAV error ('[Errno 5] Input/output error: ...' -> 'Input/output
    error'), for one-line log messages."""
    m = _ERR_HEAD.search(str(e))
    return m.group(1).strip() if m else str(e)


_SECRET_KEYS = r'(?:password|passwd|passphrase|passwort|pwd|psw|pass|streamid|secret|token)'
# userinfo up to the LAST '@' of the authority, as FFmpeg reads it: 'admin:Admin@123@host' is user
# 'admin' with password 'Admin@123'
_RE_USERINFO = re.compile(r'://([^/:\s?#]*):([^/\s?#]*)@')
_RE_SRT_SECRET = re.compile(r'(?i)(?<![a-z0-9_])(streamid|passphrase)=([^&\s\'"]*)')   # SRT: streamid=#!::r=x,m=publish
_RE_QUERY_PWD = re.compile(r'(?i)(?<![a-z0-9_])(' + _SECRET_KEYS + r')=([^&/?#\s\'"]*)')
_RE_XM_PWD = re.compile(r'(?i)(_' + _SECRET_KEYS + r')=([^_&/?#\s\'"]*)')    # XM: user=admin_password=X_channel=1
_RE_DICT_PWD = re.compile(r'(?i)([\'"]-?' + _SECRET_KEYS + r'[\'"]\s*[:,]\s*[\'"])([^\'"]*)')  # {'password': 'x'}, ['-passphrase', 'x']
_RE_OPT_PWD = re.compile(r'(?i)((?<![\w-])-' + _SECRET_KEYS + r'\s+)([^\s\'"]+)')   # --ffmpeg "-passphrase x"
_RE_RTMP_KEY = re.compile(r'(?i)(rtmps?://[^/\s\'"]+(?:/[^/\s\'"?#]+)+/)([^/\s\'"?#]+)')   # rtmp://host/app/KEY
_RE_STREAM_KEY = re.compile(r'(/live2(?:\?[^/\s\'"]*)?/)([^\s\'"?#]+)')   # rtmp://host/live2/KEY, live2?backup=1/KEY
_RE_HLS_CID = re.compile(r'([?&]cid=)([^&\s\'"]+)')              # YouTube HLS ingest key
# applied in this order; group 2 of each is the secret (also what the pre-commit hook looks for)
_SECRET_PATTERNS = (_RE_USERINFO, _RE_SRT_SECRET, _RE_QUERY_PWD, _RE_XM_PWD, _RE_DICT_PWD, _RE_OPT_PWD,
                    _RE_STREAM_KEY, _RE_RTMP_KEY, _RE_HLS_CID)

_RE_URL = re.compile(r'([a-z][a-z0-9+.-]*://)([^\s\'"<>]+)', re.I)

def _mask_userinfo(m):
    """user:password@ up to the LAST '@' of a URL: a password with '@' or '/' in it (unencoded, as
    typed in cameras.ini) has no other visible end."""
    scheme, rest = m.group(1), m.group(2)
    at = rest.rfind("@")
    if at < 0:
        return m.group(0)
    user, colon, pwd = rest[:at].partition(":")
    if not colon or pwd == "***" or "/" in user:
        return m.group(0)
    return f"{scheme}{user}:***{rest[at:]}"

def _star(m):
    s, a, b = m.group(0), m.start(2) - m.start(0), m.end(2) - m.start(0)
    return s[:a] + "***" + s[b:]

def find_secrets(text):
    """The passwords and stream keys mask_url would hide in text (used by .githooks/pre-commit)."""
    return [m.group(2) for pat in _SECRET_PATTERNS for m in pat.finditer(str(text)) if m.group(2)]

def mask_url(url) -> str:
    """Hide passwords and stream keys for logs/overlays; works on whole log lines, not only on bare URLs.
    Handles 'scheme://user:pwd@host' (an '@' inside the password included), password=/pwd=/psw=/
    passphrase=/streamid= in queries and XM-style paths ('&' or '_' separated), dict and argv forms
    ('password': 'x', '-passphrase', 'x'), the last path element of rtmp(s) URLs (the stream key on
    YouTube, Twitch, Facebook, Kick, Restream ...) and YouTube HLS ?cid=."""
    if not url: return url or ""
    s = _RE_URL.sub(_mask_userinfo, str(url))
    for pat in _SECRET_PATTERNS:
        s = pat.sub(_star, s)
    return s

def display_url(url) -> str:
    """Short, credential-free form for the on-screen status overlay."""
    if not url: return ""
    s = str(url)
    if s.isdigit(): return f"camera {s}"
    tail = s.split("@")[-1] if "://" in s else s
    return mask_url(tail)



_ANSI_SEQ = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[ -/]*[0-~]")   # CSI, OSC, other ESC
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")

def printable(text, limit=200):
    """A name as it may appear in a window title, a terminal or a log: terminal escape sequences and
    control characters (newlines included) removed, at most `limit` characters."""
    return _CONTROL_CHARS.sub("", _ANSI_SEQ.sub("", str(text or "")))[:limit].strip()

DRM_ROOT = "/sys/class/drm"


# hardware decoding APIs FFmpeg knows by name, whether the build in use was made with them or not
KNOWN_HWACCELS = ("cuda", "vaapi", "vdpau", "qsv", "d3d11va", "dxva2", "d3d12va", "videotoolbox", "drm",
                  "opencl", "mediacodec", "vulkan", "amf")
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


def pci_gpus(root="/sys/bus/pci/devices"):
    """[(vendor, 'vvvv:dddd', kernel driver)] of the display controllers on the PCI bus.  Unlike drm_gpus
    this does not depend on a driver having made a DRM device: a server whose NVIDIA cards only compute
    has none for them."""
    out = []
    for dev in sorted(glob.glob(os.path.join(root, "*"))):
        try:
            with open(os.path.join(dev, "class")) as f:
                if not f.read().strip().lower().startswith("0x03"):
                    continue
            with open(os.path.join(dev, "vendor")) as f: vendor = f.read().strip().lower()
            with open(os.path.join(dev, "device")) as f: device = f.read().strip().lower()
        except OSError:
            continue
        try:
            driver = os.path.basename(os.readlink(os.path.join(dev, "driver")))
        except OSError:
            driver = "no driver"
        out.append((DRM_VENDORS.get(vendor, vendor), f"{vendor[2:]}:{device[2:]}", driver))
    return out


def accel_obstacle(accel, gpus=None):
    """What stands between --accel and this machine whatever is installed in Python, in words for the log
    and for nova doctor; None when nothing is known to (and always outside Linux, and where the kernel
    shows no display controller at all: a container, an ARM board).  `gpus`: pci_gpus()."""
    if gpus is None:
        gpus = pci_gpus() if LINUX and not WSL else []       # WSL reaches the GPU through /dev/dxg
    if not gpus:
        return None
    a = (accel or "").lower()
    if a in ("cuda", "cuvid", "nvdec"):
        nvidia = [g for g in gpus if g[0] == "NVIDIA"]
        if not nvidia:
            return "this machine has no NVIDIA GPU"
        if all(g[2] == "nouveau" for g in nvidia):
            return ("the NVIDIA card runs on the nouveau driver, and NVIDIA's decoder (NVDEC) comes with "
                    "NVIDIA's own driver only")
    if a == "qsv" and not any(g[0] == "Intel" for g in gpus):
        return "this machine has no Intel GPU, and Quick Sync is Intel's"
    return None


_ERR_LOG = re.compile(r"last error log:\s*(?:\[[^\]]*\]\s*)?(.+)$", re.S)
# FFmpeg's words when a GPU maker's library is not on the machine, and ours for it
_ERR_MEANS = (
    (("Cannot load libnvcuvid", "Failed loading nvcuvid", "Cannot load libcuda", "Could not dynamically load CUDA",
      "Cannot load nvcuvid", "Cannot load nvcuda"), "NVIDIA's driver is not installed, or not its decoding library"),
    (("Cannot load libnvidia-encode", "Cannot load nvEncodeAPI"), "NVIDIA's driver is not installed, or not its encoding library"),
)


def hw_err(e) -> str:
    """short_err for a hardware decoder or encoder that did not open: FFmpeg's own last line belongs to
    it ('Operation not permitted' alone was all the log had of a missing NVIDIA library), and what that
    line means where it is one of the usual ones."""
    text = str(e)
    head = re.sub(r"\s+returned -?\d+$", "", short_err(text.split("; last error log")[0]))
    m = _ERR_LOG.search(text)
    last = " ".join(m.group(1).split()).rstrip(".") if m else ""
    if not last:
        return head
    for needles, meaning in _ERR_MEANS:
        if any(n in last for n in needles):
            return f"{head}: {last} ({meaning})"
    return f"{head}: {last}"


def vaapi_node(gpu_idx=None, root=None):
    """The DRM render node --accel vaapi and the *_vaapi encoders use.  An index (--gpu N, or the launcher's
    NOVA_GPU_DEVICES) names /dev/dri/renderD(128+N).  None, nothing asked for, is the first node that is
    not an NVIDIA card, whose driver has no VAAPI: next to NVIDIA cards the integrated Intel or AMD GPU is
    renderD128 on one machine and renderD129 on the next.  renderD128 when the kernel tells nothing."""
    try:
        if gpu_idx is not None:
            return f"/dev/dri/renderD{128 + int(gpu_idx)}"
    except (TypeError, ValueError):
        pass
    nodes = []
    for p in glob.glob(os.path.join(root or DRM_ROOT, "renderD[0-9]*")):
        try:
            with open(os.path.join(p, "device", "vendor")) as f:
                nodes.append((int(os.path.basename(p)[7:]), f.read().strip().lower()))
        except (OSError, ValueError):
            continue
    for n, vendor in sorted(nodes):
        if vendor != "0x10de":
            return f"/dev/dri/renderD{n}"
    return "/dev/dri/renderD128"


def parse_geom(text):
    """'x,y,w,h' (a --geometry value, a layout entry, a control message) -> (x, y, w, h), w and h
    positive and every value within what a window can have; None for anything else."""
    try:
        x, y, w, h = (int(v) for v in str(text).split(","))
    except (TypeError, ValueError):
        return None
    if not (0 < w <= 100_000 and 0 < h <= 100_000 and -1_000_000 <= x <= 1_000_000 and -1_000_000 <= y <= 1_000_000):
        return None
    return x, y, w, h

def parse_zoom(text):
    """'rx,ry,rw,rh' (a --zoom value, a layout entry, a control message) -> ratios of the picture, each
    finite, the origin inside the picture, the size positive and at most the picture; None otherwise."""
    try:
        rx, ry, rw, rh = (float(v) for v in str(text).split(","))
    except (TypeError, ValueError):
        return None
    if any(v != v or abs(v) == float("inf") for v in (rx, ry, rw, rh)):       # NaN / inf
        return None
    if not (0.0 <= rx < 1.0 and 0.0 <= ry < 1.0 and 0.0 < rw <= 1.0 and 0.0 < rh <= 1.0):
        return None
    return rx, ry, rw, rh

def parse_tile(text):
    """'I/N' (a --tile value: cell I of N, counted from 1) -> (I, N); None for anything else."""
    try:
        i, n = (int(v) for v in str(text).split("/"))
    except (TypeError, ValueError):
        return None
    return (i, n) if 1 <= i <= n <= 10_000 else None


def tile_rect(i, n, area, grid=20, ratio=16 / 9):
    """(x, y, w, h) of cell i of n (from 1, row by row) in a grid of equal windows on area = (x, y, w, h),
    the screen without its panels. The columns are the count that gives the largest windows of the given
    shape; one grid step stays free between neighbours and to the area's edges, a window is at most a
    third of the area high (two cameras do not take over the screen), its height a multiple of the grid
    step as the keyboard resizing keeps it, and the block of cells is centred."""
    ax, ay, aw, ah = area
    gap = g = max(1, int(grid))
    best = None
    for cols in range(1, n + 1):
        rows = -(-n // cols)
        h = min((ah - gap * (rows + 1)) / rows, (aw - gap * (cols + 1)) / cols / ratio)
        if best is None or h > best[0]:
            best = (h, cols, rows)
    h, cols, rows = best
    h = min(h, ah / 3)
    h = max(g, int(h // g) * g) if h >= g else max(1, int(h))       # more windows than the area holds: tiny
    w = max(1, round(h * ratio))
    x0 = ax + max(0, (aw - (cols * w + (cols - 1) * gap)) // 2)
    y0 = ay + max(0, (ah - (rows * h + (rows - 1) * gap)) // 2)
    r, c = divmod(i - 1, cols)
    return x0 + c * (w + gap), y0 + r * (h + gap), w, h


def window_title(name):
    """Title of a camera window: 'Nova - NAME' (NAME as given with -n).  Unique per camera, so the
    taskbar tells the windows apart and compositor rules (nova layout export) can match them."""
    name = printable(name)
    return f"Nova - {name}" if name else "Nova"


def settings_dir():
    """Where Nova keeps its per-window files: the folder Qt gives user settings (~/.config/nova on Linux
    and macOS, %APPDATA%\\nova on Windows); None without PySide6."""
    try:
        from PySide6.QtCore import QSettings
    except ImportError:
        return None
    return os.path.dirname(QSettings(QSettings.IniFormat, QSettings.UserScope, "nova", "nova").fileName())


class WindowStore:
    """A camera window's place and zoom on record: one small INI file per window, named by the hash of the
    camera URL, under <settings_dir>/windows.  One file per window because processes that close at the
    same moment must not wait for one another: QSettings locks its file and retries with sleeps doubling
    from 0.1 s to 5 s, and thirteen windows closing on one shared file took up to 45 s to exit."""
    def __init__(self, base=None):
        self.base = base or settings_dir()
        self.dir = os.path.join(self.base, "windows") if self.base else None

    def path(self, h):
        return os.path.join(self.dir, f"{h}.conf")

    @staticmethod
    def _text(v):
        return None if v in (None, "") else str(v)

    def read(self, h):
        """(geometry, zoom) as 'x,y,w,h' / 'rx,ry,rw,rh' strings, each None when not on record."""
        from PySide6.QtCore import QSettings
        p = self.path(h)
        if not os.path.exists(p):
            return None, None
        s = QSettings(p, QSettings.IniFormat)
        return self._text(s.value("geometry")), self._text(s.value("zoom"))

    def write(self, h, geometry, zoom):
        """Write both values (a None removes that value); the file is created on first use."""
        from PySide6.QtCore import QSettings
        os.makedirs(self.dir, exist_ok=True)
        s = QSettings(self.path(h), QSettings.IniFormat)
        for key, v in (("geometry", geometry), ("zoom", zoom)):
            if v is None: s.remove(key)
            else: s.setValue(key, str(v))
        s.sync()
        return s.status() == QSettings.Status.NoError

    def remove(self, h):
        try: os.remove(self.path(h))
        except OSError: pass

    def hashes(self):
        """The hashes that have a file."""
        try:
            return sorted(f[:-5] for f in os.listdir(self.dir) if f.endswith(".conf"))
        except OSError:
            return []


if __name__ == "__main__":
    print(f'system: {_system}, WSL:{WSL}')
    print(mask_url("rtsp://admin:secret@192.168.1.2:554/user=admin&password=secret&channel=1"))
