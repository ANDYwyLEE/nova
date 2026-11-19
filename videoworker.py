"""VideoWorker: the decode thread of a camera: TCP probe and connection, shortcut URI resolution, hardware
decoder selection with software fallback and retry, reconnection, frames fanned out to the window, the
recorder and the streamer."""

import os
import math
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
from collections import deque

import numpy as np
from PySide6.QtCore import QThread, Signal

import logsync  # installs logging.verbose / logging.trace
from mytools import Skipper, FrameRator, Clock
from util import (LINUX, MAC, WSL, mask_url, display_url, vaapi_node, short_err as _short_err, hw_err as _hw_err,
                  accel_obstacle, v4l2_decoders, KNOWN_HWACCELS, cpu_idle)
from codec_names import get_friendly_codec_name
from ffparser import FFmpegParser, for_codec
from dispframe import DisplayFrame, GL_DIRECT_FORMATS, rgba_array
from campacket import CamStream, CamPacket, FRAME, PACKET

import av
from av.codec.hwaccel import HWAccel, hwdevices_available
from av.codec.context import ThreadType
from logsync import recent_av_errors

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
HWACCEL_DEDICATED_ONLY = {"qsv", "vulkan", "opencl"}
# A DRM device is attached where the build's decoder for the stream lists it among its hardware
# configurations, and nowhere else.  FFmpeg with Raspberry Pi's patches decodes HEVC that way on the
# board's own decoder (V4L2's request API; docs/custom-ffmpeg.md): a 2160p picture took 6.5 ms of CPU on a
# Pi 5 in place of 42 ms.  The wheels list it for no decoder.
HWACCEL_WHERE_LISTED = {"drm"}
LISTED_CODECS = ("hevc", "h264", "vp9", "av1", "vp8", "mpeg2video")
# Decoders that look for the stream's parameter sets (SPS/PPS, VPS) inside the packets only. A camera may
# send them in the RTSP session description alone, where FFmpeg keeps them as extradata: h264_qsv then
# answers "More data is required to decode header" to every packet and never delivers a picture
# (Intel UHD 620, November 2026), and a V4L2 decoder, which is handed the packets and nothing else, takes
# them all without a picture or a word (Raspberry Pi 4). Their keyframes get the parameter sets in front.
INBAND_HEADER_DECODERS = ("_qsv", "_v4l2m2m")
# Decoders whose pictures lie in memory of the device: a V4L2 decoder hands out its own buffers, and they
# are mapped without the CPU's cache.  Copying a 1080p picture out of one took 14 ms on a Raspberry Pi 4
# (2 ms from ordinary memory), the scaler reading it for a plain window 40 ms.  So such a picture is read
# once: by the OpenGL window's upload when nothing else wants it, else by one copy that the scaler, the
# recorder and the streamer then read (and the device has its buffer back before their queues fill).
DEVICE_MEMORY_DECODERS = ("_v4l2m2m",)
# Decoders whose calls may never come back.  FFmpeg's V4L2 decoders wait for the device without a limit,
# and a board's decoder that has no room for one more camera takes the packets it is given and answers
# nothing, no error either (Raspberry Pi 4: two 1080p cameras decode, the third stood in that call for
# good: a window without a picture and a log without a line).  Their calls are made on a thread of their
# own and given HW_ANSWER_S; a decoder that does not answer is left to that thread.  One that never gave
# a picture is a decoder that does not work: software, until the camera is started again.  One that had
# been decoding the camera is not replaced (it was asked for and it worked): the connection ends, and
# the next one asks the device again.
MAY_HANG_DECODERS = ("_v4l2m2m",)
# FFmpeg gives a V4L2 decoder 20 buffers for its pictures and 16 for the packets, all in the memory a
# board has for graphics: 86 MB for a 1080p camera, of the 256 MB a Raspberry Pi 3 has for that in all.
# With a full-screen OpenGL window beside it the GPU found no memory there, and the window lost its
# display.  Nova holds two or three pictures at a time: a 25 fps stream ran with 10 and 4 without
# FFmpeg's "All capture buffers returned to userspace" (with 8 it came once).  --ffmpeg names others.
V4L2_DECODER_BUFFERS = {"num_capture_buffers": "12", "num_output_buffers": "4"}
HW_ANSWER_S = 5.0            # less than the 10 s without a picture that end a connection
_LEFT_BEHIND = []           # decoders that never answered: their thread is inside them, they are not freed
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
AUDIO_STALL_S = 30.0        # audio that flowed on this connection silent this long (video still flowing): reconnect
AUDIO_STALL_MAX_S = 3600.0  # the wait doubles up to this while a camera keeps dropping its audio early
# One make of camera counts its time in 1/360000 s in 32 bits: every 3 h 18 min 50 s (2^32 / 360000 s) the
# counter starts again, its video timestamps go back by 2^30 and it stops sending audio to the sessions that
# exist (captured; four cameras, each at its own moment since it was switched on). Video timestamps that go
# back this far with no audio after them: that is known within seconds, not after the usual wait.
TS_BACK_S = 60.0            # video timestamps that go back by more than this are a clock that started again
AUDIO_AFTER_BACK_S = 5.0    # ... and audio missing this long after it does not come back on this connection
GAP_SAID_S = 5.0            # picture or sound that comes back on its connection after this long: the log says so
FIRST_PICTURE_S = 60.0      # no picture at all this long after connecting: reconnect
GOP_UNKNOWN_S = 20.0        # keyframe interval assumed until two keyframes have been seen
FPS_MAX = 120               # a larger "frame rate" is a clock rate (90000), not frames
LOAD_JUDGE_S = 2.0          # the software decoder's thread is judged over stretches of this long ...
LOAD_BUSY = 0.9             # ... and does not keep up when its pictures take this share of the time
LOAD_THREADS_MAX = 16       # frame threads the software decoder gets at most (FFmpeg's limit for its own choice)
LOAD_FREE = 0.5             # with less than this many cores idle on the machine, a late picture is nobody's verdict
LOAD_HW_ROW = 2             # stretches in a row a GPU's decoder has to be behind before the log says so ...
LOAD_SHORT = 0.9            # ... with fewer than this share of the camera's pictures coming out of it
LOAD_LATE_S = 10.0          # late for this long with every core busy: the log says so ...
LOAD_SETTLED_S = 60.0       # ... but not while the cameras of a wall are still starting beside this one
CONV_NAMED = 0.1            # the conversion for the window is named in those lines from this share of a picture's time on
RERESOLVE_AFTER = 3         # connection attempts without a picture before onvif:// etc. are resolved again
RERESOLVE_MIN_S = 60.0


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

