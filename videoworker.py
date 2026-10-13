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
from ffparser import FFmpegParser
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
# hwaccel device types PyAV can attach to the *standard* decoder via av.open(hwaccel=...).
# These are known to misbehave when attached at open time; they are only used as separate decoders.
# schemes whose host is probed with a plain TCP connect before av.open(), and their default ports
PROBE_PORTS = {"rtsp": 554, "rtsps": 322, "rtmp": 1935, "rtmps": 443, "http": 80, "https": 443, "tcp": None}
HWACCEL_UNSAFE_FOR_OPEN = {"qsv", "vulkan", "drm", "opencl"}
_CONNECT_IN_PROGRESS = {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY,
                        getattr(errno, "WSAEWOULDBLOCK", 10035)}

FPS_MAX = 120               # a larger "frame rate" is a clock rate (90000), not frames

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
    """Put without blocking; drop the oldest item when the queue is full."""
    try:
        q.put_nowait(i)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(i)
        except queue.Full:
            pass

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

def _build_conn_options(url):
    v = os.getenv("NOVA_TIMEOUT", "5000000")
    logging.trace(f"[VideoWorker] NOVA_TIMEOUT={v}")
    s = str(v)
    options = {}
    if not url: return options
    if url.startswith('rtsp'):
        options['rtsp_transport'] = 'tcp'
        options['timeout'] = s      # FFmpeg >= 5: socket I/O timeout (us)
        options['stimeout'] = s     # FFmpeg <  5: same option, old name (ignored by new builds)
        options['rw_timeout'] = s
    elif url.startswith(('rtmp', 'srt', 'http', 'tcp', 'udp')):
        options['rw_timeout'] = s
    return options

def _timeout_seconds() -> float:
    try:
        return max(1.0, int(os.getenv("NOVA_TIMEOUT", "5000000")) / 1_000_000)
    except ValueError:
        return 5.0

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

