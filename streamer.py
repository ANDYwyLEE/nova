# streamer.py
import logging
from player import Player
from recworker import AVRecWorker, ErrorLog, RETRY_DELAYS, STABLE_AFTER
from avwriter import AVWriter, OutputOpenError, is_network_target
from util import mask_url
from PySide6.QtCore import Slot, Signal, QTimer

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
        self.errlog = ErrorLog(self.name)

        # A network stream that fails while it is switched on is reopened after a delay; meanwhile
        # it stays "on" with a gray icon. A file target is not reopened (that would overwrite it).
        self._retries = 0
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._reconnect)

    @Slot(str)
    def _handle_worker_error(self, err_msg):
        w = self.worker
        if not self._on or w is None or w.failure is None:
            return                          # a worker that was stopped or replaced in the meantime
        logging.error(f"[AVStreamer] worker stopped: {err_msg}")
        if not is_network_target(self.target_url):
            self.off()
            return
        ran = w.ran                         # since the first packet reached the server, not since start
        self._stop_worker()
        if ran >= STABLE_AFTER: self._retries = 0
        delay = RETRY_DELAYS[min(self._retries, len(RETRY_DELAYS) - 1)]
        self._retries += 1
        logging.warning(f"[AVStreamer] reconnecting in {delay} s (attempt {self._retries})")
        self._retry_timer.start(delay * 1000)
        self.stat_sig.emit(True)

    @Slot()
    def _reconnect(self):
        if not self._on: return
        logging.info(f"[AVStreamer] reconnecting to: {mask_url(self.target_url)}")
        self._start_worker()
        self.stat_sig.emit(True)

    def is_waiting(self):
        """Switched on, but the connection failed and the next attempt is pending."""
        return self._retry_timer.isActive()

    def state(self):
        """'off', 'on', or 'waiting' (switched on, reconnect pending). Cheap, for the GUI thread."""
        if not self._on: return "off"
        return "waiting" if self.is_waiting() else "on"

    def get_icon(self):
        return self.icon_gray if self.is_waiting() else super().get_icon()

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
        self._on = True
        self._retries = 0
        self._start_worker()
        self.stat_sig.emit(True)

    #@abstractmethod
    def off(self):
        if self.is_off(): return
        self._on = False
        self._retry_timer.stop()
        self._stop_worker()
        self.stat_sig.emit(False)
        logging.info(f"[AVStreamer] stopped stream to: {mask_url(self.target_url)}")

    #@abstractmethod
    def close(self):
        self.off()

    def _start_worker(self):
        self._drain(self.q)
        self.worker = AVRecWorker(self, self.q)
        self.worker.exit_sig.connect(self._handle_worker_error)
        self.worker.start()
        self.vid_worker.set_stream_on(True)

    def _stop_worker(self):
        self.vid_worker.set_stream_on(False)
        if self.worker:
            self.worker.stop()
            self.worker.wait()
            self.worker = None

    # ---- called on the worker thread ------------------------------------------------
    def _open_writer(self):
        """The writer opens on the first video frame, at that frame's size: the decoder's
        width()/height() read 0 between a reconnect and its next frame."""
        self.writer = None

    def _new_writer(self, frame):
        vw = self.vid_worker
        ff = vw.get_ffopts()
        try:
            return AVWriter(
                accel=self.accel,
                path=self.target_url, width=frame.width, height=frame.height, fps=vw.get_fps(),
                encoder=self.encoder or ff.v_encoder,
                source_codec=vw.get_stream_codec(),
                enc_v_opts=ff.enc_v,
                enc_a_opts=ff.enc_a,
                mux_opts=ff.mux,
                sample_rate=vw.get_sample_rate(), channels=vw.get_channels(),
                audio_enable=not ff.no_audio,
                audio_wait=0 if getattr(vw, 'has_audio', None) is False else None,
                gpu_idx=vw.get_gpu_idx(),
                v_tag=ff.v_tag, a_encoder=ff.a_encoder,
                vf=ff.vf, metadata=ff.metadata,
                live=True,
            )
        except OutputOpenError:
            raise
        except Exception as e:
            raise OutputOpenError(f"cannot start the stream: {e}") from e

    def _output_live(self):
        w = self.writer
        return w is not None and w.first_packet_sent

    def _close_writer(self):
        if self.writer:
            w, self.writer = self.writer, None
            w.close()

    def write(self, typ, frame):
        if not self._on: return
        if self.writer is None:
            if typ != 1: return             # the stream starts with a video frame
            self.writer = self._new_writer(frame)
        if typ == 1:
            self.writer.write_video(frame)
        else:
            self.writer.write_audio(frame)
