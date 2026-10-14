# videoworker.py

import os
from urllib.parse import urlparse
import socket
import selectors
import errno
import threading
import re
import time
import random
import logging
import traceback
import queue
from fractions import Fraction
from collections import deque

import numpy as np
from PySide6.QtCore import QThread, Signal

import logsync  # installs logging.verbose / logging.trace
from mytools import Skipper, FrameRator, Clock
from util import LINUX, WINDOWS, MAC, WSL, MSYS, IS_PI, mask_url, display_url
from codec_names import get_friendly_codec_name
from ffparser import FFmpegParser, for_codec
from dispframe import DisplayFrame, GL_DIRECT_FORMATS

import av
from av.codec.hwaccel import HWAccel, hwdevices_available

HWACCELS = [str(x) for x in hwdevices_available()]
try:
    CODECS_AVAILABLE = set(av.codecs_available)
except Exception:
    CODECS_AVAILABLE = set()

# Backends that ship as *separate* FFmpeg decoders ("h264_cuvid", "hevc_qsv", ...).
SEPARATE_DECODER_SUFFIX = {
    "cuda": "cuvid", "cuvid": "cuvid", "nvdec": "cuvid",
    "qsv": "qsv", "amf": "amf", "rkmpp": "rkmpp", "v4l2m2m": "v4l2m2m",
    "mmal": "mmal", "omx": "omx", "nvmpi": "nvmpi", "mediacodec": "mediacodec",
}
# hwaccel device types that are never attached to the standard decoder (CodecContext with hwaccel=);
# they are only used through their separate decoders.
HWACCEL_DEDICATED_ONLY = {"qsv", "vulkan", "drm", "opencl"}
# schemes whose host is probed with a plain TCP connect before av.open(), and their default ports
PROBE_PORTS = {"rtsp": 554, "rtsps": 322, "rtmp": 1935, "rtmps": 443, "http": 80, "https": 443, "tcp": None}
_CONNECT_IN_PROGRESS = {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY,
                        getattr(errno, "WSAEWOULDBLOCK", 10035)}

HW_RETRY_S = 300.0          # a hardware decoder that failed is tried again after 5 min, then 10, 20, ...
HW_RETRY_MAX_S = 3600.0
GIVE_UP_KEYFRAMES = 3       # a decoder with no picture after this many keyframes has failed
MAX_DECODE_ERRORS = 30      # video packets in a row the decoder rejects before it is given up
MAX_AUDIO_ERRORS = 100      # audio packets in a row before audio is ignored until the next connection
VIDEO_STALL_S = 10.0        # no video packet for this long (audio still flowing): reconnect
FIRST_PICTURE_S = 60.0      # no picture at all this long after connecting: reconnect
GOP_UNKNOWN_S = 20.0        # keyframe interval assumed until two keyframes have been seen
FPS_MAX = 120               # a larger "frame rate" is a clock rate (90000), not frames
RERESOLVE_AFTER = 3         # connection attempts without a picture before onvif:// etc. are resolved again
RERESOLVE_MIN_S = 60.0

PATTERN = re.compile(r'^\[[^\]]+\]\s*([^:]+)')

def _short_err(e: Exception) -> str:
    m = PATTERN.search(str(e))
    return m.group(1).strip() if m else str(e)


def _mask(url) -> str:
    """mask_url(), plus URLs whose password contains '/' or '@' (mask_url cannot see where such a
    password ends): everything before the last '@' of the address is hidden."""
    s = mask_url(url)
    head, sep, rest = s.partition("://")
    if sep and "@" in rest:
        userinfo, _, tail = rest.rpartition("@")
        if not re.fullmatch(r"[^:/@]*(:\*\*\*)?", userinfo):
            return f"{head}://***@{tail}"
    return s


def _queue_nowait_put(q, i):
    """Put without blocking; drop the oldest item when the queue is full. False when one was dropped."""
    try:
        q.put_nowait(i)
        return True
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(i)
        except queue.Full:
            pass
        return False

def _pick_gpu_index(spec=None, default_idx=0):
    """GPU index from --gpu (spec) or NOVA_GPU_DEVICES: one index, or a comma list to pick from at random.
    The launcher (cams.py) deals the cameras over a list deterministically before they get here."""
    v = spec if spec not in (None, "") else os.getenv("NOVA_GPU_DEVICES")
    logging.trace(f"[VideoWorker] --gpu={spec!r} NOVA_GPU_DEVICES={os.getenv('NOVA_GPU_DEVICES')!r}")
    if not v:
        return default_idx
    try:
        nums = [int(x.strip()) for x in str(v).split(",")]
        return random.choice(nums)
    except Exception:
        logging.warning(f"[VideoWorker] invalid GPU list {v!r}; using GPU {default_idx}")
        return default_idx

_bad_timeout_warned = False

def _timeout_us() -> int:
    """NOVA_TIMEOUT in microseconds (default 5 s for camera input); a malformed value warns once."""
    global _bad_timeout_warned
    v = os.getenv("NOVA_TIMEOUT", "5000000")
    try:
        return max(1, int(v))
    except ValueError:
        if not _bad_timeout_warned:
            _bad_timeout_warned = True
            logging.warning(f"[VideoWorker] NOVA_TIMEOUT={v!r} is not a number of microseconds; using 5 s")
        return 5_000_000

def _build_conn_options(url):
    s = str(_timeout_us())
    logging.trace(f"[VideoWorker] NOVA_TIMEOUT={s}")
    options = {}
    if not url: return options
    if url.startswith('rtsp'):
        options['rtsp_transport'] = 'tcp'
        options['timeout'] = s      # rtsp demuxer: socket I/O timeout (us), passed on to its TCP connection
        options['rw_timeout'] = s
    elif url.startswith(('rtmp', 'srt', 'http', 'tcp', 'udp')):
        options['rw_timeout'] = s
    return options

def _timeout_seconds() -> float:
    return max(1.0, _timeout_us() / 1_000_000)

