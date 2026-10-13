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
