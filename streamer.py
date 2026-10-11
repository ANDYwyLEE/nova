# streamer.py
import logging
import traceback
from player import Player
from recworker import AVRecWorker
from avwriter import AVWriter
from util import mask_url
from PySide6.QtCore import Slot, Signal

class AVStreamer(Player):
    stat_sig = Signal(bool)

    def __init__(self, target_url, stream_q, vid_worker, accel, encoder):
        super().__init__("AVStreamer", "strm.png")
        self.target_url = target_url # rtmp://..., srt://..., or path/to/file.mp4
        self.q = stream_q
        self.vid_worker = vid_worker
        self.accel = accel
        self.encoder = encoder

        self._on = False
        self.worker = None
        self.writer = None

    @Slot(str)
    def _handle_worker_error(self, err_msg):
        logging.error(f"[AVStreamer] worker stopped: {err_msg}")
        if self._on: self.off()

    #@abstractmethod
    def available(self):
        return bool(self.target_url)

    #@abstractmethod
    def is_on(self):
        return self._on

    #@abstractmethod
    def is_off(self):
        return not self.is_on()

    #@abstractmethod
    def on(self):
        if self.is_on(): return
        if not self.target_url: return
        if self.vid_worker.width() <= 0 or self.vid_worker.height() <= 0:
            logging.warning("[AVStreamer] no video yet; cannot start streaming")
            return
        logging.info(f"[AVStreamer] starting stream to: {mask_url(self.target_url)}")
        self._drain(self.q)
        self._on = True
        self.worker = AVRecWorker(self, self.q)
        self.worker.exit_sig.connect(self._handle_worker_error)
        self.worker.start()
        self.vid_worker.set_stream_on(True)
        self.stat_sig.emit(True)

    #@abstractmethod
    def off(self):
        if self.is_off(): return
        self._on = False
        self.vid_worker.set_stream_on(False)
        if self.worker:
            self.worker.stop()
            self.worker.wait()
            self.worker = None
        self.stat_sig.emit(False)
        logging.info(f"[AVStreamer] stopped stream to: {mask_url(self.target_url)}")

    #@abstractmethod
    def close(self):
        self.off()

    # ---- called on the worker thread ------------------------------------------------
    def _open_writer(self):
        vw = self.vid_worker
        ff = vw.get_ffopts()
        self.writer = AVWriter(
            accel=self.accel,
            path=self.target_url, width=vw.width(), height=vw.height(), fps=vw.get_fps(),
            encoder=self.encoder or ff.v_encoder,
            source_codec=vw.get_stream_codec(),
            enc_v_opts=ff.enc_v,
            enc_a_opts=ff.enc_a,
            mux_opts=ff.mux,
            sample_rate=vw.get_sample_rate(), channels=vw.get_channels(),
            audio_enable=not ff.no_audio,
            gpu_idx=vw.get_gpu_idx(),
            v_tag=ff.v_tag, a_encoder=ff.a_encoder,
            vf=ff.vf, metadata=ff.metadata,
            live=True,
        )

    def _close_writer(self):
        if self.writer:
            w, self.writer = self.writer, None
            w.close()

    def write(self, typ, frame):
        if not self._on or not self.writer: return
        if typ == 1:
            self.writer.write_video(frame)
        else:
            self.writer.write_audio(frame)
