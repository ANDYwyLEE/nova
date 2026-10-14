"""NovaCore: the per-camera controller: owns the VideoWorker, the recorder, the streamer and the audio
player, names the camera and its folders, serves the window and the control channel."""
import re
import os
import time
import hashlib
import logging
import threading
from urllib.parse import urlparse
from PySide6.QtCore import QObject, Slot, Signal, QTimer

import logsync  # installs logging.verbose / logging.trace
from videoworker import VideoWorker
from recorder import AVRecorder
from audioplayer import AudioPlayer
from streamer import AVStreamer
from util import printable


def settings_key(url):
    """The key of a camera's window settings (geom_/zoom_ in Nova.conf); cams.py reads them with it."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


class NovaCore(QObject):
    img_sig = Signal()
    stat_sig = Signal(str)
    sz_sig = Signal(int, int)
    exit_sig = Signal()
    init_done_sig = Signal()
    bt_sig = Signal()

    def __init__(self,
                 url,
                 fps,
                 opengl,
                 accel,
                 headless,
                 auto_record,
                 encoder,
                 auto_audio,
                 camera_name,
                 snapshot_dir,
                 recording_dir,
                 record_interval,
                 ffmpeg,
                 target_url,
                 gpu=None,
        ):
        super().__init__()

        self.url = url          # as typed (onvif:// etc. are resolved by the VideoWorker)
        self.headless = headless
        self.last_status = ""     # the latest overlay text (Connecting, Unreachable, ...); "" while playing
        self.url_hash = settings_key(url)

        self.display_name = printable(camera_name)           # for the window title, as typed (minus control characters)
        if camera_name:
            self.camera_name = camera_name
        else:
            try:
                parsed = urlparse(url)
            except ValueError:                  # e.g. an unbalanced '[' (IPv6) in the address
                parsed = urlparse("")
            if url.isdigit():
                self.camera_name = f"camera{url}"
            elif parsed.hostname:
                host = parsed.hostname
                try:
                    port = parsed.port
                except ValueError:              # a password with an unencoded '/', or a bad port
                    port = None
                self.camera_name = f"{host}_{port}" if port else host
            else:
                self.camera_name = "camera"
        # a folder name under videos/: '..' or '.hidden' must not escape or hide it
        self.camera_name = re.sub(r'[^\w.\-]+', '_', self.camera_name).strip(".") or "camera"
        self.display_name = self.display_name or self.camera_name

        self.snapshot_dir = snapshot_dir or "./snapshots"
        os.makedirs(self.snapshot_dir, exist_ok=True)

        self.vid_worker = VideoWorker(url, fps, opengl, accel, headless, ffmpeg, gpu=gpu)
        self.audio_q, self.rec_q, self.stream_q, self.frame_q = self.vid_worker.get_queues()

        self.audio = AudioPlayer(self.audio_q, self.vid_worker)
        self.recorder = AVRecorder(self.camera_name, self.rec_q, self.vid_worker, accel, encoder)
        if recording_dir:
            self.recorder.set_dir(recording_dir)
        if record_interval:
            self.recorder.set_interval(record_interval)

        self.streamer = AVStreamer(target_url, self.stream_q, self.vid_worker, accel, encoder)
        self.pending_auto_streamer = target_url is not None

        self.pending_auto_record = auto_record
        self.pending_auto_audio = auto_audio

        self.vid_worker.img_sig.connect(self._on_img)
        self.vid_worker.stat_sig.connect(self._stat_sig)
        self.vid_worker.sz_sig.connect(self._on_sz)
        self.vid_worker.exit_sig.connect(self._exit)

        self.streamer.stat_sig.connect(self._bt_stat)
        self.recorder.stat_sig.connect(self._bt_stat)
        self.audio.stat_sig.connect(self._bt_stat)

    @Slot(bool)
    def _bt_stat(self, stat):
        logging.trace(f"[NovaCore] button status changed: {stat}")
        self.bt_sig.emit()


    @Slot(int,int)
    def _on_sz(self, w, h):
        self.sz_sig.emit(w, h)
        launched_rec = False
        if self.pending_auto_record:
            self.pending_auto_record = False
            self.toggle_recorder()
            launched_rec = True
        if self.pending_auto_audio:
            self.pending_auto_audio = False
            self.toggle_audio()
        if self.pending_auto_streamer:
            self.pending_auto_streamer = False
            if launched_rec:
                logging.debug("[NovaCore] Delaying auto-streamer to avoid GPU race condition.")
                QTimer.singleShot(500, self._auto_start_streamer)
                return
            self.toggle_streamer()
        self.init_done_sig.emit()

    def _auto_start_streamer(self):
        self.toggle_streamer()
        self.init_done_sig.emit()

    @Slot(str)
    def _stat_sig(self, text):
        self.last_status = text           # for the control channel's status reply
        if self.headless:
            logging.info("[NovaCore] " + re.sub(r'\s+', ' ', text).strip())
        self.stat_sig.emit(text)

    @Slot()
    def _on_img(self):
        self.img_sig.emit()

    @Slot()
    def _exit(self):
        self.exit_sig.emit()


    def start(self):
        self.vid_worker.start()

    def stop(self):
        """Close outputs, then the decoder; False if the decoder thread is still busy (a blocking open)."""
        self.recorder.close()
        self.streamer.close()
        self.audio.close()
        return self.vid_worker.stop() is not False



    def get_url(self): return self.url
    def hash(self): return self.url_hash

    def get_frame_q(self): return self.frame_q

    def width(self): return self.vid_worker.width()
    def height(self): return self.vid_worker.height()
    def ratio(self): return self.vid_worker.ratio()

    # icons of the window's status strip, read by the view
    def get_recorder_icon(self): return self.recorder.get_icon()
    def get_audio_icon(self): return self.audio.get_icon()
    def get_streamer_icon(self): return self.streamer.get_icon()

    def is_recorder_on(self): return self.recorder.is_on()
    def is_audio_on(self): return self.audio.is_on()
    def is_streamer_on(self): return self.streamer.is_on()

    def toggle_recorder(self):
        if self.recorder.is_on():  self.recorder.off()
        else:  self.recorder.on()

    def toggle_audio(self):
        if self.audio.is_on(): self.audio.off()
        else: self.audio.on()

    def toggle_streamer(self):
        if self.streamer.is_on(): self.streamer.off()
        else: self.streamer.on()

    def snapshot(self, df):
        """df: DisplayFrame (or None). Colour conversion uses swscale; PNG encoding runs off the GUI thread."""
        if df is None: return
        try:
            qimg = df.to_qimage()
        except Exception as e:
            logging.error(f"[NovaCore] snapshot conversion failed: {e}")
            return
        if qimg is None or qimg.isNull(): return
        qimg = qimg.copy()   # detach from the frame buffer before leaving the GUI thread
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.snapshot_dir, f"{self.camera_name}_{ts}.png")
        def _save():
            if qimg.save(path):
                logging.info(f"[NovaCore] snapshot saved: {path}")
            else:
                logging.error(f"[NovaCore] snapshot save failed: {path}")
        threading.Thread(target=_save, name="snapshot", daemon=True).start()
