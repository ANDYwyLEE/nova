import os, time, logging, traceback
from PySide6.QtCore import Signal
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
        now = time.time()
        self._start_segment(now, aligned=True)
        self.worker = AVRecWorker(self, self.q)
        self.worker.start()
        self.vid_worker.set_rec_on(True)
        self.flag = True
        self.stat_sig.emit(True)
        logging.info(f"[AVRecorder] started: {self.url_name}, interval={self.interval_s}s, next={int(self.next_ts)}")

    #@abstractmethod
    def off(self):
        if self.is_off(): return
        self.vid_worker.set_rec_on(False)
        if self.worker:
            self.worker.stop()
            self.worker.wait()
            self.worker = None
        now = time.time()
        self._close_and_save_segment(int(now), reason="stop")
        self.flag = False
        self.stat_sig.emit(False)
        logging.debug(f"[AVRecorder] stopped: {self.url_name}")

    #@abstractmethod
    def close(self):
        self.off()

    def write(self, typ, frame):
        if not self._on or not self.writer: return

        now = time.time()

        if self._time_jump(now):
            logging.debug(f"[AVRecorder] time-jump: {self.url_name}, last={int(self.last_now)}, now={int(now)}")
            self._rollover("time-jump", now, end_ts=int(now))

        elif now >= self.next_ts:
            self._rollover("interval", now, end_ts=int(self.next_ts))

        self.last_now = now

        try:
            if typ == 1:
                self.writer.write_video(frame)
            else:
                self.writer.write_audio(frame)
        except Exception:
            logging.error(f"[AVRecorder] write failed: {self.url_name}\n{traceback.format_exc()}")

    def _rollover(self, reason, now, end_ts):
        old_writer = self.writer
        old_path = self.path
        old_start = self.start_ts
        old_next = self.next_ts

        self._start_segment(now, aligned=True)

        try:
            old_writer.close()
        except Exception:
            logging.error(f"[AVRecorder] close failed: {self.url_name}\n{traceback.format_exc()}")

        self._save_file(old_path, old_start, end_ts)

        logging.debug(
            f"[AVRecorder] chunked: {self.url_name}, reason={reason}, "
            f"{int(old_start)}->{int(end_ts)}, next={int(self.next_ts)}"
        )

    def _start_segment(self, now, aligned):
        self.start_ts = int(now)
        self.next_ts = self._next_boundary(now) if aligned else now + self.interval_s
        self.last_now = now

        sub = os.path.join(self.dir, self.url_name)
        os.makedirs(sub, exist_ok=True)

        self.path = os.path.join(sub, f"{self.url_name}_output{self.ext}")
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
                    self.accel,
                    path=self.path, width=w, height=h, fps=fps,
                    encoder=self.encoder,
                    source_codec=source_codec,
                    open_opts=open_opts,
                    enc_v_opts=enc_v_opts,
                    enc_a_opts=enc_a_opts,
                    sample_rate=sr, channels=ch, audio_enable=True,
                    gpu_idx=gpu_idx
                )

    def _close_and_save_segment(self, end_ts, reason=""):
        if not self.writer:
            return
        path = self.path
        start = self.start_ts
        try:
            self.writer.close()
        except Exception:
            logging.error(f"[AVRecorder] close failed: {self.url_name}\n{traceback.format_exc()}")
        self.writer = None
        self._save_file(path, start, end_ts)
        if reason:
            logging.debug(f"[AVRecorder] saved({reason}): {self.url_name}, {int(start)}->{int(end_ts)}")

    def _save_file(self, src_path, start_ts, end_ts):
        #s = time.strftime("%Y%m%d_%H%M%S", time.localtime(start_ts))
        #e = time.strftime("%Y%m%d_%H%M%S", time.localtime(end_ts))
        s = time.strftime("%Y%m%d_%H%M", time.localtime(start_ts))
        e = time.strftime("%H%M", time.localtime(end_ts))
        dst = os.path.join(os.path.dirname(src_path), f"{s}_{e}{self.ext}")
        if os.path.exists(src_path):
            os.replace(src_path, dst)
        logging.info(f"[AVRecorder] saved: {dst}")

    def _next_boundary(self, now):
        i = self.interval_s
        return (int(now) // i + 1) * i

    def _time_jump(self, now):
        if not self.last_now:
            return False
        jump = max(120, self.interval_s * 2)
        return (now - self.last_now) > jump or (self.last_now - now) > jump

