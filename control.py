# control.py
"""A camera's control channel: `nova stop`, `nova status` and `nova layout apply` talk to the running
camera through it instead of signals, which also works on Windows, where Ctrl-Break only reaches
processes on the caller's console.

The launcher passes the address (--control): a Unix socket in the user's private runtime directory or
a named pipe, both accessible to this user only. One JSON line in, one JSON line out:
  {"cmd": "status"}  -> {"ok": true, "pid": ..., "video": "1920x1080", "record": "on", ...}
  {"cmd": "stop"}    -> {"ok": true}, then the camera quits as on Ctrl-C (state and recordings saved)
  {"cmd": "place", "geometry": "x,y,w,h", "zoom": "rx,ry,rw,rh"} -> moves the window"""
import os
import json
import time
import logging

from PySide6.QtCore import QObject, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket

FRESH_FRAME_S = 3.0      # a frame this recent means the camera is playing, whatever the overlay said last


def _player_state(p):
    if p is None or (hasattr(p, "available") and not p.available()):
        return "n/a"
    if hasattr(p, "state"):
        return p.state()
    return "on" if p.is_on() else "off"


class ControlServer(QObject):
    def __init__(self, address, camera_id, core, view, quit_fn):
        super().__init__()
        self.address, self.camera_id = address, camera_id
        self.core, self.view, self.quit_fn = core, view, quit_fn
        self.started = time.time()
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.UserAccessOption)
        self.server.newConnection.connect(self._accept)
        self._buffers = {}

    def listen(self):
        """Start listening; False if another live process already serves this camera."""
        probe = QLocalSocket()
        probe.connectToServer(self.address)
        if probe.waitForConnected(300):
            probe.abort()
            logging.warning(f"[Control] another Nova already runs camera {self.camera_id!r}; "
                            f"this one has no control channel")
            return False
        if os.path.isabs(self.address):
            os.makedirs(os.path.dirname(self.address), mode=0o700, exist_ok=True)
        QLocalServer.removeServer(self.address)          # a socket left by a crashed run
        if not self.server.listen(self.address):
            logging.warning(f"[Control] cannot listen on {self.address}: {self.server.errorString()}")
            return False
        logging.debug(f"[Control] listening on {self.address}")
        return True

    def close(self):
        self.server.close()

    # ------------------------------------------------------------------ connections
    def _accept(self):
        while self.server.hasPendingConnections():
            s = self.server.nextPendingConnection()
            self._buffers[s] = b""
            s.readyRead.connect(lambda s=s: self._read(s))
            s.disconnected.connect(lambda s=s: self._drop(s))

    def _drop(self, s):
        self._buffers.pop(s, None)
        s.deleteLater()

    def _read(self, s):
        buf = self._buffers.get(s, b"") + bytes(s.readAll())
        if b"\n" not in buf:
            self._buffers[s] = buf[-65536:]
            return
        line = buf.split(b"\n", 1)[0]
        try:
            reply = self.handle(json.loads(line.decode("utf-8")))
        except Exception as e:
            reply = {"ok": False, "error": str(e)}
        s.write((json.dumps(reply) + "\n").encode("utf-8"))
        s.flush()
        s.disconnectFromServer()

    # ------------------------------------------------------------------ requests
    def handle(self, req):
        cmd = req.get("cmd") if isinstance(req, dict) else None
        if cmd == "status":
            return {"ok": True, **self.status()}
        if cmd == "stop":
            logging.info("[Control] stop requested")
            QTimer.singleShot(0, self.quit_fn)
            return {"ok": True}
        if cmd == "place":
            return self.place(req.get("geometry"), req.get("zoom"))
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    def status(self):
        core = self.core
        w, h = core.width(), core.height()
        if self.view is not None:
            # the overlay as shown: the first frame of a connection clears it
            text = getattr(self.view, "render_error", None) or self.view.curr_txt or ""
            playing = not text
        else:
            last_frame = getattr(core.vid_worker, "last_frame_time", 0.0) or 0.0
            playing = bool(last_frame) and time.monotonic() - last_frame < FRESH_FRAME_S
            text = "" if playing else (getattr(core, "last_status", "") or "")
        st = {
            "pid": os.getpid(), "camera": self.camera_id, "name": core.display_name,
            "uptime": int(time.time() - self.started), "window": self.view is not None,
            "video": f"{w}x{h}" if w > 0 and h > 0 else None,
            "playing": playing,
            "status": text,
            "record": _player_state(getattr(core, "recorder", None)),
            "stream": _player_state(getattr(core, "streamer", None)),
            "audio": "on" if getattr(core, "audio", None) and core.audio.is_on() else "off",
        }
        if self.view is not None:
            g = self.view.geometry()
            st["geometry"] = f"{g.x()},{g.y()},{g.width()},{g.height()}"
            st["fullscreen"] = bool(self.view.is_fullscreen())
        return st

    def place(self, geometry, zoom):
        if self.view is None:
            return {"ok": False, "error": "headless: no window to place"}
        if not hasattr(self.view, "place"):
            return {"ok": False, "error": "this window cannot be placed"}
        self.view.place(geometry, zoom)
        return {"ok": True}