def _audio_stall_s() -> float:
    """NOVA_AUDIO_STALL in seconds (default 30; 0 never reconnects for missing audio)."""
    v = os.getenv("NOVA_AUDIO_STALL", "")
    try:
        return max(0.0, float(v)) if v.strip() else AUDIO_STALL_S
    except ValueError:
        return AUDIO_STALL_S


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
    """A name Nova knows how to try (an unknown one ends the camera). Whether this FFmpeg build can do it
    is accel_in_build's question; a known backend the build lacks falls back to software decoding."""
    a = (accel or "").lower()
    return (not a) or a in HWACCELS or a in SEPARATE_DECODER_SUFFIX or a in KNOWN_HWACCELS


def accel_in_build(accel: str) -> bool:
    """Can this FFmpeg build decode with --accel at all: its device type (vaapi, d3d11va, ...) is in the
    build, or at least one of its dedicated decoders (h264_cuvid, hevc_qsv, ...). The arm64 wheel has
    neither NVIDIA's nor Intel's decoders; nova doctor said `--accel cuda has a decoder` there."""
    a = (accel or "").lower()
    if not a or a in HWACCELS:
        return True
    suffix = SEPARATE_DECODER_SUFFIX.get(a)
    return bool(suffix) and any(n.endswith("_" + suffix) for n in CODECS_AVAILABLE)

def hwaccel_listed(codec, accel: str) -> bool:
    """Does this build's decoder `codec` (an av.Codec, or its name) decode through a device of the type
    `accel`: FFmpeg's hardware configurations of the decoder name it."""
    try:
        if isinstance(codec, str):
            codec = av.Codec(codec, "r")
        return any(c.device_type.name == accel for c in codec.hardware_configs)
    except Exception:
        return False


def listed_codecs(accel: str) -> list[str]:
    """The decoders among LISTED_CODECS that decode through a device of the type `accel` in this build."""
    return [name for name in LISTED_CODECS if hwaccel_listed(name, accel)]


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


def _v4l2_buffers(name, opts):
    """`opts` for decoder `name`, with V4L2_DECODER_BUFFERS for a V4L2 decoder where the user named none."""
    if not name.endswith("_v4l2m2m"):
        return opts
    return {**V4L2_DECODER_BUFFERS, **opts}


def _has_param_sets(data, hevc):
    """Does this Annex B access unit carry parameter sets (H.264 SPS/PPS, HEVC VPS/SPS/PPS) before its
    first slice?"""
    i = 0
    while True:
        j = data.find(b"\x00\x00\x01", i)
        if j < 0 or j + 3 >= len(data):
            return False
        h = data[j + 3]
        if hevc:
            t = (h >> 1) & 0x3f
            if t in (32, 33, 34): return True
            if t < 32: return False                 # a slice: nothing came before it
        else:
            t = h & 0x1f
            if t in (7, 8): return True
            if 1 <= t <= 5: return False
        i = j + 3


class _Reconnect(Exception):
    """Ends the current connection; the text says why."""


class _NoAnswer(Exception):
    """A decoder's call did not come back within HW_ANSWER_S."""


class _Answering:
    """decode() of a decoder of MAY_HANG_DECODERS: on a thread of its own, with a time limit."""
    _END = object()

    def __init__(self, ctx, name):
        self.ctx, self.name, self.hung = ctx, name, False
        self._in, self._out = queue.SimpleQueue(), queue.SimpleQueue()
        threading.Thread(target=self._run, name="HWDecode", daemon=True).start()

    def _run(self):
        while True:
            packet = self._in.get()
            if packet is self._END:
                return
            try:
                self._out.put((self.ctx.decode(packet), None))
            except BaseException as e:
                self._out.put((None, e))

    def decode(self, packet):
        self._in.put(packet)
        try:
            frames, err = self._out.get(timeout=HW_ANSWER_S)
        except queue.Empty:
            self.hung = True
            _LEFT_BEHIND.append(self)
            raise _NoAnswer(f"{self.name} has not answered for {HW_ANSWER_S:g} s (a board's decoder that has no "
                            f"room for one more camera takes its packets and stays silent)") from None
        if err is not None:
            raise err
        return frames

    def end(self):
        if not self.hung:
            self._in.put(self._END)


