# novacore.py
import re
import time
import hashlib
import numpy as np
from urllib.parse import urlparse
from PySide6.QtCore import QObject, Slot, Signal, QTimer
from PySide6.QtGui import QImage
import os, time, logging
from videoworker import VideoWorker
from recorder import AVRecorder
from audioplayer import AudioPlayer
from streamer import AVStreamer


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
        ):
        super().__init__()

        self.url = url
        self.headless = headless
        self.url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]

        if camera_name:
            self.camera_name = camera_name
        else:
            parsed = urlparse(url)
            if url.isdigit():
                self.camera_name = url
            elif parsed.hostname:
                host = parsed.hostname
                port = parsed.port
                self.camera_name = f"{host}_{port}" if port else host
            else:
                self.camera_name = "camera"

        self.snapshot_dir = snapshot_dir or "./snapshots"
        os.makedirs(self.snapshot_dir, exist_ok=True)

        self.vid_worker = VideoWorker(url, fps, opengl, accel, headless, ffmpeg)
        self.audio_q, self.rec_q, self.stream_q, self.frame_q = self.vid_worker.get_queues()

        self.audio = AudioPlayer(self.audio_q)
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
        self.recorder.close()
        self.streamer.close()
        self.audio.close()
        self.vid_worker.stop()



    def get_url(self): return self.url
    def hash(self): return self.url_hash

    def get_frame_q(self): return self.frame_q

    def width(self): return self.vid_worker.width()
    def height(self): return self.vid_worker.height()
    def ratio(self): return self.vid_worker.ratio()

    # TODO move to GUI
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

    def snapshot(self, data):
        if data is None: return
        final_qimg = None

        if isinstance(data, tuple) and data[0] == "nv12":
            try:
                _, (y_plane, uv_plane, w, h) = data
                u = uv_plane[:, :, 0]
                v = uv_plane[:, :, 1]
                u_full = np.repeat(np.repeat(u, 2, axis=0), 2, axis=1)
                v_full = np.repeat(np.repeat(v, 2, axis=0), 2, axis=1)
                y = y_plane.astype(float) - 16
                u = u_full.astype(float) - 128
                v = v_full.astype(float) - 128
                r = (1.164 * y + 1.596 * v).clip(0, 255).astype(np.uint8)
                g = (1.164 * y - 0.392 * u - 0.813 * v).clip(0, 255).astype(np.uint8)
                b = (1.164 * y + 2.017 * u).clip(0, 255).astype(np.uint8)
                rgba = np.dstack((r, g, b, np.full((h, w), 255, dtype=np.uint8)))
                final_qimg = QImage(rgba.data, w, h, QImage.Format_RGBA8888).copy()
                logging.debug(f"[NovaCore] NV12 converted to QImage (Manual Numpy) - Size: {w}x{h}")
            except Exception as e:
                logging.error(f"[NovaCore] NV12 conversion failed: {e}")
                return

        elif isinstance(data, tuple) and data[0] == "rgba":
            _, qimg = data
            if qimg.isNull(): return
            final_qimg = qimg

        if final_qimg:
            ts = time.strftime("%Y%m%d_%H%M%S")
            name = f"{self.camera_name}_{ts}.png"
            path = os.path.join(self.snapshot_dir, name)
            final_qimg.save(path)
            logging.info(f"[NovaCore] snapshot saved: {path}")
