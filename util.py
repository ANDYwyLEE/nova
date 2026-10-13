import os
import re
import sys
import inspect
import platform
import logging
from pprint import pprint
from urllib.parse import urlparse


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
    except: pass
    return "arm" in _machine or "aarch64" in _machine

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


# RTSP URL to normal filename
def parse_url(s):
    buf = ""
    for c in s:
        if c.isalnum(): buf += c
        elif c == '.': buf += '.'
        else: buf += '_'
    return buf


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
    s = str(url)
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


class Util():
    @staticmethod
    def ppmethods(obj):
        methods = {}
        for attr_name in dir(obj):
            attr_value = getattr(obj, attr_name)
            if callable(attr_value):
                try:
                    sig = inspect.signature(attr_value)
                    methods[attr_name] = [
                        p.name for p in sig.parameters.values()
                        if p.kind == p.POSITIONAL_OR_KEYWORD
                    ]
                except ValueError:
                    pass
        pprint(methods)

    @staticmethod
    def ppvars(obj):
        for name, value in vars(obj).items():
            print(f"{name}: {value}")


def window_title(name):
    """Title of a camera window: 'Nova - NAME' (NAME as given with -n).  Unique per camera, so the
    taskbar tells the windows apart and compositor rules (nova layout export) can match them."""
    name = (name or "").strip()
    return f"Nova - {name}" if name else "Nova"


if __name__ == "__main__":
    print(f'system: {_system}, WSL:{WSL}')
    print(mask_url("rtsp://admin:secret@192.168.1.2:554/user=admin&password=secret&channel=1"))
