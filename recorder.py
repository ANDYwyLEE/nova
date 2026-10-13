# recorder.py
import os, time, logging, traceback
from PySide6.QtCore import Signal, Slot, QTimer
from player import Player
from recworker import AVRecWorker, ErrorLog, RETRY_DELAYS, STABLE_AFTER
from avwriter import AVWriter, OutputOpenError

DEFAULT_DIR = "./videos"
DEFAULT_INTERVAL_S = 1800

class AVRecorder(Player):
    stat_sig = Signal(bool)

    def __init__(self, url_name, rec_q, vid_worker, accel, encoder):
        super().__init__("AVRecorder", "rec.png")
        self.url_name = url_name
        self.q = rec_q
        self.vid_worker = vid_worker
        self.accel = accel
        self.encoder = encoder

        self.dir = DEFAULT_DIR
        self.ext = ".mp4"
        self.interval_s = DEFAULT_INTERVAL_S

        self.flag = False
        self.worker = None
        self.writer = None
        self.errlog = ErrorLog(self.name)

        # A recording that fails while switched on (disk full, encoder or camera trouble) is restarted
        # after 5, 10, 20, 40, then every 60 s; meanwhile it stays "on" with a gray icon. It used to
        # switch itself off for good, with nothing to show for it (a headless camera kept decoding).
        self._retries = 0
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._restart)

        self.start_ts = 0
        self.next_ts = 0
        self.last_now = 0
        self.path = ""

        self.set_dir(self.dir)

    def set_dir(self, d):
        self.dir = d
        os.makedirs(self.dir, exist_ok=True)

    def set_interval(self, sec):
        self.interval_s = max(60, int(sec))

    #@abstractmethod
    def available(self):
        return True

    #@abstractmethod
    def is_on(self):
        return self.flag

    #@abstractmethod
    def is_off(self):
        return not self.is_on()

    def is_waiting(self):
        """Switched on, but the recording failed and the next attempt is pending."""
        return self.flag and self._retry_timer.isActive()

    def state(self):
        """'off', 'on', or 'waiting' (switched on, restart pending). Cheap, for the GUI thread."""
        if not self.flag: return "off"
        return "waiting" if self.is_waiting() else "on"

    def get_icon(self):
        return self.icon_gray if self.is_waiting() else super().get_icon()

    #@abstractmethod
    def on(self):
        if self.is_on(): return
        if self.vid_worker.width() <= 0 or self.vid_worker.height() <= 0:
            logging.warning("[AVRecorder] no video yet; cannot start recording")
            return
        self.flag = True
        self._retries = 0
        self._start_worker()
        self.stat_sig.emit(True)
        logging.info(f"[AVRecorder] starting: {self.url_name}, interval={self.interval_s}s")

    #@abstractmethod
    def off(self):
        if self.is_off(): return
        self.flag = False
        self._retry_timer.stop()
        self._stop_worker()
        self.stat_sig.emit(False)
        logging.info(f"[AVRecorder] stopped: {self.url_name}")

    #@abstractmethod
    def close(self):
        self.off()

    def _start_worker(self):
        self._drain(self.q)
        self.worker = AVRecWorker(self, self.q)
        self.worker.exit_sig.connect(self._on_worker_exit)
        self.worker.start()
        self.vid_worker.set_rec_on(True)

    def _stop_worker(self):
        self.vid_worker.set_rec_on(False)
        if self.worker:
            self.worker.stop()
            self.worker.wait()
            self.worker = None

    @Slot(str)
    def _on_worker_exit(self, err_msg):
        w = self.worker
        if not self.flag or w is None or w.failure is None:
            return                          # a worker that was stopped or replaced in the meantime
        ran = w.ran                         # since its first muxed packet
        self._stop_worker()
        if ran >= STABLE_AFTER: self._retries = 0
        delay = RETRY_DELAYS[min(self._retries, len(RETRY_DELAYS) - 1)]
        self._retries += 1
        logging.error(f"[AVRecorder] recording failed: {err_msg}")
        logging.warning(f"[AVRecorder] restarting in {delay} s (attempt {self._retries})")
        self._retry_timer.start(delay * 1000)
        self.stat_sig.emit(True)

    @Slot()
    def _restart(self):
        if not self.flag: return
        logging.info(f"[AVRecorder] restarting: {self.url_name}")
        self._start_worker()
        self.stat_sig.emit(True)

    # ---- called on the worker thread ------------------------------------------------
    def _open_writer(self):
        """The segment opens on the first video frame, at that frame's size: the decoder's
        width()/height() read 0 between a reconnect and its next frame."""
        self.writer = None
        self.last_now = 0

    def _output_live(self):
        w = self.writer
        return w is not None and w.first_packet_sent

    def _close_writer(self):
        self._close_and_save_segment(int(time.time()), reason="stop")

    def write(self, typ, frame):
        if not self.flag: return

        now = time.time()
        if typ == 1:                            # segments start with a video frame, at its size
            if self.writer is None:
                self._start_segment(now, frame, aligned=True)
                logging.info(f"[AVRecorder] recording: {self.path}, next={int(self.next_ts)}")
            elif self._time_jump(now):
                logging.debug(f"[AVRecorder] time-jump: {self.url_name}, last={int(self.last_now)}, now={int(now)}")
                self._rollover("time-jump", now, end_ts=int(now), frame=frame)
            elif now >= self.next_ts:
                self._rollover("interval", now, end_ts=int(self.next_ts), frame=frame)
        elif self.writer is None or self._time_jump(now):
            return                              # audio waits for the video frame that opens the segment
        self.last_now = now

        if typ == 1:
            self.writer.write_video(frame)
        else:
            self.writer.write_audio(frame)

    def _rollover(self, reason, now, end_ts, frame):
        old_writer = self.writer
        old_path = self.path
        old_start = self.start_ts

        self._start_segment(now, frame, aligned=True)   # open the new segment first: no frames lost

        try:
            old_writer.close()
        except Exception:
            logging.error(f"[AVRecorder] close failed: {self.url_name}\n{traceback.format_exc()}")

        self._save_file(old_path, old_start, end_ts, old_writer)
        logging.debug(f"[AVRecorder] chunked: {self.url_name}, reason={reason}, "
                      f"{int(old_start)}->{int(end_ts)}, next={int(self.next_ts)}")

    def _start_segment(self, now, frame, aligned):
        self.start_ts = int(now)
        self.next_ts = self._next_boundary(now) if aligned else now + self.interval_s
        self.last_now = now

        sub = os.path.join(self.dir, self.url_name)
        self.path = os.path.join(sub, f"{self.url_name}_output{self.ext}")
        vw = self.vid_worker
        ff = vw.get_ffopts()
        try:
            os.makedirs(sub, exist_ok=True)
            self.writer = AVWriter(
                    self.accel,
                    path=self.path, width=frame.width, height=frame.height, fps=vw.get_fps(),
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
                    live=False,
                )
        except OutputOpenError:
            raise
        except Exception as e:
            raise OutputOpenError(f"cannot start a segment: {e}") from e

    def _close_and_save_segment(self, end_ts, reason=""):
        if not self.writer:
            return
        writer, path, start = self.writer, self.path, self.start_ts
        self.writer = None
        try:
            writer.close()
        except Exception:
            logging.error(f"[AVRecorder] close failed: {self.url_name}\n{traceback.format_exc()}")
        self._save_file(path, start, end_ts, writer)
        if reason:
            logging.debug(f"[AVRecorder] saved({reason}): {self.url_name}, {int(start)}->{int(end_ts)}")

    def _save_file(self, src_path, start_ts, end_ts, writer):
        if not os.path.lexists(src_path):
            return
        if not writer.first_packet_sent:
            # nothing reached the file (no frames, or its header write failed): no recording in it.
            # A segment with audio only (video failing) is kept.
            try: os.remove(src_path)
            except OSError: pass
            logging.warning(f"[AVRecorder] empty segment discarded: {src_path}")
            return
        frames = writer.v_frames if writer.v_frames else f"0 video / {writer.a_frames} audio"
        s = time.strftime("%Y%m%d_%H%M", time.localtime(start_ts))
        e = time.strftime("%H%M", time.localtime(end_ts))
        base = os.path.join(os.path.dirname(src_path), f"{s}_{e}")
        dst = base + self.ext
        n = 1
        while os.path.exists(dst):              # never overwrite an earlier segment
            dst = f"{base}_{n}{self.ext}"; n += 1
        os.replace(src_path, dst)
        logging.info(f"[AVRecorder] saved: {dst} ({frames} frames)")

    def _next_boundary(self, now):
        i = self.interval_s
        return (int(now) // i + 1) * i

    def _time_jump(self, now):
        if not self.last_now:
            return False
        jump = max(120, self.interval_s * 2)
        return (now - self.last_now) > jump or (self.last_now - now) > jump
