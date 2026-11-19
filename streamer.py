"""AVStreamer: the live stream to the -t target (RTMP, SRT, HLS or a file): lifecycle, reconnection
with growing delays, the LIVE icon."""
import logging
from player import Player
from recworker import AVRecWorker, ErrorLog, RETRY_DELAYS, STABLE_AFTER
from avwriter import AVWriter, OutputOpenError, CopyRefused, is_network_target, timestamp_watch
from campacket import AUDIO, PACKET, wants_copy
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
        self._copy_why = None       # -e copy: why this camera's stream is encoded all the same
        self._copy_codec = None     # ... and the codec that was found for

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

    def encoder_note(self):
        """AVWriter.encoder_note() of the stream being sent, for `nova status`; None without one.  With
        -e copy that could not be done: the encoder that works in its place, and why."""
        w, why = self.writer, self._copy_why
        try:
            if why:
                return w.copy_note(why) if w is not None and not w.copy else f"encoded in place of -e copy ({why})"
            return w.encoder_note() if w is not None else None
        except Exception:
            return None

    def _copy_asked(self):
        return wants_copy(self.encoder or getattr(self.vid_worker.get_ffopts(), "v_encoder", None))

    def _tell_worker(self, name, *args):
        f = getattr(self.vid_worker, name, None)
        if f is not None:
            f(*args)

    def _no_copy(self, codec, why):
        """-e copy cannot be done for what the camera sends (an RTMP server takes H.264, and the camera
        sends HEVC): the stream is encoded while it sends that.  Said once; `nova status` goes on saying it."""
        if why != self._copy_why:
            logging.warning(f"[AVStreamer] ⚠️ -e copy is not possible here: {why}. The stream is encoded in its place")
        self._copy_why, self._copy_codec = why, codec
        self._tell_worker("refuse_copy", "streamer", codec)

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
        self._copy_why = self._copy_codec = None
        self._tell_worker("allow_copy", "streamer")
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
        self._tell_worker("set_stream_copy", self._copy_asked())
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
        """The writer of a stream that begins with `frame` (a picture, or with -e copy a key packet).
        None: -e copy cannot be done for this camera's stream (_no_copy), and pictures are on their way."""
        vw = self.vid_worker
        ff = vw.get_ffopts()
        packet = hasattr(frame, "cam")
        encoder = self.encoder or ff.v_encoder
        if wants_copy(encoder) and not packet:
            encoder = None                      # copy was refused for this stream: the encoder Nova chooses
        try:
            writer = AVWriter(
                accel=self.accel,
                path=self.target_url, width=frame.width, height=frame.height, fps=vw.get_fps(),
                encoder=encoder,
                source_codec=vw.get_stream_codec(),
                enc_v_opts=ff.enc_v,
                enc_a_opts=ff.enc_a,
                mux_opts=ff.mux,
                sample_rate=vw.get_sample_rate(), channels=vw.get_channels(),
                audio_enable=not ff.no_audio,
                audio_wait=0 if getattr(vw, 'has_audio', None) is False else None,
                gpu_idx=vw.get_gpu_idx(), gpu_given=getattr(vw, 'gpu_given', lambda: True)(),
                v_tag=ff.v_tag, a_encoder=ff.a_encoder,
                vf=ff.vf, metadata=ff.metadata,
                live=True,
                timestamps=timestamp_watch(vw),
                first=frame if packet else None,
            )
        except CopyRefused as e:
            self._no_copy(frame.cam.codec, str(e))
            return None
        except OutputOpenError:
            raise
        except Exception as e:
            raise OutputOpenError(f"cannot start the stream: {e}") from e
        if packet:
            self._copy_why = self._copy_codec = None
        return writer

    def _output_live(self):
        w = self.writer
        return w is not None and w.first_packet_sent

    def _close_writer(self):
        if self.writer:
            w, self.writer = self.writer, None
            w.close()

    def write(self, typ, frame):
        """typ: a picture to encode (FRAME), sound (AUDIO), or with -e copy a packet of the camera's video
        (PACKET), which can begin a stream only as a key picture."""
        if not self._on: return
        if typ == AUDIO:
            if self.writer is not None:     # the stream starts with a video frame
                self.writer.write_audio(frame)
            return
        packet = typ == PACKET
        if packet and self._copy_codec is not None and frame.cam.codec == self._copy_codec:
            return                          # queued before copy was refused: pictures are on their way
        key = not packet or frame.key
        w = self.writer
        if w is not None and (getattr(w, "copy", False) != packet or (packet and (key or frame.cam is not w.cam) and not w.takes(frame))):
            # not the stream that is being sent (the camera changed its codec, its size or its parameter
            # sets, or -e copy ends or begins for it): a new session for what comes now
            if not key: return
            self._close_writer()
            w = None
        if w is None:
            if not key: return
            self.writer = w = self._new_writer(frame)
            if w is None: return
        if packet:
            w.write_packet(frame)
        else:
            w.write_video(frame)
