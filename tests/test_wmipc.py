"""wmipc: the sway (i3 IPC) and Hyprland request-socket clients, against fake compositor sockets."""
import os
import json
import socket
import struct
import threading

import wmipc


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
        c.sendall(json.dumps(clients).encode() if req == "j/clients" else b"ok")

    t = _serve(path, handle, 9)
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


def test_backend_selection(tmp_path, monkeypatch):
    for k in ("SWAYSOCK", "HYPRLAND_INSTANCE_SIGNATURE", "NOVA_WM_IPC"):
        monkeypatch.delenv(k, raising=False)
    assert wmipc.Placer.for_session() is None
    sock = tmp_path / "sway.sock"
    sock.touch()
    monkeypatch.setenv("SWAYSOCK", str(sock))
    assert isinstance(wmipc.Placer.for_session().b, wmipc._Sway)
    monkeypatch.setenv("NOVA_WM_IPC", "0")
    assert wmipc.Placer.for_session() is None
    monkeypatch.delenv("NOVA_WM_IPC")
    monkeypatch.delenv("SWAYSOCK")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "abc")
    (tmp_path / "hypr" / "abc").mkdir(parents=True)
    (tmp_path / "hypr" / "abc" / ".socket.sock").touch()
    assert isinstance(wmipc.Placer.for_session().b, wmipc._Hyprland)


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