def accel_supported(accel: str) -> bool:
    a = (accel or "").lower()
    return (not a) or a in HWACCELS or a in SEPARATE_DECODER_SUFFIX

def _decoder_candidates(accel: str, codec_name: str) -> list[str]:
    """Dedicated HW decoder names for the requested backend, e.g. cuda+h264 -> ['h264_cuvid']."""
    suf = SEPARATE_DECODER_SUFFIX.get(accel.lower())
    if not suf: return []
    name = f"{codec_name.lower()}_{suf}"
    if CODECS_AVAILABLE and name not in CODECS_AVAILABLE:
        return []
    return [name]

def _device_source(src: str):
    """Map capture-device inputs to (file, format). Returns (None, None) for an unusable spec.
       '0'                      -> /dev/video0 (v4l2) | 0:none (avfoundation)
       v4l2:///dev/video2       -> explicit format
       dshow://video=Camera     -> Windows DirectShow (device *name* required)
       avfoundation://0         -> macOS"""
    if src.isdigit():
        if LINUX or WSL: return f"/dev/video{src}", "v4l2"
        if MAC: return f"{src}:none", "avfoundation"
        return None, None
    m = re.match(r'^(v4l2|video4linux2|dshow|avfoundation|gdigrab|x11grab)://(.*)$', src)
    if m:
        return m.group(2), m.group(1)
    return src, None

def _delay(seconds: float) -> str:
    return f"{seconds / 60:.0f} min" if seconds >= 60 else f"{seconds:.0f} s"

def _strerror(code: int) -> str:
    # socket.errorTab: Windows' WSA error texts (os.strerror() does not know them)
    return getattr(socket, "errorTab", {}).get(code) or os.strerror(code)

def _copy_codec_params(ctx, base):
    """What a fresh decoder context needs from the demuxer's one (avcodec_parameters_to_context is not
    exposed by PyAV).  pkt_timebase matters: cuvid rescales packet timestamps with it."""
    ctx.width = base.width
    ctx.height = base.height
    if base.extradata:
        ctx.extradata = base.extradata
    for attr in ("pkt_timebase", "codec_tag", "bits_per_coded_sample", "sample_aspect_ratio", "framerate",
                 "colorspace", "color_range", "color_primaries", "color_trc"):
        try:
            v = getattr(base, attr)
            if v: setattr(ctx, attr, v)
        except Exception:
            pass


class _Reconnect(Exception):
    """Ends the current connection; the text says why."""


class _VideoDecode:
    """The video decoder of one connection: hardware when it works, software otherwise.

    The container is opened without a hardware context, so the decoder can be chosen, and changed,
    without reconnecting.  A hardware decoder that does not open, produces no picture or keeps rejecting
    packets is replaced by the software decoder at once (from the next keyframe).  When VideoWorker's
    retry delay has passed, a new hardware decoder is tried *alongside* the software one and takes over
    when it delivers its first picture, so a retry costs no picture."""

    def __init__(self, worker, stream):
        self.w = worker
        self.base = stream.codec_context
        self.codec_name = self.base.codec.name
        self.ctx = None
        self.is_hw = False
        self.name = ""
        self.kf = 0              # keyframes fed to the decoder before its first picture
        self.got = False         # the decoder delivered a picture
        self.errors = 0          # packets in a row the decoder rejected
        self.last_err = ""       # the last rejection, for the give-up message
        self.wait_kf = False     # the decoder (re)starts at the next keyframe
        self.trial = None        # hardware decoder on trial, fed alongside the software one
        self.trial_name = ""
        self.trial_kf = 0
        self.trial_errors = 0
        self.skip_upto = None    # after a switch: drop pictures the previous decoder already delivered
        self.skipped = 0
        self.last_pts = None

    def open(self):
        w = self.w
        if w.accel and w._hw_fail is None:
            ctx, name, err = w._open_hw_decoder(self.base, self.codec_name)
            if ctx is not None:
                logging.info(f"[VideoWorker] 🚀 HW decoder: {name}")
                self._use(ctx, True, name)
                return
            w._hw_failed(err, self.codec_name)
        self._use_sw()

    def _use(self, ctx, is_hw, name):
        self.ctx, self.is_hw, self.name = ctx, is_hw, name
        self.kf, self.got, self.errors, self.last_err = 0, False, 0, ""

    def _use_sw(self):
        b = self.base
        if b.is_open:
            b.flush_buffers()           # used before a hardware decoder took over
        else:
            opts = _dec_opts_for(b, self.w.dec_opts)
            b.options.update(opts)
            logging.debug(f"[VideoWorker] software decoder {self.codec_name} options={b.options}")
            try:
                b.open()
                _report_unused_dec(b, opts)
            except Exception as e:
                logging.debug(f"[VideoWorker] explicit open of {self.codec_name} failed, decoding lazily: {_short_err(e)}")
        self._use(b, False, self.codec_name)

    def fallback_sw(self, reason, key):
        """The hardware decoder failed while decoding: software from now on, starting with this
        packet when it is a keyframe, else with the next keyframe."""
        self.w._hw_failed(reason, self.codec_name)
        self._use_sw()
        self.skip_upto, self.skipped = self.last_pts, 0
        self.wait_kf = not key

    def decode(self, packet):
        """The pictures to show / record for this video packet.  Raises _Reconnect when the
        connection should be given up."""
        key = packet.is_keyframe
        if self.wait_kf:
            if not key:
                return []
            self.wait_kf = False

        if self.trial is None and key and not self.is_hw and self.w._hw_retry_due():
            self._start_trial()
        if self.trial is not None:
            frames = self._feed_trial(packet, key)
            if frames:
                self.w._hw_succeeded(self.trial_name)
                self.skip_upto, self.skipped = self.last_pts, 0
                self._use(self.trial, True, self.trial_name)
                self.got = True
                self.trial = None
                return self._fresh(frames)

        if key and not self.got:
            if self.kf >= GIVE_UP_KEYFRAMES:
                why = f" (last error: {self.last_err})" if self.last_err else ""
                if not self.is_hw:
                    raise _Reconnect(f"no decodable frames after {self.kf} keyframes{why}")
                self.fallback_sw(f"{self.name} produced no frames after {self.kf} keyframes{why}", key)
            self.kf += 1
        try:
            frames = self.ctx.decode(packet)
        except av.error.FFmpegError as e:
            return self._rejected(packet, key, e)
        self.errors = 0
        if frames:
            self.got = True
        return self._fresh(frames)

    def _rejected(self, packet, key, e):
        self.errors += 1
        self.last_err = _short_err(e)
        if self.errors == 1:
            logging.verbose(f"[VideoWorker] {self.name} rejected a video packet ({_short_err(e)}); skipped")
        if self.errors < MAX_DECODE_ERRORS:
            return []
        reason = f"{self.name}: {self.errors} video packets in a row failed to decode ({_short_err(e)})"
        if not self.is_hw:
            raise _Reconnect(reason)
        self.fallback_sw(reason, key)
        return self.decode(packet) if key else []

    def _fresh(self, frames):
        if self.skip_upto is not None and frames:
            keep = []
            for f in frames:
                if (self.skip_upto is not None and f.pts is not None and f.pts <= self.skip_upto
                        and self.skipped < 64):
                    self.skipped += 1
                    continue
                self.skip_upto = None
                keep.append(f)
            frames = keep
        if frames and frames[-1].pts is not None:
            self.last_pts = frames[-1].pts
        return frames

    def _start_trial(self):
        ctx, name, err = self.w._open_hw_decoder(self.base, self.codec_name)
        if ctx is None:
            self.w._hw_failed(err, self.codec_name)
            return
        logging.debug(f"[VideoWorker] trying {name} again, alongside the software decoder")
        self.trial, self.trial_name, self.trial_kf, self.trial_errors = ctx, name, 0, 0

    def _feed_trial(self, packet, key):
        if key:
            self.trial_kf += 1
        try:
            frames = self.trial.decode(packet)
        except av.error.FFmpegError as e:
            self.trial_errors += 1
            if self.trial_errors >= GIVE_UP_KEYFRAMES:
                self._end_trial(f"{self.trial_name}: {_short_err(e)}")
            return None
        self.trial_errors = 0
        frames = [f for f in frames if f.width > 0 and f.height > 0]
        if frames:
            return frames
        if self.trial_kf > GIVE_UP_KEYFRAMES:
            self._end_trial(f"{self.trial_name} produced no frames after {self.trial_kf - 1} keyframes")
        return None

    def _end_trial(self, reason):
        self.trial = None
        self.w._hw_failed(reason, self.codec_name)


