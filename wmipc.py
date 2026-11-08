"""Window placement through the compositor's IPC, for compositors that ignore the positions a window asks
for: sway (i3 IPC on $SWAYSOCK) and Hyprland (its request socket). There Nova moves and sizes its own
window by asking the compositor, so a plain start opens at the remembered place, --layout at the layout's,
the arrow keys move the window, and no window rules are needed. Native Wayland and XWayland windows alike:
the window is found by the process id (one window per Nova process).

Placer.for_session() returns None anywhere else (X11, GNOME, KDE, Windows, macOS) and Nova keeps using
Qt's own setGeometry. NOVA_WM_IPC=0 turns it off (sway/Hyprland then tile Nova windows like any other).
"""
import os
import json
import time
import socket
import struct
import logging

FIND_TIMEOUT_S = 5.0        # how long to wait for the compositor to map the window after show()
RECT_TTL_S = 1.0            # a rect read from the compositor is reused this long


class _Sway:
    """i3 IPC: 'i3-ipc' + uint32 length + uint32 type + JSON payload, native byte order."""
    MAGIC = b"i3-ipc"
    RUN_COMMAND, GET_TREE = 0, 4
    name = "sway"

    def __init__(self, path):
        self.path = path

    def _request(self, kind, payload=b""):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(self.path)
            s.sendall(self.MAGIC + struct.pack("=II", len(payload), kind) + payload)
            head = self._recv(s, len(self.MAGIC) + 8)
            n, _ = struct.unpack("=II", head[len(self.MAGIC):])
            return json.loads(self._recv(s, n))

    @staticmethod
    def _recv(s, n):
        buf = b""
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("compositor closed the IPC socket")
            buf += chunk
        return buf

    def command(self, cmd):
        res = self._request(self.RUN_COMMAND, cmd.encode())
        bad = [r.get("error", "?") for r in res if not r.get("success")]
        if bad:
            raise RuntimeError("; ".join(bad))

    def rect(self, pid):
        """(x, y, w, h) of the process's window in layout coordinates, or None while it is not mapped."""
        stack = [self._request(self.GET_TREE)]
        while stack:
            n = stack.pop()
            if n.get("pid") == pid and n.get("type") in ("con", "floating_con"):
                r = n["rect"]
                return r["x"], r["y"], r["width"], r["height"]
            stack += n.get("nodes", []) + n.get("floating_nodes", [])
        return None

    def float_frameless(self, pid):
        self.command(f"[pid={pid}] floating enable, border none")

    def place(self, pid, x, y, w, h):
        self.command(f"[pid={pid}] resize set {w} {h}, move absolute position {x} {y}")


class _Hyprland:
    """Hyprland's request socket: one plain-text request per connection, 'j/' for JSON replies."""
    name = "Hyprland"

    def __init__(self, path):
        self.path = path

    def _request(self, text):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(self.path)
            s.sendall(text.encode())
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return buf.decode(errors="replace")

    def command(self, cmd):
        reply = self._request(f"dispatch {cmd}").strip()
        if reply != "ok":
            raise RuntimeError(reply or "no reply")

    def rect(self, pid):
        for c in json.loads(self._request("j/clients") or "[]"):
            if c.get("pid") == pid and c.get("mapped", True):
                (x, y), (w, h) = c["at"], c["size"]
                return x, y, w, h
        return None

    def float_frameless(self, pid):
        for c in json.loads(self._request("j/clients") or "[]"):
            if c.get("pid") == pid and not c.get("floating"):
                self.command(f"setfloating pid:{pid}")
        self._request(f"dispatch setprop pid:{pid} border_size 0")      # cosmetic: an older keyword may fail

    def place(self, pid, x, y, w, h):
        self.command(f"resizewindowpixel exact {w} {h},pid:{pid}")
        self.command(f"movewindowpixel exact {x} {y},pid:{pid}")


def _backend():
    if os.environ.get("NOVA_WM_IPC", "1") == "0":
        return None
    sway = os.environ.get("SWAYSOCK")
    if sway and os.path.exists(sway):
        return _Sway(sway)
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if sig:
        runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        for base in (os.path.join(runtime, "hypr"), "/tmp/hypr"):
            path = os.path.join(base, sig, ".socket.sock")
            if os.path.exists(path):
                return _Hyprland(path)
    return None


class Placer:
    """Moves and sizes this process's window through the compositor. Until the compositor has mapped
    the window, the last requested geometry waits and is applied once it shows up."""

    def __init__(self, backend):
        self.b = backend
        self.pid = os.getpid()
        self.ready = False
        self._pending = None
        self._rect = None
        self._rect_at = 0.0
        self._failed = False

    @classmethod
    def for_session(cls):
        b = _backend()
        return cls(b) if b else None

    @property
    def name(self):
        return self.b.name

    def try_attach(self):
        """Called from a timer after show(): True once the window is mapped and floating (then the pending
        geometry is applied), False to be called again."""
        try:
            if self.b.rect(self.pid) is None:
                return False
            self.b.float_frameless(self.pid)
            self.ready = True
            if self._pending:
                self._apply(*self._pending)
                self._pending = None
            return True
        except Exception as e:
            self._fail(e)
            return True                 # nothing more to wait for

    def place(self, x, y, w, h):
        g = (int(x), int(y), int(w), int(h))
        if not self.ready:
            self._pending = g
            return
        self._apply(*g)

    def _apply(self, x, y, w, h):
        try:
            self.b.place(self.pid, x, y, w, h)
            self._rect, self._rect_at = (x, y, w, h), time.monotonic()
        except Exception as e:
            self._fail(e)

    def rect(self):
        """The window's (x, y, w, h) as the compositor has it, or None (not mapped, IPC failed)."""
        if not self.ready:
            return None
        now = time.monotonic()
        if self._rect is None or now - self._rect_at > RECT_TTL_S:
            try:
                r = self.b.rect(self.pid)
            except Exception as e:
                self._fail(e)
                return None
            if r is not None:
                self._rect, self._rect_at = r, now
        return self._rect

    def _fail(self, e):
        if not self._failed:
            self._failed = True
            logging.warning(f"[Nova] {self.b.name} IPC: {e}; window placement may not follow")
