import os
import re
import sys
import inspect
import platform
import logging
from pprint import pprint


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


if __name__ == "__main__":
    print(f'system: {_system}, WSL:{WSL}')
