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
            "floating_nodes": [{"type": "floating_con", "pid": pid, "rect": {"x": 220, "y": 600, "width": 391, "height": 220}}]}]}]}


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
    clients = [{"pid": 1, "at": [0, 0], "size": [5, 5]},
               {"pid": pid, "mapped": True, "floating": False, "at": [3660, 60], "size": [320, 180]}]

    def handle(c):
        req = c.recv(4096).decode()
        seen.append(req)
        c.sendall(json.dumps(clients).encode() if req == "j/clients" else b"ok")

    t = _serve(path, handle, 6)
    b = wmipc._Hyprland(path)
    assert b.rect(pid) == (3660, 60, 320, 180)
    b.float_frameless(pid)                         # clients, setfloating, setprop
    b.place(pid, 3660, 60, 320, 180)
    t.join(5)
    assert seen[2] == f"dispatch setfloating pid:{pid}"
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