def _strerror(code: int) -> str:
    # socket.errorTab: Windows' WSA error texts (os.strerror() does not know them)
    return getattr(socket, "errorTab", {}).get(code) or os.strerror(code)


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
        self.force_sw = False           # set after a HW decoder proved unusable
        self._resolved = None           # resolved av.open() source
        self._hwaccel_attached = False  # av.open() was given an HWAccel for this connection

        self.ffopts = FFmpegParser.parse(ffmpeg)
        if self.ffopts.ignored:
            logging.warning(f"[VideoWorker] --ffmpeg: {' '.join(self.ffopts.ignored)} are ffmpeg command-line options "
                            f"without a library equivalent; ignored. (-vf, -r, -s, -pix_fmt, -ar, -ac, -metadata are supported.)")
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
        self.rec_q = queue.Queue(maxsize=120)     # ~4 s @ 30 fps; oldest frames are dropped when full
        self.stream_q = queue.Queue(maxsize=120)
        self.frame_q = deque(maxlen=1)
        self.clock = Clock()
        self.rec_on = False
        self.stream_on = False
        self.audio_on = False
        self.run_flag = True
        self.gpu_idx = _pick_gpu_index(gpu)
        self._unreachable = None        # last probe failure, to log state changes once
        self._url_warned = False
        self._url_hint = ""             # appended to a failure overlay when the URL cannot be parsed
        self._last_fail = None          # last connection failure logged (repeats are logged at DEBUG)
        self._fps_warned = False
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

            container = self._open_container(src)
            if container is None:
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

    # ------------------------------------------------------------------ connection
    def _resolve_source(self):
        """onvif:// xmeye:// hik:// dahua:// are resolved here, on the worker thread."""
        if self._resolved is not None:
            return self._resolved
        if not any(self.url.startswith(p) for p in ("onvif://", "xmeye://", "hik://", "dahua://")):
            self._resolved = self.url
            return self._resolved
        self.stat_sig.emit(f"Resolving...\n{display_url(self.url)}")
        try:
            from auth import resolve_input_uri
            self._resolved = resolve_input_uri(self.url)
        except Exception as e:
            logging.error(f"[VideoWorker] URI resolve failed: {e}")
            self._resolved = None
        return self._resolved

    def _hw_device(self, accel):
        if 'hwaccel_device' in self.dec_opts: return str(self.dec_opts['hwaccel_device'])
        if accel in ("cuda", "nvdec"): return str(self.gpu_idx)
        if accel == "vaapi": return f"/dev/dri/renderD{128 + self.gpu_idx}"
        return None

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
        if fmt: kwargs["format"] = fmt
        # PyAV's interrupt callback: a blocking open()/read() gives up after NOVA_TIMEOUT so that
        # stop() and reconnects never hang forever (UDP sources, dead cameras, ...)
        t_s = _timeout_seconds()
        if t_s > 0 and not fmt:
            kwargs["timeout"] = (t_s, t_s)

        self._hwaccel_attached = False
        if (self.accel and not self.force_sw and self.accel in HWACCELS
                and self.accel not in HWACCEL_UNSAFE_FOR_OPEN):
            try:
                kwargs["hwaccel"] = HWAccel(
                    device_type=self.accel,
                    device=self._hw_device(self.accel),
                    allow_software_fallback=False,
                    options=self.hw_opts or None,
                )
                self._hwaccel_attached = True
                logging.debug(f"[VideoWorker] av.open with hwaccel={self.accel} device={self._hw_device(self.accel)} opts={self.hw_opts}")
            except Exception as e:
                logging.warning(f"[VideoWorker] HWAccel({self.accel}) setup failed: {_short_err(e)}")

        # av.open() cannot be interrupted before its timeout, so a camera that is down would hold
        # stop() for up to NOVA_TIMEOUT.  Probe the host first in 0.5 s slices that honour run_flag.
        if not fmt and t_s > 0:
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
    def _open_named_decoder(self, name, base, hw=True):
        ctx = None
        try:
            codec = av.Codec(name, 'r')
            ctx = av.CodecContext.create(codec)
            ctx.width = base.width
            ctx.height = base.height
            if base.extradata:
                ctx.extradata = base.extradata
            for attr in ("pkt_timebase", "time_base"):
                try: setattr(ctx, attr, base.time_base); break
                except Exception: pass
            if hw:
                ctx.thread_type = "NONE"
                if "cuvid" in name:
                    ctx.options['gpu'] = str(self.gpu_idx)
                elif "qsv" in name:
                    ctx.options['qsv_device'] = str(self.gpu_idx) if (WINDOWS or MSYS) else f"/dev/dri/renderD{128 + self.gpu_idx}"
                elif any(x in name for x in ("rkmpp", "amf", "mediacodec")):
                    ctx.options['hwaccel_device'] = str(self.gpu_idx)
            ctx.options.update(self.dec_opts)
            logging.debug(f"[VideoWorker] trying decoder {name} options={ctx.options}")
            ctx.open()
            _report_unused_dec(ctx, self.dec_opts)
            return ctx
        except Exception as e:
            logging.debug(f"[VideoWorker] decoder {name} unavailable: {_short_err(e)}")
            return None

    def _setup_video_decoder(self, video_stream):
        """Returns (codec_context, is_hw)."""
        base = video_stream.codec_context
        codec_name = base.codec.name
        self.codec_name = codec_name
        logging.debug(f"[VideoWorker] Stream codec: {codec_name} ({get_friendly_codec_name(codec_name)}) "
                      f"{base.width}x{base.height}")

        if self.accel and not self.force_sw:
            # 1) dedicated HW decoder (h264_cuvid, hevc_qsv, h264_rkmpp, ...)
            for d_name in _decoder_candidates(self.accel, codec_name):
                ctx = self._open_named_decoder(d_name, base, hw=True)
                if ctx is not None:
                    logging.info(f"[VideoWorker] 🚀 HW decoder: {d_name}")
                    return ctx, True
            # 2) hwaccel attached to the standard decoder by av.open(hwaccel=...)
            #    (vaapi, videotoolbox, d3d11va, dxva2, vdpau, cuda/nvdec ...)
            if self._hwaccel_attached:
                try:
                    base.options.update(self.dec_opts)
                    base.open()
                    _report_unused_dec(base, self.dec_opts)
                    logging.info(f"[VideoWorker] 🚀 HW decoder: {codec_name} + {self.accel} hwaccel")
                    return base, True
                except Exception as e:
                    logging.warning(f"[VideoWorker] {self.accel} hwaccel failed for {codec_name}: {_short_err(e)}")
            logging.warning(f"[VideoWorker] ❌ No working HW decoder for --accel={self.accel}. "
                            f"Falling back to software ({codec_name}).")

        # 3) software (multi-threaded, libavcodec defaults)
        if self._hwaccel_attached:
            # `base` carries a hwaccel that just failed; build a clean software context instead.
            ctx = self._open_named_decoder(codec_name, base, hw=False)
            if ctx is not None:
                return ctx, False
            self.force_sw = True
            raise RuntimeError(f"software decoder {codec_name} failed to open; reconnecting without hwaccel")
        base.options.update(self.dec_opts)
        logging.debug(f"[VideoWorker] software decoder {codec_name} options={base.options}")
        try:
            base.open()
            _report_unused_dec(base, self.dec_opts)
        except Exception as e:
            logging.debug(f"[VideoWorker] explicit open of {codec_name} failed, decoding lazily: {_short_err(e)}")
        return base, False

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

    # ------------------------------------------------------------------ streaming
    def _stream_loop(self, container, u_disp):
        video_stream = next((s for s in container.streams if s.type == "video"), None)
        audio_stream = next((s for s in container.streams if s.type == "audio"), None)

        if video_stream is None:
            self.stat_sig.emit("No video stream")
            self.clock.sleep(10000)
            return

        context, is_hw = self._setup_video_decoder(video_stream)
        audio_context = audio_stream.codec_context if audio_stream else None

        fps_cap = self._frame_rate(video_stream)
        self.fps = fps_cap

        sk = Skipper(maxfps=self.fps_disp, o_fps=fps_cap)
        fr = FrameRator()
        logging.verbose(f"[VideoWorker] fps_disp:{self.fps_disp}, fps_cap:{fps_cap}")

        streams = [video_stream]
        if audio_stream is not None:
            streams.append(audio_stream)

        first_frame_logged = False
        keyframe_count = 0
        for packet in container.demux(*streams):
            if not self.run_flag: break
            if packet.dts is None: continue

            if packet.stream.type == 'video':
                if packet.is_keyframe: keyframe_count += 1
                if not first_frame_logged and keyframe_count > 3:
                    if is_hw:
                        logging.warning(f"[VideoWorker] {context.codec.name} produced no frames after "
                                        f"{keyframe_count} keyframes; reconnecting with software decoding. [{u_disp}]")
                        self.force_sw = True
                    else:
                        logging.warning(f"[VideoWorker] no decodable frames after {keyframe_count} keyframes; reconnecting. [{u_disp}]")
                    break

                for frame in context.decode(packet):
                    w, h = frame.width, frame.height
                    if w <= 0 or h <= 0: continue
                    pix_fmt = frame.format.name
                    self.last_frame_time = time.monotonic()

                    if not first_frame_logged:
                        first_frame_logged = True
                        self._last_fail = None
                        logging.info(f"[VideoWorker] First frame ({context.codec.name}, {'HW' if is_hw else 'SW'}) "
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
                if not audio_context: continue
                for frame in audio_context.decode(packet):
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
            _queue_nowait_put(self.rec_q, (typ, frame))
            if clone is not None:
                _queue_nowait_put(self.stream_q, (typ, clone))
        elif rec:
            _queue_nowait_put(self.rec_q, (typ, frame))
        else:
            _queue_nowait_put(self.stream_q, (typ, frame))

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
