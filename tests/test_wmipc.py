"""wmipc: the sway (i3 IPC) and Hyprland request-socket clients, against fake compositor sockets."""
import os
import json
import shutil
import socket
import struct
import tempfile
import threading
from pathlib import Path

import pytest

import wmipc

# sway and Hyprland speak over Unix sockets: nothing to test where Python has none (Windows)
pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no Unix sockets on this platform")


@pytest.fixture
def tmp_path():
    """A short directory for the sockets: a socket path holds at most 104 bytes on macOS, and pytest's own
    tmp_path there (/private/var/folders/...) is longer than that."""
    d = tempfile.mkdtemp(prefix="nv", dir="/tmp" if os.path.isdir("/tmp") else None)
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


def _serve(path, handle, requests):
    """A Unix socket server answering `requests` connections with handle(conn) in a thread."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(8)

    def run():
        for _ in range(requests):
            c, _ = srv.accept()
            with c:
                handle(c)
        srv.close()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _sway_tree(pid):
    return {"type": "root", "nodes": [{"type": "output", "nodes": [{"type": "workspace", "nodes": [],
            "floating_nodes": [{"type": "floating_con", "pid": pid, "fullscreen_mode": 0,
                                "rect": {"x": 220, "y": 600, "width": 391, "height": 220}}]}]}]}


def test_sway_rect_and_commands(tmp_path):
    path = str(tmp_path / "sway.sock")
    seen = []
    pid = os.getpid()

    def handle(c):
        head = c.recv(14)
        n, kind = struct.unpack("=II", head[6:])
        payload = c.recv(n) if n else b""
        seen.append((kind, payload.decode()))
        reply = json.dumps(_sway_tree(pid) if kind == 4 else [{"success": True}]).encode()
        c.sendall(b"i3-ipc" + struct.pack("=II", len(reply), kind) + reply)

    t = _serve(path, handle, 4)
    p = wmipc.Placer(wmipc._Sway(path))
    p.place(10, 20, 300, 200)                     # before the window is mapped: kept for later
    assert p.ready is False and seen == []
    assert p.try_attach() is True and p.ready     # GET_TREE, floating + border, then the pending place
    assert p.rect() == (10, 20, 300, 200)         # the place just applied, within RECT_TTL_S
    t.join(5)
    assert seen[0] == (4, "")
    assert seen[1] == (0, f"[pid={pid}] floating enable, border none")
    assert seen[2] == (0, f"[pid={pid}] resize set 300 200, move absolute position 10 20")


def test_sway_command_error_is_logged_once(tmp_path, caplog):
    path = str(tmp_path / "sway.sock")

    def handle(c):
        head = c.recv(14)
        n, kind = struct.unpack("=II", head[6:])
        c.recv(n)
        reply = json.dumps([{"success": False, "error": "No matching node"}]).encode()
        c.sendall(b"i3-ipc" + struct.pack("=II", len(reply), kind) + reply)

    t = _serve(path, handle, 2)
    p = wmipc.Placer(wmipc._Sway(path))
    p.ready = True
    p.place(1, 2, 3, 4)
    p.place(1, 2, 3, 4)
    t.join(5)
    assert sum("No matching node" in r.message for r in caplog.records) == 1


def test_hyprland_rect_and_commands(tmp_path):
    path = str(tmp_path / ".socket.sock")
    seen = []
    pid = os.getpid()
    clients = [{"pid": 1, "at": [0, 0], "size": [5, 5], "fullscreen": 1},
               {"pid": pid, "mapped": True, "floating": False, "at": [3660, 60], "size": [320, 180], "fullscreen": 0}]

    def handle(c):
        req = c.recv(4096).decode()
        seen.append(req)
        c.sendall(json.dumps(clients).encode() if req == "j/clients" else b"[]" if req == "j/monitors" else b"ok")

    t = _serve(path, handle, 11)
    b = wmipc._Hyprland(path)
    assert b.state(pid) == (3660, 60, 320, 180, wmipc.NORMAL)   # Hyprland's own state, not what it tells the window
    clients[1]["fullscreen"] = 1
    assert b.state(pid)[4] == wmipc.MAXIMIZED
    clients[1]["fullscreen"] = 2
    assert b.state(pid)[4] == wmipc.FULLSCREEN
    clients[1]["fullscreen"] = 3                   # maximized and fullscreen on top: fullscreen
    assert b.state(pid)[4] == wmipc.FULLSCREEN
    clients[1]["fullscreen"] = 0
    b.float_frameless(pid)                         # clients, setfloating, setprop
    b.place(pid, 3660, 60, 320, 180)
    t.join(5)
    assert seen[5] == f"dispatch setfloating pid:{pid}"
    assert seen[-2:] == [f"dispatch resizewindowpixel exact 320 180,pid:{pid}",
                         f"dispatch movewindowpixel exact 3660 60,pid:{pid}"]


def _listening(path):
    """A Unix socket that accepts connections (this process serves it), closed by the caller."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(8)
    return srv