class VideoWorker(QThread):
    img_sig = Signal()
    stat_sig = Signal(str) # (text)
    sz_sig = Signal(int, int) # (w, h)
    exit_sig = Signal()

    def __init__(self, url, fps_disp, opengl, accel, headless, ffmpeg, gpu=None):
        super().__init__()
        self.url = url                  # as typed by the user (may be onvif://, xmeye://, '0', ...)
        self.fps_disp = fps_disp
        self.opengl = opengl
        self.accel = (accel or "").lower() or None
        self.headless = headless
        self._resolved = None           # resolved av.open() source
        self._resolve_t = 0.0           # when it was resolved (monotonic)
        self._resolve_failed = False    # the last resolution attempt failed (log the next one quietly)

        self.ffopts = FFmpegParser.parse(ffmpeg)
        for msg in self.ffopts.warnings:
            logging.warning(f"[VideoWorker] --ffmpeg {msg}")
        for msg in self.ffopts.notes:
            logging.info(f"[VideoWorker] --ffmpeg {msg}")
        self.hw_opts = self.ffopts.hw
        self.open_opts = self.ffopts.open
        self.dec_opts = self.ffopts.dec
        self.enc_v_opts = self.ffopts.enc_v
        self.enc_a_opts = self.ffopts.enc_a

        self.fps = 0
        self._size = (0, 0)             # (w, h) of the last picture; one tuple, so readers never see half an update
        self.last_frame_time = 0.0      # time.monotonic() of the last decoded video frame (0.0: none yet)
        self.sample_rate = 0
        self.channels = 0
        self.audio_q = queue.Queue(maxsize=10)
        # ~1.6 s at 25 fps: audio frames (~50/s) share the queues. When a writer cannot keep up the
        # oldest items are dropped and counted (_put); larger queues would hold GBs of decoded 4K frames.
        self.rec_q = queue.Queue(maxsize=120)
        self.stream_q = queue.Queue(maxsize=120)
        self._drops = {}                # queue name -> [items dropped since the last warning, time of it]
        self.has_audio = None           # the current connection carries audio (None: not connected yet)
        self.frame_q = deque(maxlen=1)
        self.clock = Clock()
        self.rec_on = False
        self.stream_on = False
        self.audio_on = False
        self.run_flag = True
        self.gpu_idx = _pick_gpu_index(gpu)
        self._gpu_given = gpu not in (None, "") or bool(os.getenv("NOVA_GPU_DEVICES"))
        self._gpu_warned = False
        self._unreachable = None        # last probe failure, to log state changes once
        self._last_fail = None          # last connection failure logged (repeats are logged at DEBUG)
        self._url_warned = False
        self._url_hint = ""             # appended to a failure overlay when the URL cannot be parsed
        self._fps_warned = False
        self._hw_fail = None            # why the last hardware decoder failed; None: use hardware
        self._hw_retry_at = 0.0         # monotonic time after which hardware is tried again
        self._hw_backoff = HW_RETRY_S
        self._gops = deque(maxlen=5)    # recent keyframe intervals (s), for the no-picture watchdog
        self._session_frames = 0        # video frames decoded on the current connection
        self._fail_streak = 0           # connection attempts in a row that gave no picture
        self.codec_name = "h264"
        self._clone_warned = False

    def width(self): return self._size[0]
    def height(self): return self._size[1]
    def size(self): return self._size
    def ratio(self):
        w, h = self._size
        return w / h if w > 0 and h > 0 else 1.0

    def get_fps(self): return self.fps
    def get_sample_rate(self): return self.sample_rate
    def get_channels(self): return self.channels
    def get_queues(self): return self.audio_q, self.rec_q, self.stream_q, self.frame_q
    def set_rec_on(self, v: bool): self.rec_on = bool(v)
    def set_stream_on(self, v: bool): self.stream_on = bool(v)
    def set_audio_on(self, v: bool): self.audio_on = bool(v)
    def get_gpu_idx(self): return self.gpu_idx
    def get_stream_codec(self): return self.codec_name
    def get_encoder_opts(self): return self.enc_v_opts, self.enc_a_opts
    def get_open_opts(self): return self.open_opts
    def get_ffopts(self): return self.ffopts
    def get_resolved_url(self): return self._resolved

    # ------------------------------------------------------------------ main loop
    def run(self):
        logging.debug("[VideoWorker] started")

        if self.accel and not accel_supported(self.accel):
            logging.critical(f"[VideoWorker] ❌ --accel={self.accel} is not supported by this FFmpeg/PyAV build. "
                             f"Available hw devices: {HWACCELS}")
            self.stat_sig.emit(f"Unsupported --accel={self.accel}")
            self.run_flag = False
            self.exit_sig.emit()
            return

        while self.run_flag:
            src = self._resolve_source()
            if not self.run_flag: break
            if src is None:
                self.stat_sig.emit(f"Failed:\nURI resolve\n{display_url(self.url)}")
                self.clock.sleep(10000)
                continue

            u_disp = display_url(src)
            self.stat_sig.emit(f"Connecting...\n{u_disp}")
            self._session_frames = 0

            container = self._open_container(src)
            if container is None:
                self._attempt_failed()
                continue
            if not self.run_flag:
                container.close(); break

            try:
                self._stream_loop(container, u_disp)
            except Exception as e:
                errstr = _short_err(e)
                self.stat_sig.emit(f"Stream error:\n{errstr}")
                self._log_fail(errstr, f"[VideoWorker] Stream exception: {_mask(str(e))}", logging.ERROR)
                logging.trace(f"[VideoWorker] Exception Traceback: {traceback.format_exc()}")
            finally:
                try: container.close()
                except Exception: pass
            if self._session_frames == 0:
                self._attempt_failed()

            if self.run_flag:
                self.clock.sleep(1000)

        logging.debug("[VideoWorker] loop finished")

    def stop(self) -> bool:
        """Stop and wait for the thread.  False: it is still blocked in I/O (av.open() and the stream
        analysis cannot be interrupted and may take up to 2 x NOVA_TIMEOUT); the caller must not
        destroy the thread object then."""
        if self.run_flag:
            self.run_flag = False
            self.clock.wake()
            if not self.wait(int(_timeout_seconds() * 1000) + 3000):
                logging.warning("[VideoWorker] decoder thread did not finish in time (blocked in I/O)")
                return False
            logging.debug("[VideoWorker] stopped.")
            return True
        return not self.isRunning()

    def _log_fail(self, key, msg, level=logging.WARNING):
        """Log a connection failure when it differs from the previous one; at DEBUG while it repeats."""
        if key != self._last_fail:
            self._last_fail = key
            logging.log(level, msg)
        else:
            logging.debug(msg)

    def _attempt_failed(self):
        """A connection attempt ended without a picture (unreachable, open failure, empty session)."""
        self._fail_streak += 1

    # ------------------------------------------------------------------ connection
    def _resolve_source(self):
        """onvif:// xmeye:// hik:// dahua:// are resolved here, on the worker thread.  The result is kept;
        after RERESOLVE_AFTER attempts in a row without a picture it is resolved again, bypassing the
        disk cache (the camera may have moved to another port, or the first answer was a fallback)."""
        if not any(self.url.startswith(p) for p in ("onvif://", "xmeye://", "hik://", "dahua://")):
            self._resolved = self.url
            return self._resolved
        again = self._resolved is not None
        if again and not (self._fail_streak >= RERESOLVE_AFTER
                          and time.monotonic() - self._resolve_t >= RERESOLVE_MIN_S):
            return self._resolved
        self.stat_sig.emit(f"Resolving...\n{display_url(self.url)}")
        url = None
        try:
            from auth import resolve_input_uri
            url = resolve_input_uri(self.url, use_cache=not again, should_stop=lambda: not self.run_flag,
                                    quiet=self._resolve_failed or again)
        except Exception as e:
            logging.debug(f"[VideoWorker] URI resolve raised: {_mask(str(e))}")
        self._resolve_t = time.monotonic()
        if not self.run_flag:
            return self._resolved
        if url:
            if again and url != self._resolved:
                logging.info(f"[VideoWorker] {display_url(self.url)} resolved again: {_mask(url)}")
            self._resolved = url
            self._resolve_failed = False
            self._fail_streak = 0
        elif again:
            logging.debug("[VideoWorker] resolving again failed; keeping the previous URL")
            self._fail_streak = 0
        else:
            self._log_fail("resolve", f"[VideoWorker] URI resolve failed for {display_url(self.url)}; retrying every 10 s",
                           logging.ERROR)
            self._resolve_failed = True
        return self._resolved

    def _hw_device(self, accel):
        if self.ffopts.hw_device: return str(self.ffopts.hw_device)
        if accel in ("cuda", "nvdec"): return str(self.gpu_idx)
        if accel == "vaapi": return f"/dev/dri/renderD{128 + self.gpu_idx}"
        # adapter index; only when asked for, so a single-GPU setup keeps FFmpeg's default adapter
        if accel in ("d3d11va", "dxva2", "d3d12va") and self._gpu_given: return str(self.gpu_idx)
        if self._gpu_given: self._warn_gpu_ignored(accel)
        return None

    def _warn_gpu_ignored(self, what):
        if self._gpu_given and not self._gpu_warned:
            self._gpu_warned = True
            logging.warning(f"[VideoWorker] ⚠️ {what} has no device option; --gpu / NOVA_GPU_DEVICES "
                            f"(GPU {self.gpu_idx}) does not apply to decoding, the default device is used")

    def _open_container(self, src):
        file, fmt = _device_source(src)
        if file is None:
            logging.critical(f"[VideoWorker] ❌ Capture device '{src}' needs a name on this platform, "
                             f"e.g. -i \"dshow://video=Integrated Camera\"")
            self.stat_sig.emit("Invalid capture device")
            self.run_flag = False
            self.exit_sig.emit()
            return None

        open_opts = _build_conn_options(src)
        open_opts.update(self.open_opts)
        kwargs = {"options": open_opts}
        device = bool(fmt)                      # a capture device: no host to probe, no I/O deadline
        if not fmt and self.ffopts.in_fmt:
            fmt = self.ffopts.in_fmt            # --ffmpeg "-f mjpeg -i": the input format, as on an ffmpeg command line
        if fmt: kwargs["format"] = fmt
        # PyAV's interrupt callback: a blocking open()/read() gives up after NOVA_TIMEOUT so that
        # stop() and reconnects never hang forever (UDP sources, dead cameras, ...)
        t_s = _timeout_seconds()
        if t_s > 0 and not device:
            kwargs["timeout"] = (t_s, t_s)
        # No hwaccel here: a hardware context given to av.open() is created for the standard decoder
        # even when a dedicated one (h264_cuvid) decodes, and any hardware problem fails the whole open.
        # The decoder is chosen once the stream is known (_VideoDecode).

        # av.open() cannot be interrupted before its timeout, so a camera that is down would hold
        # stop() for up to NOVA_TIMEOUT.  Probe the host first in 0.5 s slices that honour run_flag.
        if not device and t_s > 0:
            reason = self._probe_host(file, t_s)
            if not self.run_flag:
                return None
            if reason is not None:
                self.stat_sig.emit(f"Unreachable:\n{reason}")
                if reason != self._unreachable:
                    logging.info(f"[VideoWorker] unreachable, retrying every 10 s: {reason} [{_mask(file)}]")
                    self._unreachable = reason
                    self._last_fail = None
                else:
                    logging.debug(f"[VideoWorker] still unreachable: {reason}")
                self.clock.sleep(10000)
                return None
            if self._unreachable is not None:
                logging.info(f"[VideoWorker] host answers again [{_mask(file)}]")
                self._unreachable = None
                self._last_fail = None

        logging.debug(f"[VideoWorker] av.open({_mask(file)}, format={fmt}, options={open_opts})")
        try:
            return av.open(file, **kwargs)
        except Exception as e:
            errstr = _short_err(e)
            self.stat_sig.emit(f"Failed:\n{errstr}{self._url_hint}")
            level = logging.WARNING if isinstance(e, (av.error.TimeoutError, av.error.OSError)) else logging.ERROR
            self._log_fail(errstr, f"[VideoWorker] open failed, retrying every 10 s: {_mask(str(e))}", level)
            logging.trace(f"[VideoWorker] Exception Traceback: {traceback.format_exc()}")
            self.clock.sleep(10000)
        return None

    def _probe_host(self, url, budget_s):
        """TCP connect to the camera's host:port, in 0.5 s slices so stop() interrupts it at once.
        None = the host answers (or the probe does not apply: devices, udp, srt, files, a URL it cannot
        parse); otherwise a short reason.  Every address of the host is tried at once, so a dead IPv6
        address does not hide a working IPv4 one.  A host whose addresses all refuse or are unroutable
        is reported immediately, a silent one after budget_s (the NOVA_TIMEOUT av.open() would spend)."""
        self._url_hint = ""
        try:
            p = urlparse(url)
            scheme, host = p.scheme, p.hostname
            port = p.port if scheme in PROBE_PORTS and host else None
        except ValueError:
            # e.g. a password with an unencoded / ? # [ ]: Python's error text contains a piece of it
            self._warn_bad_url(url)
            return None
        if scheme not in PROBE_PORTS or not host:
            return None
        port = port or PROBE_PORTS[scheme]
        if not port:
            return None
        try:
            return self._probe_tcp(host, port, budget_s)
        except Exception as e:          # the probe is an optimisation; it must never take the worker down
            logging.debug(f"[VideoWorker] probe of {host}:{port} skipped: {type(e).__name__}")
            return None

    def _warn_bad_url(self, url):
        self._url_hint = "\n(check the URL: percent-encode\n/ ? # @ [ ] in the password)"
        if not self._url_warned:
            self._url_warned = True
            scheme = url.split("://", 1)[0] if "://" in url else "?"
            logging.warning(f"[VideoWorker] ⚠️ cannot read host:port from the {scheme}:// URL: a user name or password "
                            f"containing / ? # @ [ ] must be percent-encoded (%2F %3F %23 %40 %5B %5D); trying anyway")

    def _getaddrinfo(self, host, port, deadline):
        """getaddrinfo() that stop() and the deadline can abandon (a DNS lookup cannot be interrupted).
        Returns (infos, None), (None, reason), or (None, None) when stopped."""
        box = {}
        done = threading.Event()
        def lookup():
            try:
                box["r"] = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
            except Exception as e:      # OSError, UnicodeError for an invalid name
                box["e"] = e
            finally:
                done.set()
        threading.Thread(target=lookup, name="nova-dns", daemon=True).start()
        while not done.wait(0.25):
            if not self.run_flag:
                return None, None
            if time.monotonic() >= deadline:
                return None, f"{host}: name lookup timed out"
        if "e" in box:
            e = box["e"]
            return None, f"{host}: {getattr(e, 'strerror', None) or e}"
        return box["r"], None

    def _probe_tcp(self, host, port, budget_s):
        deadline = time.monotonic() + budget_s
        infos, reason = self._getaddrinfo(host, port, deadline)
        if infos is None:
            return reason
        sel = selectors.DefaultSelector()
        pending = []
        errors = []
        try:
            for family, stype, proto, _, addr in infos:
                try:
                    sock = socket.socket(family, stype, proto)
                except OSError as e:
                    errors.append(e.strerror or str(e))
                    continue
                sock.setblocking(False)
                rc = sock.connect_ex(addr)
                if rc == 0:
                    sock.close()
                    return None
                if rc in _CONNECT_IN_PROGRESS:
                    sel.register(sock, selectors.EVENT_WRITE)
                    pending.append(sock)
                else:
                    errors.append(_strerror(rc))        # refused, no route, ...
                    sock.close()
            while pending and self.run_flag:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                for key, _ in sel.select(min(0.5, left)):
                    sock = key.fileobj
                    rc = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                    if rc == 0:
                        return None
                    errors.append(_strerror(rc))
                    sel.unregister(sock)
                    pending.remove(sock)
                    sock.close()
        finally:
            for sock in pending:
                sock.close()
            sel.close()
        if pending or not errors:
            return f"{host}:{port} does not answer"
        return f"{host}:{port}: {errors[-1]}"

    # ------------------------------------------------------------------ decoder selection
    def _hw_failed(self, reason, codec_name):
        """A hardware decoder could not be used: software now, hardware again after a delay that
        doubles while it keeps failing (5 min ... 1 h)."""
        if self._hw_fail is None:
            self._hw_backoff = HW_RETRY_S
        else:
            self._hw_backoff = min(HW_RETRY_MAX_S, self._hw_backoff * 2)
        self._hw_retry_at = time.monotonic() + self._hw_backoff
        msg = (f"[VideoWorker] ❌ No working HW decoder for --accel={self.accel} ({_mask(reason)}). "
               f"Falling back to software ({codec_name}); hardware is tried again in {_delay(self._hw_backoff)}.")
        if reason != self._hw_fail:
            logging.warning(msg)
        else:
            logging.debug(msg)
        self._hw_fail = reason

    def _hw_succeeded(self, name):
        if self._hw_fail is not None:
            logging.info(f"[VideoWorker] 🚀 HW decoder: {name} (hardware decoding again)")
        self._hw_fail = None
        self._hw_backoff = HW_RETRY_S

    def _hw_retry_due(self):
        return bool(self.accel) and self._hw_fail is not None and time.monotonic() >= self._hw_retry_at

    def _open_hw_decoder(self, base, codec_name):
        """(ctx, label, None) for an opened hardware decoder, else (None, None, reason)."""
        errors = []
        # 1) dedicated HW decoder (h264_cuvid, hevc_qsv, h264_rkmpp, ...)
        for d_name in _decoder_candidates(self.accel, codec_name):
            ctx, err = self._open_named_decoder(d_name, base)
            if ctx is not None:
                return ctx, d_name, None
            errors.append(f"{d_name}: {err}")
        # 2) the standard decoder with a hardware device (vaapi, videotoolbox, d3d11va, dxva2, vdpau, cuda ...)
        if self.accel in HWACCELS and self.accel not in HWACCEL_DEDICATED_ONLY:
            ctx, err = self._open_hwaccel_decoder(base)
            if ctx is not None:
                return ctx, f"{codec_name} + {self.accel} hwaccel", None
            errors.append(f"{codec_name} + {self.accel}: {err}")
        return None, None, "; ".join(errors) or f"no {self.accel} decoder for {codec_name}"

    def _open_named_decoder(self, name, base):
        """(ctx, None) or (None, reason) for a dedicated hardware decoder."""
        try:
            ctx = av.CodecContext.create(av.Codec(name, 'r'))
            _copy_codec_params(ctx, base)
            ctx.thread_type = "NONE"
            if "cuvid" in name:
                ctx.options['gpu'] = str(self.gpu_idx)
            else:
                # h264_qsv / _amf / _rkmpp / _v4l2m2m / _mediacodec have no device option
                self._warn_gpu_ignored(name)
            opts = _dec_opts_for(ctx, self.dec_opts)
            ctx.options.update(opts)
            logging.debug(f"[VideoWorker] trying decoder {name} options={ctx.options}")
            ctx.open()
            _report_unused_dec(ctx, opts)
            return ctx, None
        except Exception as e:
            logging.debug(f"[VideoWorker] decoder {name} unavailable: {_short_err(e)}")
            return None, _short_err(e)

    def _open_hwaccel_decoder(self, base):
        """(ctx, None) or (None, reason): the stream's own decoder with --accel's device attached."""
        device = self._hw_device(self.accel)
        try:
            hw = HWAccel(device_type=self.accel, device=device, allow_software_fallback=False,
                         options=self.hw_opts or None)
            ctx = av.CodecContext.create(base.codec, "r", hwaccel=hw)     # creates the device
        except Exception as e:
            logging.debug(f"[VideoWorker] {self.accel} device {device} unavailable: {_short_err(e)}")
            return None, _short_err(e)
        if not ctx.is_hwaccel:
            return None, f"{self.accel} cannot decode {base.codec.name}"
        try:
            _copy_codec_params(ctx, base)
            opts = _dec_opts_for(ctx, self.dec_opts)
            ctx.options.update(opts)
            logging.debug(f"[VideoWorker] trying {base.codec.name} + {self.accel} device={device} options={ctx.options}")
            ctx.open()
            _report_unused_dec(ctx, opts)
            return ctx, None
        except Exception as e:
            logging.debug(f"[VideoWorker] {self.accel} hwaccel failed for {base.codec.name}: {_short_err(e)}")
            return None, _short_err(e)

    def _frame_rate(self, video_stream):
        """The camera's frame rate, from the stream header; a value above FPS_MAX is a clock rate
        (RTP's 90 kHz), not a frame rate."""
        for rate in (video_stream.average_rate, video_stream.guessed_rate):
            try:
                r = float(rate) if rate else 0.0
            except (TypeError, ValueError, ZeroDivisionError):
                r = 0.0
            if 0 < r <= FPS_MAX:
                return max(1, int(round(r)))
        fps = min(FPS_MAX, max(1, int(self.fps_disp or 30)))
        msg = (f"[VideoWorker] cannot read the video frame rate (average {video_stream.average_rate}, "
               f"guessed {video_stream.guessed_rate}); assuming {fps} fps")
        if not self._fps_warned:
            self._fps_warned = True
            logging.warning(msg)
        else:
            logging.debug(msg)
        return fps

    def _gop_s(self):
        return max(self._gops) if self._gops else GOP_UNKNOWN_S

    # ------------------------------------------------------------------ streaming
    def _stream_loop(self, container, u_disp):
        video_stream = next((s for s in container.streams if s.type == "video"), None)
        audio_stream = next((s for s in container.streams if s.type == "audio"), None)

        if video_stream is None:
            self.stat_sig.emit("No video stream")
            self._log_fail("novideo", f"[VideoWorker] no video stream in {u_disp}; retrying every 10 s")
            self.clock.sleep(10000)
            return

        dec = _VideoDecode(self, video_stream)
        self.codec_name = dec.codec_name
        logging.debug(f"[VideoWorker] Stream codec: {dec.codec_name} ({get_friendly_codec_name(dec.codec_name)}) "
                      f"{dec.base.width}x{dec.base.height}")
        dec.open()
        audio_context = audio_stream.codec_context if audio_stream else None

        fps_cap = self._frame_rate(video_stream)
        self.fps = fps_cap

        sk = Skipper(maxfps=self.fps_disp, o_fps=fps_cap)
        fr = FrameRator()
        logging.verbose(f"[VideoWorker] fps_disp:{self.fps_disp}, fps_cap:{fps_cap}")

        streams = [video_stream]
        if audio_stream is not None:
            streams.append(audio_stream)
        self.has_audio = audio_stream is not None     # writers need not wait for audio that cannot come

        first_frame_logged = False
        a_errors = 0
        t_start = time.monotonic()
        t_vpkt = t_start            # last video packet
        t_vframe = None             # last decoded video frame on this connection
        t_key = None                # last video keyframe packet
        try:
            for packet in container.demux(*streams):
                if not self.run_flag: break
                if packet.dts is None: continue
                now = time.monotonic()

                if packet.stream.type == 'video':
                    t_vpkt = now
                    if packet.is_keyframe:
                        if t_key is not None:
                            self._gops.append(now - t_key)
                        t_key = now

                    for frame in dec.decode(packet):
                        w, h = frame.width, frame.height
                        if w <= 0 or h <= 0: continue
                        pix_fmt = frame.format.name
                        t_vframe = self.last_frame_time = now
                        self._session_frames += 1

                        if not first_frame_logged:
                            first_frame_logged = True
                            self._fail_streak = 0
                            self._last_fail = None
                            logging.info(f"[VideoWorker] First frame ({dec.name}, {'HW' if dec.is_hw else 'SW'}) "
                                         f"pix_fmt={pix_fmt} {w}x{h}")

                        if (w, h) != self._size:
                            self._size = (w, h)
                            self.sz_sig.emit(w, h)
                            logging.verbose(f"[VideoWorker] emit video width:{w}, height:{h}")

                        self._dispatch_av(1, frame)

                        if self.headless: continue

                        sk.update(fr.get_fps())
                        if sk.is_skip() and not frame.key_frame: continue

                        if self.opengl and pix_fmt in GL_DIRECT_FORMATS:
                            self.frame_q.append(DisplayFrame.from_av_frame(frame))
                        else:
                            self.frame_q.append(DisplayFrame.from_rgba_array(frame.to_ndarray(format="rgba")))
                        self.img_sig.emit()

                elif packet.stream.type == 'audio':
                    aframes = []
                    if audio_context is not None:
                        try:
                            aframes = audio_context.decode(packet)
                            a_errors = 0
                        except av.error.FFmpegError as e:
                            a_errors += 1
                            if a_errors == 1:
                                logging.verbose(f"[VideoWorker] audio packet rejected ({_short_err(e)}); skipped")
                            if a_errors >= MAX_AUDIO_ERRORS:
                                logging.warning(f"[VideoWorker] {a_errors} audio packets in a row failed to decode "
                                                f"({_short_err(e)}); audio is ignored until the next connection")
                                audio_context = None
                    for frame in aframes:
                        sr = frame.sample_rate or (audio_stream.rate if audio_stream else 0) or 48000
                        self.sample_rate = sr
                        self.channels = _nb_channels(frame)

                        if self.audio_on:
                            pcm_bytes, ch = _frame_to_pcm16(frame, self.channels)
                            if pcm_bytes:
                                _queue_nowait_put(self.audio_q, (pcm_bytes, sr, ch))

                        self._dispatch_av(2, frame)

                else:
                    logging.debug(f"[VideoWorker] {packet}")

                # watchdog: audio (or data) keeps the connection busy while the picture is gone
                if now - t_vpkt > VIDEO_STALL_S:
                    raise _Reconnect(f"no video for {now - t_vpkt:.0f} s while audio keeps flowing")
                if t_vframe is None:
                    if now - t_start > max(FIRST_PICTURE_S, 3 * self._gop_s()):
                        raise _Reconnect(f"no picture {now - t_start:.0f} s after connecting")
                elif now - t_vframe > max(VIDEO_STALL_S, 3 * self._gop_s()):
                    reason = f"no picture for {now - t_vframe:.0f} s"
                    if not dec.is_hw:
                        raise _Reconnect(reason)
                    dec.fallback_sw(f"{dec.name}: {reason}", False)
                    t_vframe = now
        except _Reconnect as r:
            self.stat_sig.emit(f"Reconnecting:\n{r}")
            self._log_fail(re.sub(r"\d+", "#", str(r)), f"[VideoWorker] {r}; reconnecting. [{u_disp}]")

    # ------------------------------------------------------------------ recorder / streamer hand-off
    def _dispatch_av(self, typ, frame):
        """Hand decoded frames to the recorder/streamer queues without copying.
        PyAV frames are independent refcounted buffers, so the first consumer gets the original;
        only when both are active is one copy made for the second (each writer sets pts and
        picture type on the frame it gets)."""
        rec, strm = self.rec_on, self.stream_on
        if not (rec or strm): return
        if rec and strm:
            clone = self._clone_frame(frame)
            self._put(self.rec_q, (typ, frame), "recorder")
            if clone is not None:
                self._put(self.stream_q, (typ, clone), "streamer")
        elif rec:
            self._put(self.rec_q, (typ, frame), "recorder")
        else:
            self._put(self.stream_q, (typ, frame), "streamer")

    def _put(self, q, item, name):
        """Queue a frame for the recorder/streamer. Frames dropped because it cannot keep up (slow disk,
        a software encoder at 4K) are reported at most every 10 s instead of vanishing silently."""
        if _queue_nowait_put(q, item):
            return
        n, t = self._drops.get(name, (0, 0.0))
        n, now = n + 1, time.monotonic()
        if now - t >= 10.0:
            logging.warning(f"[VideoWorker] {name} cannot keep up: {n} frame(s) dropped")
            n, t = 0, now
        self._drops[name] = (n, t)

    def _clone_frame(self, frame):
        try:
            return _copy_frame(frame)
        except Exception as e:
            if not self._clone_warned:
                self._clone_warned = True
                logging.error(f"[VideoWorker] frame copy failed ({frame.format.name}): {e}. "
                              f"Streaming copy skipped while recording is on.")
            return None


