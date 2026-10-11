# recorder.py
import os, time, logging, traceback
from PySide6.QtCore import Signal, Slot
from player import Player
from recworker import AVRecWorker
from avwriter import AVWriter

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

    #@abstractmethod
    def on(self):
        if self.is_on(): return
        if self.vid_worker.width() <= 0 or self.vid_worker.height() <= 0:
            logging.warning("[AVRecorder] no video yet; cannot start recording")
            return
        self._drain(self.q)
        self.flag = True
        self.worker = AVRecWorker(self, self.q)
        self.worker.exit_sig.connect(self._on_worker_exit)
        self.worker.start()
        self.vid_worker.set_rec_on(True)
        self.stat_sig.emit(True)
        logging.info(f"[AVRecorder] starting: {self.url_name}, interval={self.interval_s}s")

    #@abstractmethod
    def off(self):
        if self.is_off(): return
        self.vid_worker.set_rec_on(False)
        self.flag = False
        if self.worker:
            self.worker.stop()
            self.worker.wait()
            self.worker = None
        self.stat_sig.emit(False)
        logging.info(f"[AVRecorder] stopped: {self.url_name}")

    #@abstractmethod
    def close(self):
        self.off()

    @Slot(str)
    def _on_worker_exit(self, err_msg):
        logging.error(f"[AVRecorder] worker stopped: {err_msg}")
        if self.flag: self.off()

    # ---- called on the worker thread ------------------------------------------------
    def _open_writer(self):
        self._start_segment(time.time(), aligned=True)
        logging.info(f"[AVRecorder] recording: {self.path}, next={int(self.next_ts)}")

    def _close_writer(self):
        self._close_and_save_segment(int(time.time()), reason="stop")

    def write(self, typ, frame):
        if not self.flag or not self.writer: return

        now = time.time()
        if self._time_jump(now):
            logging.debug(f"[AVRecorder] time-jump: {self.url_name}, last={int(self.last_now)}, now={int(now)}")
            self._rollover("time-jump", now, end_ts=int(now))
        elif now >= self.next_ts:
            self._rollover("interval", now, end_ts=int(self.next_ts))
        self.last_now = now

        if typ == 1:
            self.writer.write_video(frame)
        else:
            self.writer.write_audio(frame)

    def _rollover(self, reason, now, end_ts):
        old_writer = self.writer
        old_path = self.path
        old_start = self.start_ts

        self._start_segment(now, aligned=True)   # open the new segment first: no frames lost

        try:
            old_writer.close()
        except Exception:
            logging.error(f"[AVRecorder] close failed: {self.url_name}\n{traceback.format_exc()}")

        self._save_file(old_path, old_start, end_ts, old_writer.frames_written)
        logging.debug(f"[AVRecorder] chunked: {self.url_name}, reason={reason}, "
                      f"{int(old_start)}->{int(end_ts)}, next={int(self.next_ts)}")

    def _start_segment(self, now, aligned):
        self.start_ts = int(now)
        self.next_ts = self._next_boundary(now) if aligned else now + self.interval_s
        self.last_now = now

        sub = os.path.join(self.dir, self.url_name)
        os.makedirs(sub, exist_ok=True)

        self.path = os.path.join(sub, f"{self.url_name}_output{self.ext}")
        vw = self.vid_worker
        ff = vw.get_ffopts()
        self.writer = AVWriter(
                    self.accel,
                    path=self.path, width=vw.width(), height=vw.height(), fps=vw.get_fps(),
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

    def _close_and_save_segment(self, end_ts, reason=""):
        if not self.writer:
            return
        writer, path, start = self.writer, self.path, self.start_ts
        self.writer = None
        try:
            writer.close()
        except Exception:
            logging.error(f"[AVRecorder] close failed: {self.url_name}\n{traceback.format_exc()}")
        self._save_file(path, start, end_ts, writer.frames_written)
        if reason:
            logging.debug(f"[AVRecorder] saved({reason}): {self.url_name}, {int(start)}->{int(end_ts)}")

    def _save_file(self, src_path, start_ts, end_ts, frames):
        if not os.path.exists(src_path):
            return
        if frames <= 0:
            try: os.remove(src_path)
            except OSError: pass
            logging.warning(f"[AVRecorder] empty segment discarded: {src_path}")
            return
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