def test_backend_selection(tmp_path, monkeypatch):
    for k in ("SWAYSOCK", "HYPRLAND_INSTANCE_SIGNATURE", "NOVA_WM_IPC", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert wmipc.Placer.for_session() is None
    wl, sway = _listening(tmp_path / "wayland-1"), _listening(tmp_path / "sway.sock")
    try:
        monkeypatch.setenv("SWAYSOCK", str(tmp_path / "sway.sock"))
        assert wmipc.Placer.for_session() is None          # no Wayland display: not a sway session
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
        assert isinstance(wmipc.Placer.for_session().b, wmipc._Sway)   # one process serves both sockets
        monkeypatch.setenv("NOVA_WM_IPC", "0")
        assert wmipc.Placer.for_session() is None
        monkeypatch.delenv("NOVA_WM_IPC")
        monkeypatch.delenv("SWAYSOCK")
        monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "abc")
        (tmp_path / "hypr" / "abc").mkdir(parents=True)
        hypr = _listening(tmp_path / "hypr" / "abc" / ".socket.sock")
        assert isinstance(wmipc.Placer.for_session().b, wmipc._Hyprland)
        hypr.close()
    finally:
        wl.close()
        sway.close()


def test_a_compositor_ipc_left_over_from_another_session_is_not_used(tmp_path, monkeypatch):
    """A GNOME login after a sway one still had SWAYSOCK (sway imports it into systemd's user environment):
    a dead socket file, or a sway alive on another VT, must not place this session's windows."""
    for k in ("HYPRLAND_INSTANCE_SIGNATURE", "NOVA_WM_IPC"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("SWAYSOCK", str(tmp_path / "sway.sock"))
    wl = _listening(tmp_path / "wayland-0")
    try:
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(str(tmp_path / "sway.sock"))
        dead.close()                                       # the file stays, nobody answers
        assert os.path.exists(tmp_path / "sway.sock")
        assert wmipc.Placer.for_session() is None
        os.unlink(tmp_path / "sway.sock")
        other = _listening(tmp_path / "sway.sock")
        pids = {"sway.sock": 4242, "wayland-0": 1111}       # the peers as SO_PEERCRED reports them (Linux)
        monkeypatch.setattr(wmipc, "_peer_pid", lambda p: pids[os.path.basename(p)])
        assert wmipc.Placer.for_session() is None          # a sway, but not this session's compositor
        pids["sway.sock"] = 1111
        assert isinstance(wmipc.Placer.for_session().b, wmipc._Sway)
        pids["sway.sock"] = wmipc.UNKNOWN_PEER             # no SO_PEERCRED (macOS, BSDs): answering is enough
        assert isinstance(wmipc.Placer.for_session().b, wmipc._Sway)
        other.close()
    finally:
        wl.close()



def test_placer_keeps_the_last_normal_rect_and_fails_quietly(caplog, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(wmipc.time, "monotonic", lambda: clock[0])
    answers = []

    class B:
        name = "fake"
        calls = 0
        def state(self, pid):
            B.calls += 1
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a
    p = wmipc.Placer(B())
    assert p.maximized() is False and p.normal_rect() is None and B.calls == 0   # not mapped: no IPC at all
    p.ready = True
    answers += [(10, 20, 300, 200, wmipc.NORMAL), (20, 20, 3400, 1400, wmipc.MAXIMIZED),
                (0, 0, 3440, 1440, wmipc.FULLSCREEN), ConnectionError("gone"), ConnectionError("gone")]
    assert p.rect() == (10, 20, 300, 200) and p.maximized() is False and B.calls == 1   # one read per RECT_TTL_S
    clock[0] += 2
    assert p.maximized() is True and p.rect() == (20, 20, 3400, 1400)
    assert p.normal_rect() == (10, 20, 300, 200)
    clock[0] += 2
    assert p.maximized() is False and p.normal_rect() == (10, 20, 300, 200)
    clock[0] += 2
    assert p.rect() is None and p.maximized() is False and p.normal_rect() == (10, 20, 300, 200)
    clock[0] += 2
    p.rect()
    assert sum("gone" in r.message for r in caplog.records) == 1


def test_sway_rect_is_the_container_with_its_title_bar(tmp_path):
    """sway's `rect` leaves out a title bar (deco_rect) while `resize set` and `move` take the container's box,
    and for a moment after `border none` the tree still reports the bar the window was mapped with: read as it
    was, a window just placed at 400,700 640x360 looked like 400,725 640x335."""
    path = str(tmp_path / "sway.sock")
    pid = os.getpid()
    tree = _sway_tree(pid)
    con = tree["nodes"][0]["nodes"][0]["floating_nodes"][0]
    con["rect"] = {"x": 400, "y": 725, "width": 640, "height": 335}
    con["deco_rect"] = {"x": 400, "y": 677, "width": 640, "height": 25}

    def handle(c):
        head = c.recv(14)
        n, kind = struct.unpack("=II", head[6:])
        if n:
            c.recv(n)
        reply = json.dumps(tree).encode()
        c.sendall(b"i3-ipc" + struct.pack("=II", len(reply), kind) + reply)

    t = _serve(path, handle, 1)
    assert wmipc._Sway(path).state(pid) == (400, 700, 640, 360, wmipc.NORMAL)
    t.join(5)


def test_placer_asks_again_while_the_compositor_has_another_rect(monkeypatch, caplog):
    """Two native Wayland windows started at once in sway: each is mapped tiled and re-tiled by the other, and a
    buffer drawn for a tiled size reached sway after Nova's placement, which made it the floating window's size
    (3436x1390 instead of 640x360, in 2 of 5 starts); that size was then kept on record. For SETTLE_S after the
    first place the compositor's rect is checked and the place asked for again."""
    clock = [50.0]
    monkeypatch.setattr(wmipc.time, "monotonic", lambda: clock[0])
    caplog.set_level("INFO")

    class B:
        name = "fake"
        def __init__(self):
            self.now, self.mode, self.placed, self.reads = None, wmipc.NORMAL, [], 0
        def state(self, pid):
            self.reads += 1
            return (*self.now, self.mode) if self.now else None
        def float_frameless(self, pid): pass
        def place(self, pid, x, y, w, h):
            self.placed.append((x, y, w, h))
            self.now = (x, y, w, h)
    b = B()
    p = wmipc.Placer(b)
    assert p.settle() is True and b.reads == 0            # not mapped: nothing to check, no IPC
    p.place(400, 700, 640, 360)
    b.now = (0, 23, 1720, 1417)                           # mapped, tiled
    assert p.try_attach() is True and b.placed == [(400, 700, 640, 360)]
    assert p.settle() is False and len(b.placed) == 1     # as asked: only checked
    b.now = (400, 700, 3436, 1390)                        # the late buffer of the tiled size
    clock[0] += 0.1
    assert p.settle() is False and b.placed == [(400, 700, 640, 360)] * 2
    assert sum("placing it again" in r.message for r in caplog.records) == 1
    assert p.rect() == (400, 700, 640, 360) and p.normal_rect() == (400, 700, 640, 360)
    b.now, b.mode = (0, 0, 3440, 1440), wmipc.FULLSCREEN  # the user's own fullscreen is not a wrong place
    clock[0] += 0.1
    assert p.settle() is False and len(b.placed) == 2
    b.mode = wmipc.NORMAL
    p.place(500, 500, 800, 450)                           # a key or `nova layout apply` meanwhile: that is the place now
    b.now = (500, 500, 640, 360)
    clock[0] += 0.1
    assert p.settle() is False and b.placed[-1] == (500, 500, 800, 450) and len(b.placed) == 4
    clock[0] += wmipc.SETTLE_S                            # later the window is the user's to resize with the compositor
    b.now = (500, 500, 300, 200)
    reads = b.reads
    assert p.settle() is True and len(b.placed) == 4 and b.reads == reads

    b2 = B()
    q = wmipc.Placer(b2)                                  # a compositor that will not give the size: a few tries, no more
    b2.place = lambda pid, x, y, w, h: b2.placed.append((x, y, w, h))
    q.place(0, 0, 9000, 9000)
    b2.now = (0, 0, 100, 100)
    q.try_attach()
    done = [q.settle() for _ in range(wmipc.SETTLE_TRIES + 2)]
    assert len(b2.placed) == 1 + wmipc.SETTLE_TRIES and done[-1] is True



def test_hyprland_window_joins_the_monitor_it_is_placed_on(tmp_path):
    """Two monitors: a window mapped on the left one's workspace and moved to the right monitor with
    movewindowpixel stayed on that workspace: F and Hyprland's own fullscreen filled the left monitor, and the
    window went away when the left monitor changed workspace. It now joins the active workspace of the monitor
    its centre is placed on; movetoworkspacesilent shifts a floating window by the monitors' distance, so the
    exact move comes after it."""
    path = str(tmp_path / ".socket.sock")
    seen = []
    pid = os.getpid()
    client = {"pid": pid, "mapped": True, "floating": True, "at": [220, 600], "size": [391, 220], "fullscreen": 0,
              "monitor": 0, "workspace": {"id": 1}}
    monitors = [{"id": 0, "x": 0, "y": 0, "width": 3440, "height": 1440, "scale": 1.0, "transform": 0, "activeWorkspace": {"id": 1}},
                {"id": 1, "x": 3440, "y": 0, "width": 3840, "height": 2160, "scale": 1.5, "transform": 1, "activeWorkspace": {"id": 7}}]

    def handle(c):
        req = c.recv(4096).decode()
        seen.append(req)
        c.sendall(json.dumps([client]).encode() if req == "j/clients" else json.dumps(monitors).encode() if req == "j/monitors"
                  else b"no such window" if "movetoworkspacesilent 1," in req else b"ok")

    t = _serve(path, handle, 18)
    b = wmipc._Hyprland(path)
    b.place(pid, 4000, 60, 266, 200)               # centre 4133,160: on the right monitor
    assert seen == ["j/monitors", "j/clients", f"dispatch movetoworkspacesilent 7,pid:{pid}",
                    f"dispatch resizewindowpixel exact 266 200,pid:{pid}", f"dispatch movewindowpixel exact 4000 60,pid:{pid}"]
    client["monitor"] = 1
    seen.clear()
    b.place(pid, 4020, 60, 266, 200)               # already there: on whatever workspace of that monitor it is
    assert [r for r in seen if "workspace" in r] == []
    seen.clear()
    b.place(pid, 3300, 700, 400, 225)              # straddling: its centre (3500) is still on the right monitor
    assert [r for r in seen if "workspace" in r] == []
    seen.clear()
    b.place(pid, 3200, 700, 400, 225)              # centre 3400: the left monitor; the move failing does not stop the placement
    assert seen[2:] == [f"dispatch movetoworkspacesilent 1,pid:{pid}", f"dispatch resizewindowpixel exact 400 225,pid:{pid}",
                        f"dispatch movewindowpixel exact 3200 700,pid:{pid}"]
    t.join(5)
    # the right monitor is 3840x2160 at scale 1.5 turned a quarter: 1440x2560 in the layout
    assert wmipc._on_monitor(monitors[1], 3440 + 1439, 2559) and not wmipc._on_monitor(monitors[1], 3440 + 1440, 100)
    assert not wmipc._on_monitor(monitors[1], 3440 + 100, 2560) and not wmipc._on_monitor(monitors[0], 3440, 0)
