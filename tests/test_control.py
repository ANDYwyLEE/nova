"""The control channel: control.py serving, cams.control() asking (a Unix socket, a named pipe on Windows)."""
import os
import threading
import time
import uuid

import pytest

pytest.importorskip("PySide6.QtNetwork")
from PySide6.QtCore import QTimer                     # noqa: E402
from PySide6.QtGui import QGuiApplication               # noqa: E402

import cams                                             # noqa: E402
from control import ControlServer                      # noqa: E402


class Player:
    def __init__(self, state): self._state = state
    def available(self): return True
    def state(self): return self._state
    def is_on(self): return self._state == "on"


class Worker:
    last_frame_time = 0.0


class Core:
    display_name = "Front door"
    camera_name = "Front_door"
    last_status = "Unreachable:\n10.0.0.9:554"
    vid_worker = Worker()
    recorder, streamer, audio = Player("on"), Player("waiting"), Player("off")
    def width(self): return 1920
    def height(self): return 1080


class View:
    placed = None
    curr_txt = "Unreachable:\n10.0.0.9:554"
    def place(self, geometry, zoom): View.placed = (geometry, zoom)
    def geometry(self):
        from PySide6.QtCore import QRect
        return QRect(10, 20, 640, 360)
    def is_fullscreen(self): return False


@pytest.fixture
def app():
    # a Gui application, as other tests in this session paint text (a core one would abort them)
    return QGuiApplication.instance() or QGuiApplication([])


@pytest.fixture
def address(tmp_path, monkeypatch):
    if os.name == "nt":
        return f"nova-test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    return cams.control_address("cam1")


def ask_all(app, server, requests):
    """Send requests one by one from a thread while the Qt loop serves them."""
    replies = []

    def client():
        for r in requests:
            replies.append(cams.control(server.address, r))
        QTimer.singleShot(0, app.quit)

    QTimer.singleShot(0, lambda: threading.Thread(target=client, daemon=True).start())
    QTimer.singleShot(10000, app.quit)                  # never hang the suite
    app.exec()
    return replies


def test_status_place_stop_and_errors(app, address):
    quits = []
    view = View()
    srv = ControlServer(address, "cam1", Core(), view, lambda: quits.append(1))
    assert srv.listen()
    try:
        status, place, bad, stop = ask_all(app, srv, [
            {"cmd": "status"}, {"cmd": "place", "geometry": "10,20,640,360", "zoom": "0,0,1,1"},
            {"cmd": "dance"}, {"cmd": "stop"}])
    finally:
        srv.close()
    assert status["ok"], status
    assert status["camera"] == "cam1" and status["video"] == "1920x1080" and status["geometry"] == "10,20,640,360"
    assert (status["record"], status["stream"], status["audio"]) == ("on", "waiting", "off")
    assert status["status"].startswith("Unreachable")          # no frame yet: the overlay text counts
    assert place == {"ok": True} and View.placed == ("10,20,640,360", "0,0,1,1")
    assert bad["ok"] is False and "dance" in bad["error"]
    assert stop == {"ok": True}
    app.processEvents(); time.sleep(0.05); app.processEvents()
    assert quits == [1]


def test_status_line_and_no_server(address):
    assert cams.control(address, {"cmd": "status"}) is None       # nothing listening
    assert cams.control(None, {"cmd": "status"}) is None
    line = cams.live_state({"ok": True, "video": "1920x1080", "record": "on", "stream": "waiting",
                            "status": ""})
    assert line == "video 1920x1080, record on, stream waiting"
    assert cams.live_state({"ok": True, "video": None, "record": "off", "stream": "n/a",
                            "status": "Unreachable:\n10.0.0.9:554"}) == "no video yet, Unreachable: 10.0.0.9:554"


def test_status_carries_what_runs_in_place_of_what_was_asked_for(app, address):
    """--accel that is not in use and an encoder that is not the one named: under the camera in
    `nova status`, whatever its -l (the log's lines about them are warnings)."""
    class Core2(Core):
        vid_worker = type("W", (), {"last_frame_time": 0.0, "accel_note": lambda self:
                                    "--accel v4l2m2m is not in use (h264_v4l2m2m has not answered for 5 s): decoded on the CPU"})()
        recorder = type("R", (Player,), {"encoder_note": lambda self: "libx264 on the CPU in place of -e h264_v4l2m2m"})("on")
        streamer = type("S", (Player,), {"encoder_note": lambda self: None})("off")

    server = ControlServer(address, "cam1", Core2(), None, lambda: None)
    try:
        st = server.status()
        assert st["notes"] == ["--accel v4l2m2m is not in use (h264_v4l2m2m has not answered for 5 s): decoded on the CPU",
                               "recording with libx264 on the CPU in place of -e h264_v4l2m2m"]
        assert cams.status_notes(dict(st, ok=True)) == st["notes"]
        assert ControlServer.status(type("X", (), {"core": Core(), "view": None, "camera_id": "c", "started": 0.0,
                                                   "_notes": ControlServer._notes})())["notes"] == []   # nothing to say
    finally:
        server.server.close()
    assert cams.status_notes(None) == [] and cams.status_notes({"ok": True}) == []
    assert cams.status_notes({"ok": True, "notes": ["a\n  b", ""]}) == ["a b"]


def test_second_instance_does_not_steal_the_channel(app, address):
    first = ControlServer(address, "cam1", Core(), None, lambda: None)
    assert first.listen()
    try:
        second = ControlServer(address, "cam1", Core(), None, lambda: None)
        assert not second.listen()
        (status,) = ask_all(app, first, [{"cmd": "status"}])
        assert status["ok"] and status["window"] is False
    finally:
        first.close()


def test_drop_of_a_socket_already_deleted(app):
    """On Windows a pipe connection still open at exit is destroyed with the server, and its destructor
    emits disconnected: _drop then met a deleted QLocalSocket and logged an uncaught RuntimeError after
    "Nova exited gracefully" (seen in every window's log on a Windows runner)."""
    import shiboken6
    from PySide6.QtNetwork import QLocalSocket
    srv = ControlServer("nova-test-unused", "cam1", Core(), None, lambda: None)
    s = QLocalSocket()
    srv._buffers[s] = b""
    shiboken6.delete(s)
    srv._drop(s)
    assert s not in srv._buffers


@pytest.mark.skipif(os.name == "nt", reason="a named pipe needs no directory")
def test_a_runtime_directory_of_another_user_does_not_end_the_camera(app, tmp_path, monkeypatch, caplog):
    """`su`, `sudo -u` and `runuser` keep XDG_RUNTIME_DIR, another user's /run/user/N: the camera died at
    start on PermissionError when it made its socket there (seen as root's directory under WSL)."""
    import logging
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "not-there"))
    a = cams.control_address("cam1")                      # the launcher looks elsewhere ...
    assert not a.startswith(str(tmp_path)) and f"nova-{os.getuid()}" in a
    blocker = tmp_path / "file"
    blocker.write_text("")                                # ... and a camera that cannot make the directory runs on
    s = ControlServer(str(blocker / "nova" / "cam1.sock"), "cam1", Core(), View(), lambda: None)
    with caplog.at_level(logging.WARNING):
        assert s.listen() is False
    assert "no control channel" in caplog.text
