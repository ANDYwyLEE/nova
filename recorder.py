"""Recording to segment files under videos/NAME: the folder lock, segments rotated by --record-interval
on local time, crash leftovers kept as *_recovered.mp4, retries after a failure."""
import os, time, errno, logging, traceback
import av
from PySide6.QtCore import Signal, Slot, QTimer
from player import Player
from recworker import AVRecWorker, ErrorLog, WorkerRefused, RETRY_DELAYS, STABLE_AFTER
from avwriter import AVWriter, OutputOpenError

try: import fcntl
except ImportError: fcntl = None
try: import msvcrt
except ImportError: msvcrt = None

DEFAULT_DIR = "./videos"
DEFAULT_INTERVAL_S = 1800
LOCK_NAME = ".nova-recording.lock"
GAP_S = 60                  # a stop this long after the last frame ends the segment at that frame


def _min_interval():
    """60 s; tests set NOVA_MIN_RECORD_INTERVAL to get segments of a few seconds."""
    try:
        return max(1, int(os.getenv("NOVA_MIN_RECORD_INTERVAL", "60")))
    except ValueError:
        return 60


def _gmtoff(t):
    return time.localtime(t).tm_gmtoff


def _on_boundary(w, interval):
    """w: local wall time as seconds since the epoch (UTC offset added)."""
    if interval >= 86400:
        return w % 86400 == 0 and (w // 86400) % (interval // 86400) == 0
    return (w % 86400) % interval == 0


def next_boundary(now, interval):
    """The first segment boundary after now, on the local wall clock: the time of day is a multiple of
    interval counted from local midnight (a day or more: local midnight every N days). UTC alignment
    cut 24h files at 01:00/02:00 in Central Europe and 1h files at :30 in +5:30 zones. Offset changes
    are followed: on the fall-back night 1h segments start at 02:00 twice, on the spring-forward night
    03:00 follows 01:00."""
    t = int(now)
    while True:
        off = _gmtoff(t)
        w = t + off
        if interval >= 86400:
            days = interval // 86400
            nxt = ((w // 86400) // days + 1) * days * 86400
        else:
            day = w - w % 86400
            nxt = min(day + ((w - day) // interval + 1) * interval, day + 86400)
        cand = nxt - off
        if _gmtoff(cand) == off:
            return cand
        lo, hi = t, cand                    # the offset changes in between: find its first second
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if _gmtoff(mid) == off: lo = mid
            else: hi = mid
        if _on_boundary(hi + _gmtoff(hi), interval):
            return hi
        t = hi


class DirLock:
    """Advisory lock on a camera's recording folder, held while it records, so two Nova processes
    never write the same segment file (`nova run X` while `nova start` already records X). The OS
    drops it when the process dies: a crash leaves nothing to clean up."""
    WIN_OFFSET = 1 << 20    # Windows locks a byte range; this one lies past the pid text, which stays readable
    _warned = False

    def __init__(self, folder):
        self.path = os.path.join(folder, LOCK_NAME)
        self.fd = None

    def acquire(self):
        """True when this process holds the lock (or the filesystem cannot lock: then with a warning)."""
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if msvcrt is not None:
                os.lseek(fd, self.WIN_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            elif fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK, errno.EDEADLK):
                os.close(fd)
                return False
            if not DirLock._warned:         # e.g. a network share without locks: record unprotected
                DirLock._warned = True
                logging.warning(f"[AVRecorder] cannot lock {self.path} ({e}); recording without the lock")
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{os.getpid():<16}\n".encode())
        except OSError:
            pass
        self.fd = fd
        return True

    def holder(self):
        """The pid the holder wrote, '' when unknown."""
        try:
            with open(self.path, "rb") as f:
                return f.read(16).decode(errors="replace").strip()
        except OSError:
            return ""

    def release(self):
        if self.fd is None: return
        fd, self.fd = self.fd, None
        try:
            if msvcrt is not None:
                os.lseek(fd, self.WIN_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            elif fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _has_media(path, size):
    """False when the file holds no packet: killed before its first fragment, it has a header at most
    (36 bytes to ~1 KB, which FFmpeg may refuse to open). A larger file we cannot read is kept."""
    try:
        with av.open(path) as c:
            return any(p.size for p in c.demux())
    except Exception:
        return size >= 4096

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
        self.lock = None
        self._locked_out = False    # set on the worker thread: another process records this camera
        self._lock_reported = False
        self._unsaved = []          # (path, start, end) of finished segments whose rename failed

        self.set_dir(self.dir)

    def set_dir(self, d):
        self.dir = d
        os.makedirs(self.dir, exist_ok=True)

    def set_interval(self, sec):
        self.interval_s = max(_min_interval(), int(sec))

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
        self._lock_reported = False
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
        if self._locked_out:
            # one ERROR, then quiet attempts: this process takes over when the other one stops
            delay = RETRY_DELAYS[-1]
            if not self._lock_reported:
                self._lock_reported = True
                logging.error(f"[AVRecorder] not recording: {err_msg}; trying again every {delay} s")
            else:
                logging.debug(f"[AVRecorder] still locked out: {err_msg}")
        else:
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
        (logging.debug if self._locked_out else logging.info)(f"[AVRecorder] restarting: {self.url_name}")
        self._start_worker()
        self.stat_sig.emit(True)

    # ---- called on the worker thread ------------------------------------------------
    def _folder(self):
        return os.path.join(self.dir, self.url_name)

    def _open_writer(self):
        """Claims the camera folder and keeps what a crash left there. The segment itself opens on the
        first video frame, at that frame's size: the decoder's width()/height() read 0 between a
        reconnect and its next frame."""
        self.writer = None
        self.last_now = 0
        was_locked_out, self._locked_out = self._locked_out, False     # (another failure is not a lock-out)
        sub = self._folder()
        os.makedirs(sub, exist_ok=True)
        lock = DirLock(sub)
        if not lock.acquire():
            self._locked_out = True
            pid = lock.holder()
            raise WorkerRefused(f"{sub} is being recorded by another Nova process{f' (pid {pid})' if pid else ''}")
        if was_locked_out:
            logging.info(f"[AVRecorder] {sub} is free again: recording")
        self._lock_reported = False
        self.lock = lock
        try:
            self._retry_unsaved()
            self._recover_leftovers(sub)
        except Exception:
            self._release_lock()
            raise

    def _release_lock(self):
        if self.lock is not None:
            self.lock.release()
            self.lock = None

    def _output_live(self):
        w = self.writer
        return w is not None and w.first_packet_sent

    def _close_writer(self):
        try:
            if self.writer is not None:
                writer, path, start = self.writer, self.path, self.start_ts
                self.writer = None
                now = time.time()
                # stopped during a camera outage: the segment ended with its last frame
                end = self.last_now if now - self.last_now > GAP_S else now
                self._finish(writer, path, start, int(end))
        finally:
            self._release_lock()

    def write(self, typ, frame):
        if not self.flag: return

        now = time.time()
        if typ == 1:                            # segments start with a video frame, at its size
            if self.writer is None:
                self._start_segment(now, frame)
                logging.info(f"[AVRecorder] recording: {self.path}, next={int(self.next_ts)}")
            elif self._time_jump(now):
                # an outage longer than the jump limit (or the clock was set): the old segment ends
                # with its last frame, not at the reconnect hours later
                logging.debug(f"[AVRecorder] time-jump: {self.url_name}, last={int(self.last_now)}, now={int(now)}")
                self._rollover("time-jump", now, end_ts=int(self.last_now), frame=frame)
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
        old_writer, old_path, old_start = self.writer, self.path, self.start_ts
        self.writer = None
        # finish the old segment first: if its rename fails, the new one must take another file name
        # (it used to reopen the same name and truncate it)
        self._finish(old_writer, old_path, old_start, end_ts)
        self._start_segment(now, frame)
        logging.debug(f"[AVRecorder] chunked: {self.url_name}, reason={reason}, "
                      f"{int(old_start)}->{int(end_ts)}, next={int(self.next_ts)}")

    def _temp_path(self, sub):
        """<name>_output.mp4, unless a finished segment whose rename failed still has that name."""
        base = os.path.join(sub, f"{self.url_name}_output")
        path, n = base + self.ext, 1
        while os.path.lexists(path):
            path = f"{base}_{n}{self.ext}"; n += 1
        return path

    def _start_segment(self, now, frame):
        sub = self._folder()
        vw = self.vid_worker
        ff = vw.get_ffopts()
        try:
            os.makedirs(sub, exist_ok=True)
            path = self._temp_path(sub)
            writer = AVWriter(
                    self.accel,
                    path=path, width=frame.width, height=frame.height, fps=vw.get_fps(),
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
                    live=False,
                )
        except OutputOpenError:
            raise
        except Exception as e:
            raise OutputOpenError(f"cannot start a segment: {e}") from e
        # only now: a failure above must leave no half-set segment state behind
        self.writer, self.path = writer, path
        self.start_ts = int(now)
        self.next_ts = next_boundary(now, self.interval_s)
        self.last_now = now

    def _finish(self, writer, path, start, end_ts):
        try:
            writer.close()
        except Exception:
            logging.error(f"[AVRecorder] close failed: {self.url_name}\n{traceback.format_exc()}")
        self._save_file(path, start, end_ts, writer)

    def _unique(self, base):
        """base + ext, or base_N + ext: never overwrite an earlier segment."""
        dst, n = base + self.ext, 1
        while os.path.lexists(dst):
            dst = f"{base}_{n}{self.ext}"; n += 1
        return dst

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
        if self._rename(src_path, start_ts, end_ts, frames):
            self._retry_unsaved()
        else:
            self._unsaved.append((src_path, start_ts, end_ts))

    def _rename(self, src_path, start_ts, end_ts, frames=None):
        s = time.strftime("%Y%m%d_%H%M", time.localtime(start_ts))
        e = time.strftime("%H%M", time.localtime(end_ts))
        dst = self._unique(os.path.join(os.path.dirname(src_path), f"{s}_{e}"))
        try:
            os.replace(src_path, dst)
        except OSError as err:
            logging.error(f"[AVRecorder] cannot rename {src_path} to {os.path.basename(dst)}: {err}; "
                          f"it keeps its name, the next segment is written next to it")
            return False
        logging.info(f"[AVRecorder] saved: {dst}" + (f" ({frames} frames)" if frames is not None else ""))
        return True

    def _retry_unsaved(self):
        for item in list(self._unsaved):
            if not os.path.lexists(item[0]) or self._rename(*item):
                self._unsaved.remove(item)

    def _recover_leftovers(self, sub):
        """A segment still named <name>_output*.mp4 when recording starts was left by a crash (SIGKILL,
        OOM, power loss): the lock says no other process writes here. Fragmented MP4 plays up to its
        last complete fragment, so it is kept under the time it was last written to, instead of being
        truncated by the new segment."""
        prefix = f"{self.url_name}_output"
        unsaved = {u[0] for u in self._unsaved}
        for name in sorted(os.listdir(sub)):
            stem = name[:-len(self.ext)] if name.endswith(self.ext) else None
            if not stem or not (stem == prefix or (stem.startswith(prefix + "_") and stem[len(prefix) + 1:].isdigit())):
                continue
            path = os.path.join(sub, name)
            if path in unsaved:
                continue
            try:
                st = os.stat(path)
                if not _has_media(path, st.st_size):
                    os.remove(path)
                    logging.warning(f"[AVRecorder] empty segment left by a crash removed: {path}")
                    continue
                stamp = time.strftime("%Y%m%d_%H%M", time.localtime(st.st_mtime))
                dst = self._unique(os.path.join(sub, f"{stamp}_recovered"))
                os.replace(path, dst)
                logging.warning(f"[AVRecorder] segment left by a crash kept: {dst} (last written {stamp})")
            except OSError as e:
                logging.error(f"[AVRecorder] cannot recover {path}: {e}")

    def _time_jump(self, now):
        if not self.last_now:
            return False
        jump = max(120, self.interval_s * 2)
        return (now - self.last_now) > jump or (self.last_now - now) > jump
