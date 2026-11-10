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
SETTLE_S = 2.0              # how long after the window got its place the compositor's rect is checked against it
SETTLE_TRIES = 5            # at most this many times is the place asked for again
NORMAL, MAXIMIZED, FULLSCREEN = 0, 1, 2     # the window's mode as the compositor has it


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

    def state(self, pid):
        """(x, y, w, h, mode) of the process's window in layout coordinates, or None while it is not mapped.
        sway has no maximized state, only fullscreen. The rect is the container's, which is what `resize set`
        and `move` take: sway's `rect` leaves out a title bar (deco_rect), and the one a window is mapped
        with is still reported for a moment after `border none`."""
        stack = [self._request(self.GET_TREE)]
        while stack:
            n = stack.pop()
            if n.get("pid") == pid and n.get("type") in ("con", "floating_con"):
                r, bar = n["rect"], (n.get("deco_rect") or {}).get("height", 0)
                return (r["x"], r["y"] - bar, r["width"], r["height"] + bar,
                        FULLSCREEN if n.get("fullscreen_mode") else NORMAL)
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

    def _client(self, pid):
        for c in json.loads(self._request("j/clients") or "[]"):
            if c.get("pid") == pid and c.get("mapped", True):
                return c
        return None

    def state(self, pid):
        """(x, y, w, h, mode) from Hyprland's own state (fullscreen 0 none, 1 maximized, 2 fullscreen, 3 both).
        What it tells a native window is no guide: it reports every one of them maximized, floating or not."""
        c = self._client(pid)
        if c is None:
            return None
        (x, y), (w, h) = c["at"], c["size"]
        fs = c.get("fullscreen", 0)
        return x, y, w, h, FULLSCREEN if fs & 2 else MAXIMIZED if fs & 1 else NORMAL

    def float_frameless(self, pid):
        for c in json.loads(self._request("j/clients") or "[]"):
            if c.get("pid") == pid and not c.get("floating"):
                self.command(f"setfloating pid:{pid}")
        self._request(f"dispatch setprop pid:{pid} border_size 0")      # cosmetic: an older keyword may fail

    def place(self, pid, x, y, w, h):
        self.command(f"resizewindowpixel exact {w} {h},pid:{pid}")
        self.command(f"movewindowpixel exact {x} {y},pid:{pid}")


UNKNOWN_PEER = -1           # the socket answers, but this OS cannot say which process serves it


def _peer_pid(path):
    """The pid of the process serving the Unix socket at path, UNKNOWN_PEER without SO_PEERCRED (BSDs),
    None when nothing answers there."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(path)
            opt = getattr(socket, "SO_PEERCRED", None)
            if opt is None:
                return UNKNOWN_PEER
            pid, _uid, _gid = struct.unpack("3i", s.getsockopt(socket.SOL_SOCKET, opt, struct.calcsize("3i")))
            return pid
    except OSError:
        return None


def _serves_this_session(ipc_path):
    """True when the compositor IPC socket answers and belongs to the compositor of this session's Wayland
    display. sway imports SWAYSOCK into systemd's user environment, where it outlives the sway login: a
    GNOME login afterwards had it, and a socket left behind by a crash, or a sway on another VT, must not
    take over the placement of windows on another desktop."""
    ipc = _peer_pid(ipc_path)
    wl = os.environ.get("WAYLAND_DISPLAY")
    if ipc is None or not wl:
        return False
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    display = _peer_pid(wl if os.path.isabs(wl) else os.path.join(runtime, wl))
    return display is not None and (ipc == display or UNKNOWN_PEER in (ipc, display))


def _backend():
    if os.environ.get("NOVA_WM_IPC", "1") == "0":
        return None
    sway = os.environ.get("SWAYSOCK")
    if sway and os.path.exists(sway) and _serves_this_session(sway):
        return _Sway(sway)
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if sig:
        runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        for base in (os.path.join(runtime, "hypr"), "/tmp/hypr"):
            path = os.path.join(base, sig, ".socket.sock")
            if os.path.exists(path) and _serves_this_session(path):
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
        self._rect = None           # (x, y, w, h) now, from the compositor or the last place()
        self._mode = NORMAL
        self._normal = None         # the rect it had when last neither maximized nor fullscreen
        self._read_at = None
        self._failed = False
        self._want = None           # the last rect asked for
        self._ready_at = None
        self._settle_left = SETTLE_TRIES

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
            if self.b.state(self.pid) is None:
                return False
            self.b.float_frameless(self.pid)
            self.ready = True
            self._ready_at = time.monotonic()
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

    def settle(self):
        """Called a few times a second after the window got its first place; True when there is nothing more
        to check. sway and Hyprland map the window tiled before Nova floats it, and sway gives a floating window
        the size of whatever buffer it draws next: a buffer still on its way for a tiled size (two native
        windows starting at once re-tile each other) arrives after the placement and undoes its size, which
        would then be kept on record. So for SETTLE_S the place is asked for again while the compositor has
        another rect for the window in its normal state."""
        if not self.ready or self._failed or self._want is None or self._settle_left <= 0:
            return True
        if time.monotonic() - self._ready_at > SETTLE_S:
            return True
        try:
            st = self.b.state(self.pid)
        except Exception as e:
            self._fail(e)
            return True
        if st is not None and st[4] == NORMAL and tuple(st[:4]) != self._want:
            self._settle_left -= 1
            logging.info(f"[Nova] {self.b.name} has the window at {tuple(st[:4])}, not at {self._want}: placing it again")
            self._apply(*self._want)
        return False

    def _apply(self, x, y, w, h):
        self._want = (x, y, w, h)
        try:
            self.b.place(self.pid, x, y, w, h)
            self._rect = self._normal = (x, y, w, h)
            self._mode, self._read_at = NORMAL, time.monotonic()
        except Exception as e:
            self._fail(e)

    def _read(self):
        """Refresh rect and mode from the compositor at most every RECT_TTL_S; False if it has no answer."""
        if not self.ready:
            return False
        now = time.monotonic()
        if self._read_at is None or now - self._read_at > RECT_TTL_S:
            self._read_at = now
            try:
                st = self.b.state(self.pid)
            except Exception as e:
                self._fail(e)
                st = None
            if st is None:
                self._rect, self._mode = None, NORMAL
            else:
                self._rect, self._mode = st[:4], st[4]
                if self._mode == NORMAL:
                    self._normal = self._rect
        return self._rect is not None

    def rect(self):
        """The window's (x, y, w, h) as the compositor has it, or None (not mapped, IPC failed)."""
        return self._rect if self._read() else None

    def normal_rect(self):
        """Where the window was when last neither maximized nor fullscreen (by the compositor's keys too):
        the place to keep on record. None until known."""
        self._read()
        return self._normal

    def maximized(self):
        """True while the compositor has the window maximized; False before it is mapped or when IPC fails.
        Where Nova places the window this, not Qt's window state, says whether it is maximized: Hyprland
        tells every native window it is, and an XWayland window it maximizes is never told."""
        return self._read() and self._mode == MAXIMIZED

    def _fail(self, e):
        if not self._failed:
            self._failed = True
            logging.warning(f"[Nova] {self.b.name} IPC: {e}; window placement may not follow")
