"""Platform checks, password and stream-key masking for logs and terminals, small parsers shared by the
launcher and the camera (geometry, zoom, intervals), and short forms of FFmpeg's error texts."""
import os
import re
import platform


_system = platform.system().lower()
_machine = platform.machine().lower()

LINUX = (_system == "linux")
WINDOWS = (_system == "windows")
MAC = (_system == "darwin")
SOLARIS = (_system in ["sunos", "solaris"])
AIX = (_system == "aix")
HPUX = (_system == "hp-ux")

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

def window_title(name):
    """Title of a camera window: 'Nova - NAME' (NAME as given with -n).  Unique per camera, so the
    taskbar tells the windows apart and compositor rules (nova layout export) can match them."""
    name = printable(name)
    return f"Nova - {name}" if name else "Nova"


def settings_dir():
    """Where Nova keeps its per-window files: $NOVA_SETTINGS_DIR, else the folder Qt gives user settings
    (~/.config/nova on Linux and macOS, %APPDATA%\\nova on Windows); None without PySide6."""
    d = os.environ.get("NOVA_SETTINGS_DIR")
    if d:
        return os.path.expanduser(d)
    try:
        from PySide6.QtCore import QSettings
    except ImportError:
        return None
    return os.path.dirname(QSettings(QSettings.IniFormat, QSettings.UserScope, "nova", "nova").fileName())


class WindowStore:
    """A camera window's place and zoom on record: one small INI file per window, named by the hash of the
    camera URL, under <settings_dir>/windows.  One file per window because processes that close at the
    same moment must not wait for one another: QSettings locks its file and retries with sleeps doubling
    from 0.1 s to 5 s, and thirteen windows closing on one shared file took up to 45 s to exit.  A window
    without a file of its own is looked up in the former shared file (QSettings 'nova'/'nova': nova.conf,
    keys geom_<hash> / zoom_<hash>), so places survive the change."""
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
        if os.path.exists(p):
            s = QSettings(p, QSettings.IniFormat)
            return self._text(s.value("geometry")), self._text(s.value("zoom"))
        legacy = QSettings("nova", "nova")
        return self._text(legacy.value(f"geom_{h}")), self._text(legacy.value(f"zoom_{h}"))

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
