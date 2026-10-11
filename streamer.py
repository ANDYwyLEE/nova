import logging
import traceback
from player import Player
from recworker import AVRecWorker
from avwriter import AVWriter
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
        logging.error(f"[AVStreamer] Worker reported error: {err_msg}")
        self.off()

    #@abstractmethod
    def available(self):
        return True
        
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
        try:
            logging.info(f"[AVStreamer] Starting stream to: {self.target_url}")
            fps = self.vid_worker.get_fps()
            w = self.vid_worker.width()
            h = self.vid_worker.height()
            sr = self.vid_worker.get_sample_rate()
            ch = self.vid_worker.get_channels()
            gpu_idx = self.vid_worker.get_gpu_idx()
            source_codec = self.vid_worker.get_stream_codec()
            open_opts = self.vid_worker.get_open_opts()
            enc_v_opts, enc_a_opts = self.vid_worker.get_encoder_opts()

            self.writer = AVWriter(
                accel=self.accel,
                path=self.target_url, width=w, height=h, fps=fps,
                encoder=self.encoder,
                source_codec=source_codec,
                open_opts=open_opts,
                enc_v_opts=enc_v_opts,
                enc_a_opts=enc_a_opts,
                sample_rate=sr, channels=ch, audio_enable=True,
                gpu_idx=gpu_idx
            )
            self.worker = AVRecWorker(self, self.q)
            self.worker.exit_sig.connect(self._handle_worker_error)
            self.worker.start()
            self.vid_worker.set_stream_on(True)
            self._on = True
            self.stat_sig.emit(True)
        except Exception:
            logging.error(f"[AVStreamer] Failed to start: {traceback.format_exc()}")

    #@abstractmethod
    def off(self):
        if self.is_off(): return
        self._on = False
        self.stat_sig.emit(False)
        self.vid_worker.set_stream_on(False)
        if self.worker:
            self.worker.stop()
            self.worker.wait()
            self.worker = None
        if self.writer:
            self.writer.close()
            self.writer = None
        logging.info(f"[AVStreamer] Stoped stream to: {self.target_url}")
        
    #@abstractmethod
    def close(self):
        self.off()

    def write(self, typ, frame):
        if not self._on or not self.writer: return
        if typ == 1:
            self.writer.write_video(frame)
        else:
            self.writer.write_audio(frame)