def _copy_plane(src, dst, rows):
    """Copy `rows` rows of a plane; line sizes may differ (alignment), the visible bytes fit both."""
    ls, ld = src.line_size, dst.line_size
    if ls <= 0 or ld <= 0:
        raise ValueError(f"unsupported line size {ls}/{ld}")
    n = min(ls, ld)
    s = np.frombuffer(memoryview(src), dtype=np.uint8, count=ls * rows)
    d = np.frombuffer(memoryview(dst), dtype=np.uint8, count=ld * rows)
    d.reshape(rows, ld)[:, :n] = s.reshape(rows, ls)[:, :n]

def _copy_frame(frame):
    """An independent copy of a decoded frame (own buffers, own pts), for any pixel / sample format
    and any size: yuvj422p from MJPEG, p010le from 10-bit HEVC, odd widths ...  frame.reformat() to the
    same format returns the frame itself, and to_ndarray() supports only some formats."""
    if isinstance(frame, av.VideoFrame):
        c = av.VideoFrame(frame.width, frame.height, frame.format.name)
        for s, d in zip(frame.planes, c.planes):
            _copy_plane(s, d, min(s.height, d.height))
        for attr in ("colorspace", "color_range", "color_primaries", "color_trc", "chroma_location"):
            try: setattr(c, attr, getattr(frame, attr))
            except Exception: pass
    else:
        c = av.AudioFrame(format=frame.format.name, layout=frame.layout, samples=frame.samples)
        c.sample_rate = frame.sample_rate
        for s, d in zip(frame.planes, c.planes):
            n = min(s.buffer_size, d.buffer_size)
            np.frombuffer(memoryview(d), dtype=np.uint8, count=n)[:] = np.frombuffer(memoryview(s), dtype=np.uint8, count=n)
    c.pts = frame.pts
    c.time_base = frame.time_base
    return c