class _Load:
    """Does the connection's thread keep up with the camera?  It decodes a picture, converts it and hands
    it on before it reads the next packet, so a thread that works nearly all the time while no more
    pictures come out of it than the camera sends is falling behind: the camera's packets pile up, and
    the camera drops some and then the connection.  (A file, or the burst a camera opens with, is read
    faster than the frame rate: busy as well, and not behind.)

    Behind is no verdict while the machine has no core to spare (`idle`, util.cpu_idle; Linux).  Eight
    windows starting at once on a four-core board made a picture take 47 ms through the board's HEVC
    decoder, which needs 16: the decoder was called too slow and left for the software one, which
    takes four times the CPU, and the next camera followed.  Neither another decoder nor more threads
    help where every core is busy; `late` counts the stretches that have been behind that way.

    A connection's first stretch is no verdict either, while the connection goes on: the camera opens
    with a burst, and what starts beside the thread (the recorder and its encoder, the window) takes
    time from it.  On a Raspberry Pi 3 B+ a 10 fps camera that takes 40 ms a picture took 97 for the two
    seconds in which its recording started: the decoder got a thread it did not need, by a reconnection
    that left a hole at the start of the file.  A connection that ends inside that stretch is judged
    (`end`): that one may be the camera giving up on a thread that is behind."""

    def __init__(self, fps, idle=None, late=0):
        self.fps = fps
        self.idle = idle        # the machine's idle CPU seconds so far (a function), or None: not asked
        self.t0 = None          # start of the stretch being judged
        self.first = True       # ... and it is the connection's first
        self.i0 = None          # idle() at that moment
        self.busy = 0.0         # seconds of it spent on video packets
        self.converting = 0.0   # of those, the seconds spent converting pictures for the window
        self.pictures = 0
        self.conv = 0.0         # of the seconds a picture the last verdict returned: the conversion's
        self.late = late        # stretches in a row that were behind while every core was busy
        self.free = None        # the cores that were idle during the last such stretch
        self.short = True       # the last verdict's stretch gave fewer pictures than the camera sends (LOAD_SHORT)
        self.row = 0            # stretches in a row that ended in a verdict and were short of pictures

    def add(self, now, worked, pictures, converting=0.0):
        """A video packet that arrived at `now` and took `worked` seconds, `converting` of them for the
        conversion of its pictures.  Returns the seconds a picture takes when the stretch that ends
        here was too much for the thread, else None; `conv` then holds the conversion's part of them."""
        if self.t0 is None:
            self.t0, self.i0 = now, self.idle() if self.idle else None
        self.busy += worked
        self.converting += converting
        self.pictures += pictures
        return self._judge(now + worked, LOAD_JUDGE_S, starting=True)

    def end(self, now):
        """The same for the stretch the end of the connection cut short: an overloaded connection may not
        live long enough for a whole one."""
        return self._judge(now, LOAD_JUDGE_S / 2)

    def _judge(self, now, least, starting=False):
        if self.t0 is None or now - self.t0 < least:
            return None
        first, self.first = self.first, False
        span, busy, converting, n = now - self.t0, self.busy, self.converting, self.pictures
        i0, i1 = self.i0, self.idle() if self.idle else None
        free = max(0.0, i1 - i0) / span if i0 is not None and i1 is not None and span > 0 else None
        self.t0, self.i0, self.busy, self.converting, self.pictures = now, i1, 0.0, 0.0, 0
        if n and busy >= LOAD_BUSY * span and n <= self.fps * span * 1.1 and not (first and starting):
            if free is not None and free < LOAD_FREE:
                self.late, self.free, self.row = self.late + 1, free, 0
                return None
            self.short = n < self.fps * span * LOAD_SHORT
            self.late, self.row = 0, self.row + 1 if self.short else 0
            self.conv = min(converting, busy) / n
            return busy / n
        self.late = self.row = 0
        return None


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
        # PyAV leaves the demuxer's decoder context without a packet time base; cuvid / qsv rescale
        # timestamps with it and warn at every start ("Invalid pkt_timebase, passing timestamps as-is")
        try:
            if not self.base.pkt_timebase and stream.time_base:
                self.base.pkt_timebase = stream.time_base
        except Exception as e:
            logging.debug(f"[VideoWorker] pkt_timebase not set: {_short_err(e)}")
        self.ctx = None
        self.is_hw = False
        self.name = ""
        self.kf = 0              # keyframes fed to the decoder before its first picture
        self.got = False         # the decoder delivered a picture
        self.errors = 0          # packets in a row the decoder rejected
        self.t_rejected = 0.0    # when the first of them was (time.time())
        self.last_err = ""       # the last rejection, for the give-up message
        self.wait_kf = False     # the decoder (re)starts at the next keyframe
        self.headers = None      # parameter sets the decoder in use needs in its keyframes (else None)
        self.copies = False      # the decoder in use hands out device memory (DEVICE_MEMORY_DECODERS)
        self.guard = None        # the decoder in use is called through this (MAY_HANG_DECODERS), else None
        self.trial_guard = None
        self.threads = 0         # frame threads the worker gave the software decoder (0: FFmpeg's default)
        self.trial = None        # hardware decoder on trial, fed alongside the software one
        self.trial_name = ""
        self.trial_headers = None
        self.trial_kf = 0
        self.trial_errors = 0
        self._headers_told = None
        self.skip_upto = None    # after a switch: drop pictures the previous decoder already delivered
        self.skipped = 0
        self.last_pts = None

    def open(self):
        w = self.w
        if w._hw_never_codec and w._hw_never_codec != self.codec_name:
            w._hw_fail = w._hw_never_codec = None       # the camera sends another codec than the one --accel is not for
        if w.accel and w._hw_fail is None:
            never = w._accel_never()
            other = None if never else w._accel_not_for(self.base.codec)
            if never:
                w._hw_failed(never, self.codec_name, never=True)
            elif other:
                w._hw_failed(other, self.codec_name, never=True)
                w._hw_never_codec = self.codec_name
            else:
                ctx, name, err = w._open_hw_decoder(self.base, self.codec_name)
                if ctx is not None:
                    # "🚀 HW decoder" is said by decode(), at its first picture: one that opens and then
                    # delivers nothing (a GPU without its firmware, a stream it refuses) is not one
                    logging.debug(f"[VideoWorker] hardware decoder {name} opened; no picture from it yet")
                    self._use(ctx, True, name)
                    return
                w._hw_failed(err, self.codec_name)
        self._use_sw()

    def release(self):
        """To be called when the connection is over, before its objects are let go.  PyAV (19.0.2; changed
        in its main branch since, issue 2440) frees a decoder while it holds the interpreter lock, and
        FFmpeg waits there for the decoder's frame threads: a thread that is still decoding a picture
        and has a line for the log waits for that same lock, and the process stands still for good,
        Ctrl-C and `nova stop` included.  So the pictures in flight are decoded out first, with the lock
        released as in every decode call; threads that have no work left say nothing when they are ended."""
        for guard in (self.guard, self.trial_guard):
            if guard is not None:
                guard.end()
        self.guard = self.trial_guard = None
        seen = []
        for ctx in (self.base, self.ctx, self.trial):
            if ctx is None or any(ctx is c for c in seen):
                continue
            seen.append(ctx)
            try:
                if ctx.is_open and ctx.thread_type & ThreadType.FRAME:
                    ctx.decode(None)
            except Exception as e:
                logging.debug(f"[VideoWorker] decoder not drained: {_short_err(e)}")

    def _use(self, ctx, is_hw, name):
        self.ctx, self.is_hw, self.name = ctx, is_hw, name
        self.kf, self.got, self.errors, self.last_err = 0, False, 0, ""
        self.headers = self._headers_for(name) if is_hw else None
        self.copies = is_hw and name.endswith(DEVICE_MEMORY_DECODERS)
        self.w.decoder, self.w.decoder_hw = name, is_hw
        if self.guard is not None and self.guard.ctx is not ctx:
            self.guard.end()
            self.guard = None
        if self.trial_guard is not None and self.trial_guard.ctx is ctx:        # the trial takes over
            self.guard, self.trial_guard = self.trial_guard, None
        elif is_hw and self.guard is None:
            self.guard = self._guard_for(ctx, name)

    @staticmethod
    def _guard_for(ctx, name):
        return _Answering(ctx, name) if name.endswith(MAY_HANG_DECODERS) else None

    def _headers_for(self, name):
        """The stream's parameter sets when decoder `name` has to find them inside the packets, else None.
        Only Annex B streams (RTSP, MPEG-TS): for MP4-style packets the decoder's own bitstream filter
        inserts them."""
        if not name.endswith(INBAND_HEADER_DECODERS) or self.codec_name not in ("h264", "hevc"):
            return None
        ed = bytes(getattr(self.base, "extradata", None) or b"")
        if ed[:4] != b"\x00\x00\x00\x01" and ed[:3] != b"\x00\x00\x01":
            return None
        return ed

    def _with_headers(self, packet, headers):
        """The keyframe `packet` as a decoder of INBAND_HEADER_DECODERS needs it: with the parameter sets
        in front, unless the camera put them there itself."""
        data = bytes(packet)
        if _has_param_sets(data, self.codec_name == "hevc"):
            return packet
        if headers is not self._headers_told:
            self._headers_told = headers
            logging.debug(f"[VideoWorker] the camera sends no parameter sets inside the stream: "
                          f"adding the {len(headers)} bytes of the session description to each keyframe")
        p = av.Packet(headers + data)
        p.pts, p.dts = packet.pts, packet.dts
        if packet.time_base is not None:
            p.time_base = packet.time_base
        if packet.duration:
            p.duration = packet.duration
        p.is_keyframe = True
        return p

    def _use_sw(self):
        b = self.base
        if b.is_open:
            b.flush_buffers()           # used before a hardware decoder took over
        else:
            opts = _dec_opts_for(b, self.w.dec_opts)
            b.options.update(opts)
            if self.w._sw_threads:          # one thread was not enough for this camera (VideoWorker._slow)
                try:
                    b.thread_type, b.thread_count = ThreadType.FRAME, self.w._sw_threads
                    self.threads = self.w._sw_threads
                except Exception as e:
                    logging.debug(f"[VideoWorker] frame threads not set: {_short_err(e)}")
            logging.debug(f"[VideoWorker] software decoder {self.codec_name} options={b.options}"
                          + (f", {self.threads} frame threads" if self.threads else ""))
            try:
                b.open()
                _report_unused_dec(b, opts)
            except Exception as e:
                logging.debug(f"[VideoWorker] explicit open of {self.codec_name} failed, decoding lazily: {_short_err(e)}")
        self._use(b, False, self.codec_name)

    def fallback_sw(self, reason, key, never=False):
        """The hardware decoder failed while decoding: software from now on, starting with this
        packet when it is a keyframe, else with the next keyframe."""
        self.w._hw_failed(reason, self.codec_name, never=never)
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
            frames = (self.guard or self.ctx).decode(self._with_headers(packet, self.headers)
                                                     if key and self.headers else packet)
        except _NoAnswer as e:
            self.guard = None                       # left to its thread, with its decoder (_LEFT_BEHIND)
            if self.got:
                # It was attached and delivered pictures: not Nova's to take the camera off it.  The
                # connection ends, and the next one asks the device for a decoder again.
                raise _Reconnect(f"{e}; it had been decoding this camera, and is asked again") from None
            self.fallback_sw(str(e), key, never=True)
            return self.decode(packet) if key else []
        except av.error.FFmpegError as e:
            return self._rejected(packet, key, e)
        self.errors = 0
        if frames:
            if self.is_hw and not self.got:
                logging.info(f"[VideoWorker] 🚀 HW decoder: {self.name}")
            self.got = True
        return self._fresh(frames)

    def _rejected(self, packet, key, e):
        self.errors += 1
        self.last_err = _short_err(e)
        if self.errors == 1:
            self.t_rejected = time.time()
            logging.verbose(f"[VideoWorker] {self.name} rejected a video packet ({_short_err(e)}); skipped")
        if self.errors < MAX_DECODE_ERRORS:
            return []
        # FFmpeg said why at the first of them, not at the last ("Unsupported sw format: yuvj420p")
        why = "; ".join(recent_av_errors(self.t_rejected - 1.0)) or _short_err(e)
        reason = f"{self.name}: {self.errors} video packets in a row failed to decode ({why})"
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
        self.trial_headers = self._headers_for(name)
        self.trial_guard = self._guard_for(ctx, name)

    def _feed_trial(self, packet, key):
        if key:
            self.trial_kf += 1
        try:
            frames = (self.trial_guard or self.trial).decode(self._with_headers(packet, self.trial_headers)
                                                             if key and self.trial_headers else packet)
        except _NoAnswer as e:
            self.trial_guard = None
            self._end_trial(str(e), never=True)
            return None
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

    def _end_trial(self, reason, never=False):
        if self.trial_guard is not None:
            self.trial_guard.end()
            self.trial_guard = None
        self.trial = None
        self.w._hw_failed(reason, self.codec_name, never=never)


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
        self._resolve_reason = None     # why, for the window ('XMeye: python-dvr is not installed: ...')

        self.ffopts = FFmpegParser.parse(ffmpeg)
        for msg in self.ffopts.warnings:
            logging.warning(f"[VideoWorker] --ffmpeg {msg}")
        for msg in self.ffopts.notes:
            logging.info(f"[VideoWorker] --ffmpeg {msg}")
        self.hw_opts = self.ffopts.hw
        self.open_opts = self.ffopts.open
        self.dec_opts = self.ffopts.dec
        self._sw_threads = 0            # frame threads for the software decoder, once one thread proved too slow
        self._late = 0                  # _Load.late, kept from one connection to the next
        self._late_told = False
        self._born = time.monotonic()
        self._hw_slow_told = False      # the line about a GPU's decoder that does not keep up has been written
        self._slow_told = False         # the line about a CPU that cannot keep up was written
        self.enc_v_opts = self.ffopts.enc_v
        self.enc_a_opts = self.ffopts.enc_a

        self.fps = 0
        self._size = (0, 0)             # (w, h) of the last picture; one tuple, so readers never see half an update
        self.last_frame_time = 0.0      # time.monotonic() of the last decoded video frame (0.0: none yet)
        self.sample_rate = 0
        self.channels = 0
        self.audio_codec = None         # 'pcm_alaw 8000 Hz mono' once connected (nova status, the log)
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
        # -e copy: who is handed the camera's packets in place of pictures, and the codec that was refused
        # for (a container that does not carry it): that one gets pictures to encode while the camera sends it
        self.copy_asked = {"recorder": False, "streamer": False}
        self._copy_refused = {}
        self._a_stall_wait = _audio_stall_s()   # how long audio may be missing before a reconnect (0: never)
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
        self.decoder, self.decoder_hw = "", False       # the decoder of the connection, for `nova status`
        self._hw_retry_at = 0.0         # monotonic time after which hardware is tried again
        self._hw_never_codec = None     # the codec --accel was found not to be for (_accel_not_for)
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
    def set_rec_copy(self, v: bool): self.copy_asked["recorder"] = bool(v)
    def set_stream_copy(self, v: bool): self.copy_asked["streamer"] = bool(v)
    def refuse_copy(self, who, codec): self._copy_refused[who] = codec
    def allow_copy(self, who): self._copy_refused.pop(who, None)

    def _wants_packets(self, who):
        """Does the recorder / the streamer write this connection's video as it comes (-e copy)?"""
        return self.copy_asked[who] and self._copy_refused.get(who) != self.codec_name

    def _frames_to(self, who):
        """Are pictures queued for the recorder / the streamer?"""
        return (self.rec_on if who == "recorder" else self.stream_on) and not self._wants_packets(who)

    def _pictures_wanted(self, cam):
        """Does anything take this connection's pictures?  Not where no window shows them and whoever
        writes takes the camera's packets (`cam`: -e copy has been asked for, and the size of the picture
        is known without decoding one): such a camera is not decoded at all."""
        if not self.headless or cam is None or not cam.width:
            return True
        return self._frames_to("recorder") or self._frames_to("streamer")

    def _audio_wanted(self):
        """Something uses this camera's sound: it is played, or a recording or stream carries it."""
        return self.audio_on or ((self.rec_on or self.stream_on) and not getattr(self.ffopts, "no_audio", False))
    def get_gpu_idx(self): return self.gpu_idx
    def gpu_given(self): return self._gpu_given     # --gpu or NOVA_GPU_DEVICES named one
    def get_stream_codec(self): return self.codec_name
    def get_ffopts(self): return self.ffopts

    # ------------------------------------------------------------------ main loop
    def run(self):
        logging.debug("[VideoWorker] started")

        if self.accel and not accel_supported(self.accel):
            names = sorted(set(KNOWN_HWACCELS) | set(SEPARATE_DECODER_SUFFIX) | set(HWACCELS))
            logging.critical(f"[VideoWorker] ❌ --accel={self.accel} is not a hardware decoder Nova knows "
                             f"(a typing error?). The names: {', '.join(names)}; this FFmpeg build has "
                             f"{', '.join(HWACCELS) or 'none of them'}")
            self.stat_sig.emit(f"Unsupported --accel={self.accel}")
            self.run_flag = False
            self.exit_sig.emit()
            return

        while self.run_flag:
            src = self._resolve_source()
            if not self.run_flag: break
            if src is None:
                self.stat_sig.emit(f"Failed:\n{self._resolve_reason or 'URI resolve'}\n{display_url(self.url)}")
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
        """Shortcut URIs (onvif:// xmeye:// hik:// dahua:// reolink:// ...: auth.SHORTCUT_DEFAULT_PORTS) are
        resolved here, on the worker thread.  The result is kept; after RERESOLVE_AFTER attempts in a row
        without a picture it is resolved again, bypassing the disk cache (the camera may have moved to
        another port, or the first answer was a fallback)."""
        from auth import shortcut_scheme, resolve_input_uri, last_failure
        if shortcut_scheme(self.url) is None:
            self._resolved = self.url
            return self._resolved
        again = self._resolved is not None
        if again and not (self._fail_streak >= RERESOLVE_AFTER
                          and time.monotonic() - self._resolve_t >= RERESOLVE_MIN_S):
            return self._resolved
        self.stat_sig.emit(f"Resolving...\n{display_url(self.url)}")
        url = None
        try:
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
            self._resolve_reason = _mask(last_failure() or "")[:80] or None     # shown in the window
        return self._resolved

    def _hw_device(self, accel):
        if self.ffopts.hw_device: return str(self.ffopts.hw_device)
        if accel in ("cuda", "nvdec"): return str(self.gpu_idx)
        if accel == "vaapi": return vaapi_node(self.gpu_idx if self._gpu_given else None)
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
    def _accel_never(self):
        """Why --accel cannot decode on this machine whatever the camera sends, so that it is said once
        and not tried every few minutes: the FFmpeg build was made without it, or the machine has no GPU
        for it (util.accel_obstacle).  None: it is worth trying."""
        a = self.accel.lower()
        if not accel_in_build(a):
            custom = "; docs/custom-ffmpeg.md builds one that has it" if a in ("vaapi", "vdpau") else ""
            return f"this FFmpeg build was made without {a} (it has: {', '.join(HWACCELS) or 'none'}{custom})"
        if a == "vdpau" and not os.environ.get("DISPLAY"):
            return "VDPAU decodes through an X display, and this camera runs without one (--accel vaapi does not need it)"
        if a in HWACCEL_WHERE_LISTED and not listed_codecs(a):
            return (f"no decoder of this FFmpeg build works through a {a.upper()} device (docs/custom-ffmpeg.md "
                    f"builds the one that decodes HEVC on a Raspberry Pi's decoder)")
        return accel_obstacle(a)

    def _accel_not_for(self, codec):
        """Why --accel cannot decode what this camera sends, while it decodes something else here: a
        Raspberry Pi 5's decoder is for HEVC, and the cameras beside it send H.264.  Said once for the
        camera, and asked again when it sends another codec.  None: it is worth trying."""
        a = self.accel.lower()
        if a in HWACCEL_WHERE_LISTED and not hwaccel_listed(codec, a):
            return (f"this FFmpeg build decodes {' and '.join(listed_codecs(a))} through a {a.upper()} device, "
                    f"and the camera sends {codec.name}")
        if a == "v4l2m2m":
            # a Raspberry Pi 4's is for H.264: an HEVC camera met FFmpeg's "Could not find a valid device"
            # at every connection, and a new try every five minutes
            takes = v4l2_decoders()
            if takes and codec.name not in takes:
                return f"this machine's V4L2 decoder takes {' and '.join(takes)}, and the camera sends {codec.name}"
        return None

    def _hw_failed(self, reason, codec_name, never=False):
        """A hardware decoder could not be used: software now, hardware again after a delay that
        doubles while it keeps failing (5 min ... 1 h); never again when the machine cannot have it."""
        if self._hw_fail is None:
            self._hw_backoff = HW_RETRY_S
        else:
            self._hw_backoff = min(HW_RETRY_MAX_S, self._hw_backoff * 2)
        # up to a fifth later than said: cameras that lost the GPU together must not all come back for it
        # in the same second (an old card under nouveau fails to make a decoder often enough as it is)
        self._hw_retry_at = float("inf") if never else time.monotonic() + self._hw_backoff * random.uniform(1.0, 1.2)
        if never:
            msg = (f"[VideoWorker] ❌ --accel={self.accel} cannot work here: {_mask(reason)}. "
                   f"Decoding in software ({codec_name}).")
        else:
            msg = (f"[VideoWorker] ❌ No working HW decoder for --accel={self.accel} ({_mask(reason)}). "
                   f"Falling back to software ({codec_name}); hardware is tried again in {_delay(self._hw_backoff)}.")
        if reason != self._hw_fail:
            logging.warning(msg)
        else:
            logging.debug(msg)
        self._hw_fail = reason

    def accel_note(self):
        """For `nova status`: a camera that was given --accel and is decoded on the CPU, and why; None when
        there is nothing of the kind to say.  The line that said so in the log is a warning, which a camera
        started with -l ERROR or CRITICAL does not write: whoever names a decoder has to be able to see
        that it is not the one in use."""
        if not self.accel or self._hw_fail is None or self.decoder_hw:
            return None
        if not self.decoder and self._session_frames:
            return None                 # nothing is decoded (-e copy, and no window): nothing runs on the CPU
        return f"--accel {self.accel} is not in use ({_mask(self._hw_fail)}): decoded on the CPU"

    def _hw_succeeded(self, name):
        if self._hw_fail is not None:
            logging.info(f"[VideoWorker] 🚀 HW decoder: {name} (hardware decoding again)")
        self._hw_fail = None
        self._hw_backoff = HW_RETRY_S

    def _hw_retry_due(self):
        return bool(self.accel) and self._hw_fail is not None and time.monotonic() >= self._hw_retry_at

    def _open_hw_decoder(self, base, codec_name):
        """(ctx, label, None) for an opened hardware decoder, else (None, None, reason).  An NVIDIA index
        that does not open (--gpu 1 on a machine with one card, a cameras.ini carried over from a machine
        with more) is answered with GPU 0, not with software decoding and libx264 for that camera."""
        ctx, label, reason = self._open_hw_decoder_on(base, codec_name)
        if ctx is None and self.accel in ("cuda", "cuvid", "nvdec") and self.gpu_idx != 0 and not self.ffopts.hw_device:
            asked, self.gpu_idx = self.gpu_idx, 0
            ctx, label, _ = self._open_hw_decoder_on(base, codec_name)
            if ctx is None:
                self.gpu_idx = asked
            else:
                reason = None
                logging.warning(f"[VideoWorker] ⚠️ GPU {asked} (--gpu / NOVA_GPU_DEVICES) does not open on this "
                                f"machine; using GPU 0 (nvidia-smi lists the cards there are)")
        return ctx, label, reason

    def _open_hw_decoder_on(self, base, codec_name):
        """_open_hw_decoder on the GPU self.gpu_idx names."""
        errors = []
        # 1) dedicated HW decoder (h264_cuvid, hevc_qsv, h264_rkmpp, ...)
        for d_name in _decoder_candidates(self.accel, codec_name):
            ctx, err = self._open_named_decoder(d_name, base)
            if ctx is not None:
                return ctx, d_name, None
            errors.append(f"{d_name}: {err}")
        # 2) the standard decoder with a hardware device (vaapi, videotoolbox, d3d11va, dxva2, vdpau, cuda ...)
        if (self.accel in HWACCELS and self.accel not in HWACCEL_DEDICATED_ONLY
                and (self.accel not in HWACCEL_WHERE_LISTED or hwaccel_listed(base.codec, self.accel))):
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
            ctx.options.update(_v4l2_buffers(name, opts))
            logging.debug(f"[VideoWorker] trying decoder {name} options={ctx.options}")
            ctx.open()
            _report_unused_dec(ctx, opts)
            return ctx, None
        except Exception as e:
            logging.debug(f"[VideoWorker] decoder {name} unavailable: {_hw_err(e)}")
            return None, _hw_err(e)

    def _open_hwaccel_decoder(self, base):
        """(ctx, None) or (None, reason): the stream's own decoder with --accel's device attached."""
        device = self._hw_device(self.accel)
        try:
            hw = HWAccel(device_type=self.accel, device=device, allow_software_fallback=False,
                         options=self.hw_opts or None)
            ctx = av.CodecContext.create(base.codec, "r", hwaccel=hw)     # creates the device
        except Exception as e:
            logging.debug(f"[VideoWorker] {self.accel} device {device} unavailable: {_hw_err(e)}")
            return None, _hw_err(e)
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
            logging.debug(f"[VideoWorker] {self.accel} hwaccel failed for {base.codec.name}: {_hw_err(e)}")
            return None, _hw_err(e)

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

    def _slow(self, per, fps, dec=None, conv=0.0, row=LOAD_HW_ROW, short=True):
        """The connection's thread needs `per` seconds for a picture (None: it keeps up), `conv` of them
        to convert it for the window, and the camera sends `fps` a second.  More decoder threads from
        the next connection on, and the words that say so are returned; where that is not to be had
        (every core in use already, or --ffmpeg names the threads), one line in the log about what
        needs less, and None.  A GPU's decoder that is the slow one (an old card on a camera of today,
        a board's one decoder with too many cameras on it) stays in use: it was asked for, it opened and
        it delivers pictures, and whether the CPU would do better is not Nova's to decide (on a small
        board the software decoder is the costlier one by far).  One line says so, once, with what
        helps; not for one late stretch (`row`: how many have followed one another), and not while every
        picture the camera sends still comes out of the decoder (`short`): cameras that share a decoder
        wait for it most of the time and are in time all the same.  Where the conversion is the slow
        part, no decoder and no number of its threads changes anything: the line says that, and nothing
        is changed."""
        if not per:
            return None
        every = 1.0 / fps
        conv = min(max(conv, 0.0), per)
        decoding = per - conv
        ms = lambda s: f"{s * 1000:.0f} ms"
        named = conv >= CONV_NAMED * per                # worth a word in the line
        hw = dec is not None and dec.is_hw
        if conv >= LOAD_BUSY * every or (hw and conv > decoding):
            self._tell_slow(f"converting a picture for the window takes {ms(conv)} of the {ms(per)} a picture "
                            f"takes here, and the camera sends one every {ms(every)}", converting=True)
            return None
        if hw and (not short or row < LOAD_HW_ROW):
            logging.debug(f"[VideoWorker] {dec.name}: a picture took {ms(decoding)} for a stretch (the camera sends "
                          f"one every {ms(every)})" + ("" if short else ", and every picture came"))
            return None
        if hw:
            if not self._hw_slow_told:
                self._hw_slow_told = True
                more = f", converting it for the window {ms(conv)} more," if named else ""
                logging.warning(f"[VideoWorker] ⚠️ the GPU's decoder does not keep up with this camera: a picture "
                                f"takes {ms(decoding)} through {dec.name}{more} and the camera sends one every "
                                f"{ms(every)}. It stays in use (--accel={self.accel} asked for it, and it "
                                f"delivers pictures). What helps: the camera's second (smaller) stream; a lower "
                                f"resolution or frame rate set in the camera; fewer cameras on this decoder; or "
                                f"this camera without --accel, decoded on the CPU")
            return None
        most = min(os.cpu_count() or 1, LOAD_THREADS_MAX)
        asked = any(k in self.dec_opts for k in ("threads", "thread_type"))
        took = (f"a picture takes {ms(per)} here" + (f" on {self._sw_threads} threads" if self._sw_threads else "")
                + (f" ({ms(decoding)} to decode it, {ms(conv)} to convert it for the window)" if named else "")
                + f" and the camera sends one every {ms(every)}")
        if not asked and most > 1 and self._sw_threads < most:
            # the first time by what is missing, with half as much again; then twice as many.  The threads
            # take the decoding off this thread, not the conversion: they share what the conversion leaves
            left = max(every - conv, every * (1.0 - LOAD_BUSY))
            self._sw_threads = n = min(most, max(2, self._sw_threads * 2, math.ceil(1.5 * decoding / left)))
            return f"{took}: the decoder gets {n} threads from now on (the picture is {n - 1} frame(s) later for it)"
        self._tell_slow(took, converting=named)
        return None

    def _tell_slow(self, took, converting=False):
        """The one line about a camera this CPU cannot keep up with, and what would need less of it:
        what spares the conversion first when that is where the time goes."""
        if self._slow_told:
            return
        self._slow_told = True
        window = not self.headless
        spare = []
        if window and not self.opengl:
            spare.append("--opengl (the GPU converts the pictures)" if converting
                         else "--opengl (no conversion of each picture)")
        if window and converting:
            spare.append("-f N (fewer pictures shown a second)")
        less = ["the camera's second (smaller) stream", "a lower resolution or frame rate set in the camera"]
        if converting:
            less = spare + less
        else:
            less += ["a GPU decoder (--accel)"] + spare + ['--ffmpeg "-skip_frame nokey" (key pictures only)']
        logging.warning(f"[VideoWorker] ⚠️ this CPU does not keep up with the camera: {took}. "
                        f"What needs less: {'; '.join(less)}")

    def _busy_machine(self, load):
        """The stretch `load` has just judged was behind with every core of the machine busy, and so
        were the ones before it for LOAD_LATE_S: one line, once.  Nothing is changed: no decoder and
        no thread more would find a core."""
        self._late = load.late
        if self._late_told or load.late * LOAD_JUDGE_S < LOAD_LATE_S:
            return
        if time.monotonic() - self._born < LOAD_SETTLED_S:
            return
        self._late_told = True
        less = ["fewer cameras at once on this machine", "the camera's second (smaller) stream",
                "a lower resolution or frame rate set in the camera"]
        if not self.headless:
            if not self.opengl:
                less.insert(1, "--opengl (the GPU converts the pictures)")
            less.append("-f N (fewer pictures shown a second)")
        logging.warning(f"[VideoWorker] ⚠️ this machine's CPU is busy: for {load.late * LOAD_JUDGE_S:.0f} s the "
                        f"pictures of this camera have come late with {load.free:.1f} of its "
                        f"{os.cpu_count() or 1} cores idle. No other decoder and no thread more would change "
                        f"that. What needs less: {'; '.join(less)}")

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
        # -e copy: the stream as its packets are handed on; made here when one asks already, else when one does
        cam = CamStream(video_stream) if self.copy_asked["recorder"] or self.copy_asked["streamer"] else None
        decoding = self._pictures_wanted(cam)       # the decoder is open and fed
        passed = False              # video packets of this connection went by undecoded
        undecoded_said = False
        if decoding:
            dec.open()
        audio_context = audio_stream.codec_context if audio_stream else None
        self.audio_codec = _audio_desc(audio_stream)
        if audio_stream is not None:
            logging.info(f"[VideoWorker] audio: {self.audio_codec}")

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
        t_afirst = t_apkt = None    # first and last audio packet on this connection
        v_ts = None                 # the last video packet's timestamp, in seconds
        t_back = back_s = None      # when the video timestamps went back (TS_BACK_S), and by how much
        v_pkts, v_wait = 0, 0.0     # since the last picture: video packets, and the longest wait for one
        handed = {"recorder": False, "streamer": False}     # -e copy: packets go there, from a key picture on
        load = _Load(fps_cap, cpu_idle, self._late)
        try:
            for packet in container.demux(*streams):
                if not self.run_flag: break
                if packet.dts is None: continue
                now = time.monotonic()

                if packet.stream.type == 'video':
                    v_pkts, v_wait = v_pkts + 1, max(v_wait, now - t_vpkt)
                    t_vpkt = now
                    if packet.pts is not None and packet.time_base:
                        ts = float(packet.pts * packet.time_base)
                        if v_ts is not None and v_ts - ts > TS_BACK_S:
                            t_back, back_s = now, v_ts - ts
                            logging.info(f"[VideoWorker] the video timestamps went back {back_s:.0f} s "
                                         f"while the stream goes on (the camera's clock started again)")
                        v_ts = ts
                    if packet.is_keyframe:
                        if t_key is not None:
                            self._gops.append(now - t_key)
                        t_key = now

                    if self.copy_asked["recorder"] or self.copy_asked["streamer"]:
                        # before it is decoded: what the decoder makes of a packet is not the file's business
                        if cam is None:
                            cam = CamStream(video_stream)
                        self._hand_on(packet, cam, handed)

                    if self._pictures_wanted(cam):
                        if not decoding:
                            decoding = True
                            dec.open()
                            dec.wait_kf = passed            # what went by is missing: on with a key picture
                            if passed:
                                logging.info("[VideoWorker] the camera's pictures are decoded again, from its next "
                                             "key picture on: something takes them now")
                        frames = dec.decode(packet)
                    else:
                        # Nothing shows this camera's pictures and nothing encodes them (-e copy writes its
                        # packets): they are not decoded, and no decoder is held, a GPU's or a board's least
                        # of all.  A packet stands for a picture where the connection is watched.
                        if decoding:
                            dec.release()
                            dec, decoding = _VideoDecode(self, video_stream), False
                            self.decoder, self.decoder_hw = "", False
                        if not undecoded_said:
                            undecoded_said = True
                            self._fail_streak, self._last_fail = 0, None
                            logging.info(f"[VideoWorker] {'From here on' if passed or first_frame_logged else 'First packet'} "
                                         f"({cam.codec} {cam.width}x{cam.height}) not decoded: -e copy writes the "
                                         f"camera's packets, and no window shows its pictures")
                        passed, frames = True, ()
                        v_pkts, v_wait = 0, 0.0
                        t_vframe = self.last_frame_time = now
                        self._session_frames += 1
                        if (cam.width, cam.height) != self._size:
                            self._size = (cam.width, cam.height)
                            self.sz_sig.emit(cam.width, cam.height)
                    converting = 0.0
                    for frame in frames:
                        w, h = frame.width, frame.height
                        if w <= 0 or h <= 0: continue
                        if cam is not None and not cam.width:
                            cam.width, cam.height = w, h    # the connection did not say: the first picture does
                        pix_fmt = frame.format.name
                        if t_vframe is not None and now - t_vframe > GAP_SAID_S:
                            # a hole in the picture (and in a recording) that nothing else in the log explains
                            logging.info(f"[VideoWorker] the picture was gone for {now - t_vframe:.0f} s on this connection "
                                         f"and is back ({v_pkts} video packets came in that time, the longest wait "
                                         f"for one was {v_wait:.0f} s)")
                        v_pkts, v_wait = 0, 0.0
                        t_vframe = self.last_frame_time = now
                        self._session_frames += 1

                        if not first_frame_logged:
                            first_frame_logged = True
                            self._fail_streak = 0
                            self._last_fail = None
                            how = "HW" if dec.is_hw else f"SW, {dec.threads} threads" if dec.threads else "SW"
                            logging.info(f"[VideoWorker] First frame ({dec.name}, {how}) pix_fmt={pix_fmt} {w}x{h}")

                        if (w, h) != self._size:
                            self._size = (w, h)
                            self.sz_sig.emit(w, h)
                            logging.verbose(f"[VideoWorker] emit video width:{w}, height:{h}")

                        direct = self.opengl and pix_fmt in GL_DIRECT_FORMATS
                        if dec.copies:
                            frame = self._read_once(frame, direct)
                        self._dispatch_av(FRAME, frame)

                        if self.headless: continue

                        sk.update(fr.get_fps())
                        if sk.is_skip() and not frame.key_frame: continue

                        if direct:
                            self.frame_q.append(DisplayFrame.from_av_frame(frame))
                        else:
                            t_conv = time.monotonic()
                            self.frame_q.append(DisplayFrame.from_rgba_array(rgba_array(frame)))
                            converting += time.monotonic() - t_conv
                        self.img_sig.emit()

                    if decoding:
                        more = self._slow(load.add(now, time.monotonic() - now, len(frames), converting),
                                          fps_cap, dec, load.conv, load.row, load.short)
                        if more:
                            raise _Reconnect(more)
                        if load.late != self._late:
                            self._busy_machine(load)

                elif packet.stream.type == 'audio':
                    if t_apkt is None:
                        t_afirst = now
                    elif now - t_apkt > GAP_SAID_S:
                        logging.info(f"[VideoWorker] the sound was gone for {now - t_apkt:.0f} s on this connection and is back")
                    t_apkt = now
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
                # the other way round: some cameras stop sending the audio of a connection after minutes or
                # hours (the RTSP session then carries video alone) and send it again to a new one. A recording
                # went on without sound for the rest of the night, with nothing in the log.
                wait = self._a_stall_wait
                stopped_with_clock = t_back is not None and t_apkt is not None and t_apkt <= t_back + 1.0
                if stopped_with_clock and wait:
                    wait = min(wait, AUDIO_AFTER_BACK_S)
                if t_apkt is not None and wait and now - t_apkt > wait and self._audio_wanted():
                    # audio that lasted: the next silence is waited for as long as this one; a camera that
                    # drops it soon after every connect is asked less and less often
                    lasted = t_apkt - t_afirst > 10 * self._a_stall_wait
                    self._a_stall_wait = _audio_stall_s() if lasted else min(AUDIO_STALL_MAX_S, self._a_stall_wait * 2)
                    after = f" since the video timestamps went back {back_s:.0f} s" if stopped_with_clock else ""
                    raise _Reconnect(f"no audio for {now - t_apkt:.0f} s{after} while video keeps flowing")
            if self.run_flag:                # the demuxer hit the end of the stream (EOF)
                logging.info(f"[VideoWorker] stream ended after {time.monotonic() - t_start:.0f} s; reconnecting [{u_disp}]")
        except _Reconnect as r:
            self.stat_sig.emit(f"Reconnecting:\n{r}")
            self._log_fail(re.sub(r"\d+", "#", str(r)), f"[VideoWorker] {r}; reconnecting. [{u_disp}]")
        finally:
            dec.release()
            more = self._slow(load.end(time.monotonic()), fps_cap, dec, load.conv, load.row, load.short)
            if more:
                logging.warning(f"[VideoWorker] {more}")
            self._busy_machine(load)

    # ------------------------------------------------------------------ recorder / streamer hand-off
    def _dispatch_av(self, typ, frame):
        """Hand decoded frames to the recorder/streamer queues without copying.
        PyAV frames are independent refcounted buffers, so the first consumer gets the original;
        only when both are active is one copy made for the second (each writer sets pts and
        picture type on the frame it gets)."""
        rec, strm = self.rec_on, self.stream_on
        if typ == FRAME:                    # not for the one that writes the camera's packets (-e copy)
            rec, strm = self._frames_to("recorder"), self._frames_to("streamer")
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

    def _hand_on(self, packet, cam, handed):
        """-e copy: this packet of the camera's video, as it came, for the recorder and the streamer that
        write it without encoding.  Each gets a connection's packets from a key picture on (what comes
        before one cannot be read), and none before the size of the picture is known (CamStream)."""
        item = None
        for who, q, on in (("recorder", self.rec_q, self.rec_on), ("streamer", self.stream_q, self.stream_on)):
            if not (on and cam.width and self._wants_packets(who)):
                handed[who] = False
                continue
            if not handed[who]:
                if not packet.is_keyframe:
                    continue
                handed[who] = True
            if item is None:
                item = CamPacket.of(packet, cam)
            self._put(q, (PACKET, item), who)

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

    def _read_once(self, frame, direct):
        """A picture that lies in device memory (DEVICE_MEMORY_DECODERS), as its readers should get it:
        itself when the OpenGL window's upload is the only one (`direct`), else a copy."""
        if direct and not (self._frames_to("recorder") or self._frames_to("streamer")):
            return frame
        return self._clone_frame(frame) or frame

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
        for attr in ("colorspace", "color_range", "color_primaries", "color_trc", "chroma_location", "key_frame"):
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

def _audio_desc(stream):
    """'pcm_alaw 8000 Hz mono' for a camera's audio stream, None without one."""
    if stream is None:
        return None
    try:
        cc = stream.codec_context
        ch = cc.layout.nb_channels if cc.layout else 0
        chs = {0: "", 1: " mono", 2: " stereo"}.get(ch, f" {ch} ch")
        return f"{cc.name} {stream.rate or cc.sample_rate or '?'} Hz{chs}"
    except Exception:
        return getattr(getattr(stream, "codec_context", None), "name", None) or "audio"


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