def _dec_opts_for(ctx, user_opts):
    """The user's decoder options this decoder defines; those of other decoders (cuvid's -resize
    for a software decoder, say) are meant for them and left out quietly."""
    opts, skipped = for_codec(ctx, user_opts)
    if skipped:
        logging.debug(f"[VideoWorker] {ctx.codec.name}: --ffmpeg options of other decoders left out: {skipped}")
    return opts


def _report_unused_dec(ctx, user_opts):
    """After open(), PyAV leaves only the options the decoder did not recognise in ctx.options."""
    try:
        unused = {k: v for k, v in dict(ctx.options).items() if k in user_opts}
    except Exception:
        return
    if unused:
        logging.warning(f"[VideoWorker] ⚠️ decoder {ctx.codec.name} ignored unknown --ffmpeg options: {unused}")


def _nb_channels(frame) -> int:
    layout = frame.layout
    n = getattr(layout, "nb_channels", None)
    if n is None:
        chans = getattr(layout, "channels", None)
        n = len(chans) if chans is not None else None
    return int(n) if n else 1

def _frame_to_pcm16(frame, ch):
    """Decoded audio frame -> interleaved int16 bytes, channel count."""
    pcm = frame.to_ndarray()
    if pcm.ndim == 1:
        pcm = pcm.reshape(1, -1)
    if frame.format.is_planar:
        pcm = pcm.T                       # (C, N) -> (N, C)
        ch = pcm.shape[1]
    else:
        ch = max(1, ch)
        pcm = pcm.reshape(-1, ch)         # (1, N*C) -> (N, C)
    if pcm.dtype == np.int16:
        pcm16 = pcm
    elif pcm.dtype.kind == 'f':
        pcm16 = (np.clip(pcm, -1.0, 1.0) * 32767.0).astype(np.int16)
    elif pcm.dtype == np.int32:
        pcm16 = (pcm >> 16).astype(np.int16)
    elif pcm.dtype == np.uint8:
        pcm16 = ((pcm.astype(np.int16) - 128) << 8).astype(np.int16)
    else:
        pcm16 = pcm.astype(np.int16)
    return np.ascontiguousarray(pcm16).tobytes(), ch
