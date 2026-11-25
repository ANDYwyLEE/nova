"""AVWriter: the PyAV writer behind recording and streaming: encoder choice and probing (EncoderManager),
timestamps, the -vf graph, audio (the camera's or silence), the output container, failures at open."""
import re
import os
import sys
import stat
import time
import threading
import gc
import tempfile
import logging
from fractions import Fraction

import av
import logsync  # installs logging.verbose / logging.trace
from campacket import wants_copy
from ffparser import for_codec
from util import (WINDOWS, MAC, IS_PI, MSYS, CONV_THREADS, get_os_type, vaapi_node, short_err as _short_err,
                  hw_err as _hw_err, nvidia_driver_missing)

try: from filelock import FileLock, Timeout
except ImportError:
    print("Error: filelock is required. Run 'pip install filelock'")
    sys.exit(1)

def _get_best_pix_fmt(encoder_name: str) -> str:
    e = encoder_name.lower()
    if any(x in e for x in ["nvenc", "qsv", "rkmpp", "videotoolbox", "amf", "vaapi", "_mf"]):
        return "nv12"
    return "yuv420p"

def _family(name: str) -> str:
    n = (name or "").lower()
    if "265" in n or "hevc" in n: return "hevc"
    if "av1" in n: return "av1"
    if "vp9" in n: return "vp9"
    return "h264"

def _mf_opts(encoder_name, opts):
    """opts with hw_encoding=1 for a Media Foundation encoder, unless the user chose. FFmpeg's *_mf encoders
    take Microsoft's software MFT by default: its H.264 one ignored a 4 Mbit/s target on a test pattern
    (140 Mbit/s) and there is no HEVC one, while with hw_encoding=1 the GPU vendor's MFT encodes (AMD's on a
    Ryzen 5900HX: 5 ms per 1080p frame, the bitrate kept). In the list of hardware encoders, mf means that."""
    if encoder_name.endswith("_mf") and "hw_encoding" not in opts:
        return {**opts, "hw_encoding": "1"}
    return opts


# Encoders that have to be told a bit rate, and a stream made by add_stream has none: Media Foundation
# refuses the output type ("could not set output type"), and a V4L2 encoder takes FFmpeg's 200 kbit/s (a
# 1080p camera in 120 kbit/s on a Raspberry Pi 4).
BITRATE_NEEDED = ("_mf", "_v4l2m2m")
# Encoders whose packets lie in buffers of the device, of which there are a handful (FFmpeg's V4L2
# encoders: four by default).  A packet the muxer keeps while it waits for the other stream holds one;
# with all of them out the encoder stands ("All capture buffers returned to userspace").  Their packets
# are copied as they come.
DEVICE_PACKET_ENCODERS = ("_v4l2m2m",)


def _default_bitrate(width, height, fps):
    """The bit rate for an encoder of BITRATE_NEEDED when the user named none: 0.1 bit per pixel and frame
    (1080p25: 5.2 Mbit/s, 1080p10: 2.1), as much as h264_amf writes for the same cameras by default."""
    return max(500_000, int(width * height * max(1, fps) * 0.1))


def _only_param_sets(data, hevc):
    """Is this Annex B packet parameter sets (H.264 SPS/PPS, HEVC VPS/SPS/PPS) and no picture?"""
    found, i = False, 0
    while True:
        j = data.find(b"\x00\x00\x01", i)
        if j < 0 or j + 3 >= len(data):
            return found
        h = data[j + 3]
        t = (h >> 1) & 0x3f if hevc else h & 0x1f
        if (t < 32) if hevc else (1 <= t <= 5):
            return False                            # a slice
        if t in ((32, 33, 34) if hevc else (7, 8)):
            found = True
        i = j + 3


def _own_packet(packet):
    """A copy of an encoder's packet in memory of its own (DEVICE_PACKET_ENCODERS)."""
    p = av.Packet(bytes(packet))
    p.pts, p.dts = packet.pts, packet.dts
    if packet.duration:
        p.duration = packet.duration
    if packet.time_base is not None:
        p.time_base = packet.time_base
    p.is_keyframe = packet.is_keyframe
    if packet.stream is not None:
        p.stream = packet.stream
    return p


LIVE_FORMATS = ("flv", "mpegts", "hls", "rtsp", "rtp_mpegts")
# The coder of FFmpeg's AAC encoder, unless --ffmpeg names one (-aac_coder twoloop is FFmpeg's default).
# Twoloop took 6 % of a Raspberry Pi 4's core and 11 % of a Pi 3's for one camera's 8 kHz sound, the fast
# coder a third of that; of everything tried (two cameras' sound at four loudnesses, noise, alarm tones, a
# hum, 8 to 48 kHz) none cost it more.  It was as close to the cameras' sound (38.6 and 42.1 dB above the
# difference, twoloop 38.7 and 43.0) and writes more bits for it: 43 kbit/s where twoloop writes 28.
AAC_CODER = "fast"
LIVE_MAX_LEAD = 1.0 # s a live stream may run ahead of the clock (YouTube: ~1 s of video per second)
AUDIO_WAIT = 2.0    # s of video a writer waits for the camera's first audio frame before deciding the track

# Codec families each container can carry (and that typical ingest servers accept).
# YouTube/Twitch RTMP: H.264 only.  YouTube HLS: H.264 / HEVC.
FORMAT_FAMILIES = {
    "flv": ("h264",),
    "mpegts": ("h264", "hevc"),
    "hls": ("h264", "hevc"),
    "mp4": ("h264", "hevc", "av1"),
    "mov": ("h264", "hevc", "av1"),
    "matroska": ("h264", "hevc", "av1", "vp9"),
}
# --ffmpeg keys that AVWriter interprets itself; they are not codec options
NON_CODEC_OPTS = ('s', 'r', 'g', 'gop_size')

SOFTWARE_ENCODERS = {"h264": ["libx264"], "hevc": ["libx265"], "av1": ["libsvtav1", "libaom-av1"], "vp9": ["libvpx-vp9"]}

# the encoders that belong to a decoding API; the others (vdpau, d3d11va, dxva2, drm ...) only decode
_ACCEL_ENCODER = {"cuda": "nvenc", "cuvid": "nvenc", "nvdec": "nvenc", "qsv": "qsv", "vaapi": "vaapi",
                  "amf": "amf", "videotoolbox": "videotoolbox", "rkmpp": "rkmpp", "v4l2m2m": "v4l2m2m"}


def _hw_suffixes(accel):
    a = (accel or "").lower()
    if IS_PI:
        base = ["rkmpp", "v4l2m2m", "nvmpi", "omx"]
    elif WINDOWS or MSYS:
        base = ["nvenc", "qsv", "amf", "mf"]
    elif MAC:
        base = ["videotoolbox"]
    else:
        base = ["nvenc", "qsv", "vaapi", "amf"]
    # try the encoder that matches the decoder's vendor first
    pref = _ACCEL_ENCODER.get(a)
    if pref in base:
        base.remove(pref); base.insert(0, pref)
    return base


def _frame_colour(frame):
    """(range, matrix, primaries, transfer) of a decoded picture as FFmpeg's numbers, None where the picture
    does not say.  Range: 1 limited (16-235), 2 full (0-255, what the old yuvj420p format meant)."""
    def val(name, unsaid):
        try:
            v = int(getattr(frame, name))
        except Exception:
            return None
        return None if v in unsaid else v
    rng = val("color_range", (0,))
    if rng is None and frame.format.name.startswith("yuvj"):
        rng = 2
    return rng, val("colorspace", (0, 2)), val("color_primaries", (0, 2)), val("color_trc", (0, 2))


def _make_hwaccel(device_type, device):
    from av.codec.hwaccel import HWAccel
    return HWAccel(device_type=device_type, device=device)


def _encoder_hwaccel(encoder_name, gpu_idx=None):
    """The hardware device an encoder has to be handed, or None.  The VAAPI encoders take pictures that live
    on the GPU: given the device, PyAV (19) puts Nova's system-memory pictures there itself.  The device is
    the DRM render node that --gpu counts, as for decoding; with no --gpu (None) the first node that can
    have VAAPI (util.vaapi_node)."""
    if encoder_name.lower().endswith("_vaapi"):
        return _make_hwaccel("vaapi", vaapi_node(gpu_idx))
    return None


def parse_bitrate(v):
    """'48k', '0.5M', '48000' -> bit/s; None when v is not a number."""
    s = str(v).strip().lower()
    mult = {"k": 1000, "m": 1000000}.get(s[-1:], 1)
    try:
        return int(float(s[:-1] if mult != 1 else s) * mult)
    except ValueError:
        return None


def _encoder_lock_path():
    """Per-user path of the probe lock. A fixed name in the shared temp dir can be created first by
    another local user, and every probe then waited the lock's 15 s and ended at libx264.
    None: no private place for it (probe without the lock)."""
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if rt and os.path.isdir(rt) and os.access(rt, os.W_OK | os.X_OK):      # not another user's, kept by su/sudo -u
        return os.path.join(rt, "nova_encoder_init.lock")      # 0700, the user's own (systemd)
    base = tempfile.gettempdir()
    if not hasattr(os, "getuid"):
        return os.path.join(base, "nova_encoder_init.lock")    # Windows: the temp dir is per user
    uid = os.getuid()
    d = os.path.join(base, f"nova-{uid}")
    try:
        try: os.mkdir(d, 0o700)
        except FileExistsError: pass
        st = os.lstat(d)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid or st.st_mode & 0o077:
        return None                                            # not ours, or open to others
    return os.path.join(d, "encoder_init.lock")


FAILED_RETRY_S = 3600  # an encoder that failed at a real open is skipped this long, then probed again

class EncoderManager:
    """Picks a working encoder once per (accel, user encoder, format) and caches it.
    A cross-process file lock keeps 15 simultaneously starting cameras from probing the GPU at once."""
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(EncoderManager, cls).__new__(cls)
                cls._instance._cache = {}        # key -> (encoder, monotonic time it expires or None)
                cls._instance._failed = {}       # encoder -> monotonic time of its last failed real open
                cls._instance._why = {}          # encoder -> why it did not open the last time it was tried
                cls._instance._warned = set()
                cls._instance._lock_broken = False
                cls._instance.gpu_idx = None     # the camera's --gpu when one was given: the device of encoders that take one
            return cls._instance

    def get_encoder(self, accel, user_encoder, source_codec, enc_opts, allowed=None):
        allowed = tuple(allowed) if allowed else ("h264", "hevc", "av1", "vp9")
        key = (accel, user_encoder, _family(source_codec), allowed, tuple(sorted(enc_opts.items())))
        hit = self._valid(key)
        if hit:
            logging.debug(f"[EncoderManager] ⚡ cached encoder: {hit}")
            return hit
        with self._lock:
            hit = self._valid(key)
            if hit: return hit
            try:
                enc = self._probe_serialised(accel, user_encoder, source_codec, enc_opts, allowed)
            except Exception as e:
                logging.error(f"[EncoderManager] encoder probe failed: {e}")
                return "libx264"
            # a result chosen while better candidates were being skipped expires with their penalty
            skipped = self._skipped()
            until = [skipped[n] for n in self._candidates(accel, user_encoder, source_codec, allowed) if n in skipped]
            self._cache[key] = (enc, min(until) if until else None)
            return enc

    def _valid(self, key):
        hit = self._cache.get(key)
        if hit and (hit[1] is None or time.monotonic() < hit[1]):
            return hit[0]
        return None

    def _skipped(self):
        """Encoders that failed at a real open within FAILED_RETRY_S -> when they may be tried again."""
        now = time.monotonic()
        return {n: t + FAILED_RETRY_S for n, t in self._failed.items() if now - t < FAILED_RETRY_S}

    def why(self, encoder_name):
        """Why this encoder did not open the last time it was tried here, or None."""
        return self._why.get(encoder_name)

    def reject(self, encoder_name, why=None):
        """encoder_name passed the probe (640x360) but did not open for a real stream: NVENC's session
        limit with many cameras, a GPU fault. The next get_encoder() probes past it for FAILED_RETRY_S.
        True the first time this encoder fails in this process (warn once)."""
        with self._lock:
            self._failed[encoder_name] = time.monotonic()
            if why:
                self._why[encoder_name] = why
            for k in [k for k, v in self._cache.items() if v[0] == encoder_name]:
                del self._cache[k]
            first = encoder_name not in self._warned
            self._warned.add(encoder_name)
            return first

    def _probe_serialised(self, *args):
        """One process probes at a time. A lock that cannot be had (held for 15 s, unusable path)
        only costs that serialisation, never the hardware encoder: probe without it from then on."""
        path = None if self._lock_broken else _encoder_lock_path()
        lock = None
        if path:
            lock = FileLock(path, timeout=15)
            try:
                lock.acquire()
            except (Timeout, OSError) as e:
                lock = None
                self._lock_broken = True
                logging.warning(f"[EncoderManager] ⚠️ probe lock {path} unusable ({e or 'busy for 15 s'}); probing without it")
        try:
            with logsync.av_quiet():                   # candidates are expected to fail
                return self._perform_hw_test(*args)
        finally:
            if lock is not None:
                lock.release()

    def _candidates(self, accel, user_encoder, source_codec, allowed):
        src_fam = _family(source_codec)
        families = [f for f in dict.fromkeys([src_fam, "h264", "hevc", "av1", "vp9"]) if f in allowed]
        out = []
        if user_encoder and _family(user_encoder) in allowed:
            out.append(user_encoder)            # (one the target cannot carry: AVWriter warns about it)
        if accel:
            for fam in families:
                for suf in _hw_suffixes(accel):
                    out.append(f"{fam}_{suf}")
        # software: libx264 first (fast enough for live use), then the others
        if "h264" in allowed: out.append("libx264")
        for fam in families:
            out += SOFTWARE_ENCODERS.get(fam, [])
        if not out: out.append("libx264")
        return list(dict.fromkeys(out))

    def _perform_hw_test(self, accel, user_encoder, source_codec, enc_opts, allowed):
        logging.debug(f"[EncoderManager] OS:{get_os_type()} accel:{accel} source:{source_codec} allowed:{allowed}")
        test_opts = {k: v for k, v in enc_opts.items() if k not in NON_CODEC_OPTS}
        skipped = self._skipped()
        asked = None                    # why the encoder the user asked for (-e) did not open
        hardware = tuple("_" + suf for suf in _hw_suffixes(accel)) if accel else ()
        first_hw = None                 # the first hardware encoder tried for --accel, and why it did not open
        for encoder_name in self._candidates(accel, user_encoder, source_codec, allowed):
            if encoder_name in skipped:
                logging.debug(f"[EncoderManager] skipping {encoder_name}: failed at a real open "
                              f"{int(FAILED_RETRY_S - (skipped[encoder_name] - time.monotonic()))} s ago")
                continue
            err = self._try_open(encoder_name, test_opts, self.gpu_idx)
            if err is None:
                self._why.pop(encoder_name, None)
                logging.verbose(f"[EncoderManager] 🚀 using {encoder_name}")
                if asked is not None:
                    # a silent libx264 in place of the hardware encoder asked for costs ten times the CPU
                    logging.warning(f"[EncoderManager] ⚠️ -e {user_encoder} does not open here ({asked}); using {encoder_name}")
                elif first_hw and not encoder_name.endswith(hardware) and encoder_name != user_encoder:
                    # the same for --accel: the GPU decodes and the CPU encodes, and only "[AVWriter] libx264" told
                    if _ACCEL_ENCODER.get(accel.lower()):
                        why = f"no hardware encoder opens here for --accel {accel} ({first_hw[0]}: {first_hw[1]})"
                    else:
                        why = f"--accel {accel} only decodes, and no other hardware encoder opens on this machine"
                    logging.warning(f"[EncoderManager] ⚠️ {why}; encoding with {encoder_name} on the CPU")
                return encoder_name
            logging.debug(f"[EncoderManager] ❌ {encoder_name}: {err}")
            self._why[encoder_name] = " ".join(str(err).split())[:160]
            # without the user's options: is it the encoder that does not open, or what it was given?
            bare = self._try_open(encoder_name, {}, self.gpu_idx) if test_opts else err
            if first_hw is None and encoder_name.endswith(hardware) and encoder_name != user_encoder:
                first_hw = (encoder_name, " ".join(str(bare or err).split())[:160])
            if encoder_name == user_encoder and asked is None:
                asked = " ".join(str(err).split())[:140]
            if test_opts and bare is None:
                # the encoder itself works: the user's options are what it rejects
                logging.warning(f"[EncoderManager] ⚠️ {encoder_name} rejects your --ffmpeg options {test_opts}: {err}. "
                                f"Trying the next encoder (preset/tune names differ per encoder: nvenc p1-p7, x264 ultrafast..veryslow).")
        logging.warning("[EncoderManager] ⚠️ no candidate opened; falling back to libx264")
        return "libx264"

    @staticmethod
    def _try_open(encoder_name, opts, gpu_idx=None):
        """Returns None when the encoder opens with these options, else the error text."""
        ctx = None
        try:
            try:
                codec = av.Codec(encoder_name, "w")
            except Exception:
                return "this FFmpeg build has no such encoder"
            hw = _encoder_hwaccel(encoder_name, gpu_idx)
            ctx = av.CodecContext.create(codec, hwaccel=hw) if hw is not None else av.CodecContext.create(codec)
            ctx.width = 640
            ctx.height = 360
            ctx.pix_fmt = opts.get('pix_fmt') or _get_best_pix_fmt(encoder_name)
            ctx.time_base = Fraction(1, 30)
            ctx.framerate = Fraction(30, 1)
            ctx.options.update({k: v for k, v in _mf_opts(encoder_name, opts).items() if k != 'pix_fmt'})
            ctx.open()
            return None
        except Exception as e:
            # an NVIDIA card without its kernel driver: FFmpeg's words for that are "Unknown error occurred"
            return ("nvenc" in encoder_name and nvidia_driver_missing()) or _hw_err(e)
        finally:
            if ctx is not None:
                try:
                    if hasattr(ctx, 'close'): ctx.close()
                except Exception: pass
                del ctx


NET_SCHEMES = ('rtmp', 'srt', 'http', 'tcp', 'udp', 'rtp')
LIVE_OPEN_TIMEOUT = 30.0 # s; YouTube took 9 s to accept a publish

NET_TIMEOUT_US = 10_000_000
_bad_timeout_warned = False

def _net_timeout_us():
    """NOVA_TIMEOUT in microseconds. A value that is not a positive number used to raise here, and
    the stream never started; it now falls back to the default with one warning."""
    global _bad_timeout_warned
    v = os.getenv("NOVA_TIMEOUT")
    if v is None or not v.strip():
        return NET_TIMEOUT_US
    try:
        us = int(float(v))
        if us > 0: return us
    except (ValueError, OverflowError):
        pass
    if not _bad_timeout_warned:
        _bad_timeout_warned = True
        logging.warning(f"[AVWriter] NOVA_TIMEOUT={v!r} is not a positive number of microseconds; "
                        f"using {NET_TIMEOUT_US} ({NET_TIMEOUT_US // 1_000_000} s)")
    return NET_TIMEOUT_US

def is_network_target(path):
    return bool(path) and path.lower().startswith(NET_SCHEMES)

def _net_timeouts(path):
    """(open, write) deadlines in seconds for av.open(timeout=), None for files.

    PyAV opens the output with avio_open2(..., NULL): protocol options such as rw_timeout,
    tcp_nodelay or SRT's latency never reach the protocol, so a stalled RTMP server used to block
    the writer (and `nova stop`) forever. PyAV's deadline is the only write timeout there is."""
    if not is_network_target(path): return None
    t = max(1.0, _net_timeout_us() / 1_000_000)
    return max(LIVE_OPEN_TIMEOUT, t), t

# Protocol options for RTMP targets.  PyAV hands the output options to avio_open2() since 19.0.2
# (PyAV PR #2437); an older PyAV opened the output with NULL options, so there these are harmless
# and their "not used" report is kept at DEBUG (logsync).
#   rtmp_flush_interval: FFmpeg reads the server's messages (acknowledgements every ~180 KB from
#   YouTube, pings) only once per this many FLV packets; at the default 10 a 25 fps + AAC stream
#   drains about 7 messages a second, barely YouTube's rate, and the rest piled up in the socket's
#   receive queue (18 B/s, measured; days later the server could no longer send and dropped the
#   connection).  With 1 every packet drains one message.
RTMP_PROTOCOL_OPTIONS = {"rtmp_flush_interval": "1"}

def _build_conn_options(fmt, mux_opts, path=""):
    """Muxer options, plus RTMP_PROTOCOL_OPTIONS for rtmp targets. Other protocol options (rw_timeout,
    tcp_nodelay, SRT's latency) are not among them (see _net_timeouts): SRT settings go in the URL
    query (srt://host:port?latency=200000&passphrase=...)."""
    options = {}
    if str(path).lower().startswith(('rtmp://', 'rtmps://')):
        options.update(RTMP_PROTOCOL_OPTIONS)
    if fmt == 'hls':
        # the HLS muxer opens every segment/playlist upload itself and passes these on
        options['method'] = 'POST'
        options['timeout'] = f"{_net_timeout_us()}us"
    options.update({k: v for k, v in mux_opts.items() if k != 'f'})
    return options

def _determine_output_format(path, user_fmt=None):
    if user_fmt: return user_fmt
    p = path.lower()
    if p.startswith(('rtmp://', 'rtmps://')): return 'flv'
    if p.startswith('srt://'): return 'mpegts'
    if p.startswith(('http://', 'https://')):
        if '.m3u8' in p: return 'hls'
        if '.ts' in p: return 'mpegts'
    if p.startswith('udp://') or p.startswith('rtp://'): return 'mpegts'
    if p.endswith('.mp4'): return 'mp4'
    if p.endswith('.mov'): return 'mov'
    if p.endswith('.mkv'): return 'matroska'
    if p.endswith('.ts'): return 'mpegts'
    if p.endswith('.flv'): return 'flv'
    return None


def _keep_steps() -> bool:
    """NOVA_TIMESTAMP_STEPS=keep: every step of the source's timestamps is written as it comes."""
    return os.getenv("NOVA_TIMESTAMP_STEPS", "").strip().lower() == "keep"


class TimestampWatch:
    """What a source's timestamps have shown. One per source (timestamp_watch), so that it outlives a
    segment and a connection: `on` (the source is doubted) once its timestamps have stepped back
    while the stream went on.  `live` False (a file that is read, a capture device): never doubted,
    there is no arrival of packets to hold against the timestamps."""
    SAY_AFTER = 3               # steps back after which the log names it (one could be a quick reconnect)

    def __init__(self, live=True):
        self.keep = _keep_steps()
        self.live = live and not self.keep
        self.on = False
        self.back = 0               # steps back while the stream went on
        self.taken = 0              # steps that were not written as they came

    def stepped_back(self):
        self.taken += 1
        self.back += 1
        if self.live:
            self.on = True
            if self.back == self.SAY_AFTER:
                logging.warning("[AVWriter] ⚠️ the source's timestamps step back and forth while its stream goes on "
                                "(a camera whose RTCP sender reports do not fit its timestamps): such a step is "
                                "a gap in the file only where the arrival of the packets shows one")


def timestamp_watch(worker):
    """The TimestampWatch of a worker's source, made on first use: its recorder and its streamer share it."""
    watch = getattr(worker, "_timestamp_watch", None)
    if watch is None:
        url = str(getattr(worker, "url", "") or "")
        watch = TimestampWatch(live="://" in url and not url.lower().startswith("file:"))
        try:
            worker._timestamp_watch = watch
        except Exception:
            pass
    return watch


class _Timeline:
    """Maps source timestamps (pts * time_base, seconds) onto the output clock.

    Within a continuous run the exact source timing is preserved (no drift, no jitter).
    On a discontinuity (reconnect, pts reset, >5 s jump, missing pts) the stream is re-anchored
    to the wall clock shared by all streams of this writer, so audio and video stay aligned and
    outages show up as gaps instead of speeding up the recording.

    `peer` is the writer's other stream: a re-anchor goes on from the furthest point either stream
    has reached, so that sound and picture of a new connection are written next to each other also
    where the file is ahead of the wall clock.

    Timestamps that step while the stream goes on.  Some cameras send RTCP sender reports that do
    not fit their own RTP timestamps; FFmpeg maps every stream onto the reports, so the timestamps
    of such a camera step by up to two seconds with every report (every 7 s), forward and back,
    each stream at its own moments, while its frames and samples arrive complete and on time.
    Taken as they come, every step forward was a gap in the file and every step back was dropped
    (a file cannot go back): a 30-minute segment got 2 to 22 s longer than 30 minutes, with as
    many seconds of holes in its sound, and sound and picture were up to 5 s apart inside it.
    So: a step back with packets still arriving is no new connection; the stream goes on by its
    own step (the frame interval, the length of the audio frame).  A source that has done this
    is doubted (`watch`, shared with the other stream, the next segments and connections): from
    then on a step that is not the stream's own is a gap only when the packets' arrival shows it."""
    MAX_JUMP = 5.0
    QUIET_S = 2.0       # a step back after this long without a packet is a new connection

    def __init__(self, origin, max_lead=None, watch=None):
        self.origin = origin
        self.max_lead = max_lead        # live output: never more than this many seconds ahead of the clock
        self.src0 = None
        self.out0 = 0.0
        self.last_src = None
        self.last_out = 0.0
        self.last_now = None            # when the last packet came
        self.step = 0.0                 # the stream's own step: the frame interval (an interval seen twice
        self._inc = None                # in a row), or the length of the last audio frame
        self.held = False               # the last frame was held back to the clock (see map): drop it
        self.catching = False
        self.peer = None                # the writer's other timeline (see the class comment)
        self.watch = watch or TimestampWatch()

    def _anchor(self, src_t, now):
        # to the wall clock, but never behind what was already written, here or in the other stream
        written = max(self.last_out, self.peer.last_out) if self.peer else self.last_out
        self.src0, self.out0 = src_t, max(now, written)

    def _own_step(self, inc, quiet, audio):
        """Is this step of the timestamps one to write as it is?  (Asked of a doubted source only.)"""
        dev = inc - self.step
        if abs(dev) <= (0.005 if audio else max(0.005, 0.2 * self.step)):
            return True                                 # the stream's own step (a picture's, give or take)
        if quiet >= self.QUIET_S:
            return True                                 # nothing came for that long: a gap
        if audio or dev < 0:
            return False                                # sound is one sample after the other
        # picture: frames the camera left out are whole frame intervals, and the next frame comes later
        whole = abs(inc / self.step - round(inc / self.step)) * self.step < 0.001
        return whole and quiet >= 0.5 * dev

    def map(self, src_t, dur=None):
        """The output time of a frame. `dur`: its length, where that says when the next one starts (audio)."""
        now = time.monotonic() - self.origin
        self.held = False
        if src_t is None:
            self.src0 = None
            out = max(now, self.last_out)
        else:
            src_t = float(src_t)
            if self.src0 is None:
                self._anchor(src_t, now)
            else:
                inc = src_t - self.last_src
                quiet = now - self.last_now if self.last_now is not None else 0.0
                if inc > self.MAX_JUMP or (inc < -0.001 and (quiet >= self.QUIET_S or self.watch.keep)):
                    self._anchor(src_t, now)
                elif inc < -0.001:
                    # back, while the stream goes on: not a new connection (see the class comment)
                    self.watch.stepped_back()
                    self.src0, self.out0 = src_t, self.last_out + self.step
                elif self.watch.on and self.step and not self._own_step(inc, quiet, dur is not None):
                    self.watch.taken += 1
                    self.src0, self.out0 = src_t, self.last_out + self.step
                elif dur is None:
                    if self._inc is not None and abs(inc - self._inc) < 0.001 and 0.0 < inc <= 1.0:
                        self.step = inc
                    self._inc = inc
            if dur is not None:
                self.step = dur
            self.last_src = src_t
            out = self.out0 + (src_t - self.src0)
            if self.max_lead is not None and (self.catching or out - now > self.max_lead):
                # the source runs ahead of the clock: seconds of picture delivered at once, or timestamps
                # that advance faster than time (jumps shorter than MAX_JUMP are kept as gaps).  Written
                # as they are, the stream would stay that far ahead for good, and YouTube warns "sending
                # data faster than realtime".  Hold every frame until the clock has caught up with what
                # was written, then go on from the clock; the caller drops the held frames.
                if now < self.last_out:
                    self.catching = self.held = True
                    self.last_now = now
                    return self.last_out
                self.catching = False
                self.src0, self.out0 = src_t, now
                out = now
        self.last_now = now
        self.last_out = out
        return out

def _src_time(frame):
    try:
        if frame.pts is not None and frame.time_base:
            return frame.pts * frame.time_base
    except Exception:
        pass
    return None

def _parse_filtergraph(vf):
    """'transpose=1,scale=640:-2' -> [('transpose','1'), ('scale','640:-2')] (commas inside [] or quotes kept)."""
    items, buf, depth, quote = [], "", 0, None
    for ch in vf:
        if quote:
            buf += ch
            if ch == quote: quote = None
            continue
        if ch in "'\"": quote = ch; buf += ch; continue
        if ch in "([": depth += 1
        elif ch in ")]": depth -= 1
        if ch == ',' and depth == 0:
            items.append(buf); buf = ""
        else:
            buf += ch
    if buf.strip(): items.append(buf)
    out = []
    for it in items:
        it = it.strip()
        if not it: continue
        name, _, args = it.partition('=')
        out.append((name.strip(), args.strip() or None))
    return out

def _parse_size(s):
    m = re.match(r'^\s*(\d+)\s*[xX:]\s*(\d+)\s*$', str(s or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


_WARNED = set()     # (user encoder, format) pairs already warned about


class OutputOpenError(Exception):
    """The output could not be started. PyAV retries the open on every later packet, so the
    caller must give up on this writer instead of writing on (a refused stream would otherwise
    reconnect once per packet)."""


class CopyRefused(Exception):
    """-e copy cannot be done for this camera and this output, and why: the caller encodes instead."""


class AVWriter:
    """Encodes decoded frames into a file or a live stream.

    live=True (RTMP/SRT/HLS targets) additionally synthesises a silent AAC track when the
    camera has no audio, because YouTube and most ingest servers reject video-only streams.

    first (a CamPacket: -e copy): no video encoder.  The camera's packets are written as they come
    (write_packet), beginning with this one, a key picture; the sound is encoded as ever."""
    V_TB = Fraction(1, 90000)
    copy, cam = False, None         # (a writer that encodes)

    def __init__(
            self, accel,
            path, width, height, fps,
            encoder=None,
            source_codec="h264",
            open_opts=None,
            enc_v_opts=None,
            enc_a_opts=None,
            mux_opts=None,
            sample_rate=0, channels=0, audio_enable=True,
            gpu_idx=0, gpu_given=True,
            v_tag=None, a_encoder=None,
            live=False,
            vf=None, metadata=None,
            audio_wait=None,
            timestamps=None,
            first=None,
        ):
        self.cam = first.cam if first is not None else None     # -e copy: the stream whose packets are written
        self.copy = self.cam is not None
        self._need_key = True           # (copy) the next packet written has to be a key picture
        self._last = None               # (copy) the stream and the count of the last packet that came
        self._left_out = 0              # (copy) packets not written because what they refer to is missing
        self._sets = b""                # (copy) the parameter sets the file's pictures are read with
        self.path = path
        self.src_width, self.src_height = width, height
        self.width, self.height = width, height
        self.fps = max(1, int(round(fps or 30)))
        self.encoder = encoder
        self.source_codec = source_codec
        self.enc_v_opts = dict(enc_v_opts or {})
        self.enc_a_opts = dict(enc_a_opts or {})
        self.mux_opts = dict(mux_opts or {})
        self.sample_rate = int(sample_rate or 0)
        self.channels = int(channels or 0)
        self.gpu_idx = gpu_idx
        self._device_gpu = gpu_idx if gpu_given else None     # None: no --gpu, the VAAPI node is looked for
        self.live = live
        self.run_flag = True
        self.first_packet_sent = False
        self.first_packet_t = None      # time.monotonic() of the first muxed packet
        self.open_failed = None         # why the first mux (connect + header) failed
        self.watch = timestamps or TimestampWatch()     # what the source's timestamps have shown (_Timeline)
        self._taken0 = self.watch.taken
        self.v_frames = 0
        self.a_frames = 0
        self.audio_track = False
        self.last_v_pts = -1
        self._colour = None             # (range, matrix, primaries, transfer) of the camera's pictures
        self._last_v_dts = None         # dts of the last video packet handed to the muxer
        self._dts_fixed = 0             # video packets whose dts had to be moved (see _monotonic)
        self.last_a_end = 0
        self.a_time_base = None
        self.silence = False
        self.metadata = dict(metadata or {})
        self.vf = (vf or "").strip() or None
        self.graph = None               # libavfilter graph for -vf, built on the first frame
        self._v_opts_checked = False
        self._a_opts_checked = False
        self._applied_v = dict(self.enc_v_opts)   # what reached the encoders (ffparser.for_codec)
        self._applied_a = dict(self.enc_a_opts)
        self._video_started = False
        self._held_audio = []
        # sample_rate 0: the decoder has not seen an audio frame yet (a writer opened at the first video
        # frame). The track is then decided by the first audio frame, or after audio_wait seconds of
        # video without one; until then the encoded video packets are held (the header needs all streams).
        self.audio_wait = AUDIO_WAIT if audio_wait is None else audio_wait
        self._audio_pending = False
        self._audio_deadline = None
        self._pending = []
        self._a_conv = None             # camera audio -> the encoder's format/layout/rate
        self._a_conv_key = None

        if self.copy:
            self._copy_unused()
        out_size = _parse_size(self.enc_v_opts.pop('s', None))
        if out_size: self.width, self.height = out_size
        if not self.copy:
            self.width -= self.width % 2
            self.height -= self.height % 2
        if self.width <= 0 or self.height <= 0:
            # an encoder would fail to open, and that failure would be blamed on the encoder
            raise ValueError(f"no video size yet ({width}x{height})")

        # -r N : output frame rate (frames are dropped, never duplicated)
        self.out_fps = None
        r = self.enc_v_opts.pop('r', None)
        if r:
            try:
                self.out_fps = float(Fraction(r))
                if self.out_fps > 0: self.fps = max(1, int(round(self.out_fps)))
            except (ValueError, ZeroDivisionError):
                logging.warning(f"[AVWriter] ignoring invalid -r {r}")
                self.out_fps = None
        self._next_frame_t = 0.0

        fmt = _determine_output_format(self.path, self.mux_opts.get('f'))
        self.fmt = fmt
        allowed = FORMAT_FAMILIES.get(fmt)

        # crash-safe MP4: fragments are complete on disk even if the process dies mid-segment
        if fmt in ("mp4", "mov") and 'movflags' not in self.mux_opts:
            self.mux_opts['movflags'] = '+frag_keyframe+empty_moov'

        self._container_opts = _build_conn_options(fmt, self.mux_opts, self.path)
        self.timeout = _net_timeouts(self.path)
        self._v_tag = v_tag
        self._enc_args = (accel, encoder, source_codec, dict(self.enc_v_opts), allowed)
        self._tried = set()             # video encoders that did not open for this writer
        if self.copy:
            self._copy_possible(first, allowed)     # CopyRefused, before there is a file
        self.container = self._open_container()

        try:
            if self.copy:
                self.v_encoder = "copy"
                self._setup_copy(first, v_tag)
            else:
                EncoderManager().gpu_idx = self._device_gpu
                self.v_encoder = EncoderManager().get_encoder(*self._enc_args)
                if encoder and allowed and _family(encoder) not in allowed and (encoder, fmt) not in _WARNED:
                    # e.g. -e hevc_nvenc in [defaults]: an RTMP stream in HEVC shows "no data" on YouTube
                    _WARNED.add((encoder, fmt))
                    logging.warning(f"[AVWriter] ⚠️ -e {encoder}: {fmt} carries {', '.join(allowed)} only; "
                                    f"using {self.v_encoder}")
                self._setup_video(v_tag)
            self._setup_audio(audio_enable, a_encoder)
            if self.metadata:
                self.container.metadata.update(self.metadata)
        except Exception:
            try: self.container.close()
            except Exception: pass
            raise

        self.origin = time.monotonic()
        lead = LIVE_MAX_LEAD if live else None
        self.v_tl = _Timeline(self.origin, lead, self.watch)
        self.a_tl = _Timeline(self.origin, lead, self.watch)
        self.v_tl.peer, self.a_tl.peer = self.a_tl, self.v_tl
        self._held_v = 0                # video frames dropped to stay real time (live), for the log
        what = f"copy ({self.cam.codec} as the camera sends it)" if self.copy else self.v_encoder
        logging.info(f"[AVWriter] {what} {self.width}x{self.height}@{self.fps} -> {fmt} "
                     f"audio={'silence' if self.silence else (self.a_stream.codec_context.codec.name if self.a_stream else 'none')}"
                     f"{' (waiting for camera audio)' if self._audio_pending else ''}")

    # ------------------------------------------------------------------ setup
    def _open_container(self):
        logging.debug(f"[AVWriter] av.open({self.path}, format={self.fmt}, options={self._container_opts}, timeout={self.timeout})")
        return av.open(self.path, mode="w", format=self.fmt, options=self._container_opts, timeout=self.timeout)

    def _rebuild(self, v_encoder):
        """A new container around another video encoder. Only before anything was muxed (the video
        encoder opens on the first frame, and nothing is muxed before the first video packet):
        audio packets encoded so far are dropped, the audio stream is set up again."""
        try: self.container.close()
        except Exception as e: logging.debug(f"[AVWriter] container close: {e}")
        self.v_encoder = v_encoder
        self._held_audio.clear()
        self._pending.clear()
        self._video_started = False
        self._v_opts_checked = self._a_opts_checked = False
        self.a_frames = 0
        self._a_conv_key = None
        try:
            self.container = self._open_container()
            self._setup_video(self._v_tag)
            if self.a_stream is not None:
                self._add_audio_stream()
            if self.metadata:
                self.container.metadata.update(self.metadata)
        except Exception as e:
            self.open_failed = self.open_failed or f"could not set up {v_encoder}: {_short_err(e)}"
            raise OutputOpenError(f"cannot start: {self.open_failed}") from e

    # ---- -e copy ---------------------------------------------------------------------------
    def _copy_unused(self):
        """-e copy with an encoder's or a filter's options: they are named once, and not used."""
        unused = [f"-{k} {v}" for k, v in self.enc_v_opts.items()] + ([f"-vf {self.vf}"] if self.vf else [])
        self.enc_v_opts, self._applied_v, self.vf = {}, {}, None
        key = ("copy", tuple(unused))
        if unused and key not in _WARNED:
            _WARNED.add(key)
            logging.warning(f"[AVWriter] ⚠️ -e copy writes the camera's pictures as they come: {', '.join(unused)} "
                            f"{'is' if len(unused) == 1 else 'are'} not used (an encoder's options)")

    def _copy_possible(self, first, allowed):
        cam = self.cam
        if cam.template is None:
            raise CopyRefused(cam.why or f"the camera's {cam.codec} stream cannot be handed on as it is")
        family = cam.codec if cam.codec in ("h264", "hevc", "av1", "vp9") else None
        if allowed and family and family not in allowed:
            raise CopyRefused(f"{self.fmt} carries {' and '.join(allowed)} only, and the camera sends {cam.codec}")
        if cam.has_sets and not (first.sets() or cam.extradata):
            raise CopyRefused(f"the camera's {cam.codec} stream names no parameter sets, and a file of it "
                              f"could not be read")

    def _setup_copy(self, first, v_tag):
        """The file's video stream is the camera's own: made from the connection's template, and no codec
        is opened for it (PyAV opens none for a stream that came from a template)."""
        cam = self.cam
        try:
            self.v_stream = self.container.add_stream_from_template(cam.template, opaque=True)
        except Exception as e:
            raise CopyRefused(f"{self.fmt} does not take the camera's {cam.codec} stream ({_short_err(e)})") from e
        ctx = self.v_stream.codec_context
        # The parameter sets the file's pictures are read with (MP4 and FLV keep them in the header): the
        # ones the camera sends in front of its key pictures, where it does; else the ones it named when
        # the connection was made (RTSP: in the SDP).
        sets = first.sets()
        self._sets = sets or cam.header()
        if sets:
            ctx.extradata = sets
        if not ctx.width or not ctx.height:
            ctx.width, ctx.height = self.width, self.height
        self.v_time_base = self.V_TB
        try: self.v_stream.time_base = self.V_TB        # (PyAV brings each packet's times into the stream's)
        except Exception as e: logging.debug(f"[AVWriter] time_base not settable: {e}")
        tag = v_tag or ("hvc1" if cam.hevc and self.fmt in ("mp4", "mov", "hls") else None)
        if tag:
            try: ctx.codec_tag = tag
            except Exception as e: logging.debug(f"[AVWriter] codec_tag {tag} rejected: {e}")
        self._v_pix_fmt = None
        logging.debug(f"[AVWriter] video: the camera's {cam.codec}, header of {len(ctx.extradata or b'')} bytes "
                      f"({'from its key picture' if sets else 'as the connection named it'})")

    def takes(self, pkt):
        """Can this packet's stream go on in this file?  Not when the camera has changed what it sends:
        another codec or size (a new connection's), or other parameter sets in front of a key picture."""
        if not self.copy:
            return False
        cam, mine = pkt.cam, self.cam
        if cam is not mine:
            if (cam.codec, cam.width, cam.height, cam.annexb) != (mine.codec, mine.width, mine.height, mine.annexb):
                return False
            if not mine.annexb and cam.extradata != mine.extradata:
                return False
        if pkt.key and self._sets:
            sets = pkt.sets() or (cam.header() if cam is not mine else b"")
            if sets and sets != self._sets:
                return False
        self.cam = cam
        return True

    def write_packet(self, pkt):
        """-e copy: a packet of the camera's video, written as it is at the time the timeline gives it.
        Packets come in the order they are decoded in: the timeline is asked with the dts, and the pts
        keeps its distance from it (a camera that sends B-frames)."""
        if pkt is None or not self.run_flag: return
        if self.open_failed: self._refuse()
        last, self._last = self._last, (pkt.cam, pkt.n)
        if last is not None and (last[0] is not pkt.cam or pkt.n != last[1] + 1):
            self._need_key = True               # a new connection, or packets a full queue dropped
        if self._need_key and not pkt.key:
            self._left_out += 1
            return
        tb = pkt.time_base
        dts = pkt.dts if pkt.dts is not None else pkt.pts
        out_t = self.v_tl.map(dts * tb if dts is not None and tb else None)
        if self.v_tl.held:
            self._held_v += 1                   # part of a burst beyond LIVE_MAX_LEAD (see write_video)
            self._need_key = True               # what follows refers to it: on with the next key picture
            return
        self._need_key = False
        if self._held_v or self._left_out:
            logging.info(f"[AVWriter] {self._held_v + self._left_out} packet(s) of the camera's video left out "
                         f"({'the source ran ahead of real time' if self._held_v else 'some were missing'}): "
                         f"on with a key picture")
            self._held_v = self._left_out = 0
        out_dts = int(round(out_t / self.v_time_base))
        if out_dts <= self.last_v_pts:
            out_dts = self.last_v_pts + 1
        self.last_v_pts = out_dts
        lead = 0
        if pkt.pts is not None and dts is not None and tb:
            lead = max(0, int(round((pkt.pts - dts) * tb / self.v_time_base)))
        p = av.Packet(pkt.data)
        p.stream = self.v_stream
        p.time_base = self.v_time_base
        p.dts, p.pts = out_dts, out_dts + lead
        if pkt.duration and tb:
            p.duration = max(0, int(round(pkt.duration * tb / self.v_time_base)))
        p.is_keyframe = pkt.key
        self._mux([p])
        self.v_frames += 1
        self._after_video(out_t)

    def _setup_video(self, v_tag):
        hw = _encoder_hwaccel(self.v_encoder, self._device_gpu)
        if hw is not None:
            self.v_stream = self.container.add_stream(self.v_encoder, rate=self.fps, hwaccel=hw)
        else:
            self.v_stream = self.container.add_stream(self.v_encoder, rate=self.fps)
        self.v_stream.width = self.width
        self.v_stream.height = self.height
        self.v_stream.pix_fmt = _get_best_pix_fmt(self.v_encoder)
        v_ctx = self.v_stream.codec_context
        c_name = self.v_encoder.lower()

        user_pf = self.enc_v_opts.get('pix_fmt')
        if user_pf:
            supported = [f.name for f in (getattr(v_ctx.codec, 'video_formats', None) or [])]
            if supported and user_pf not in supported:
                logging.warning(f"[AVWriter] {c_name} does not support -pix_fmt {user_pf} (supported: {', '.join(supported)}); keeping {self.v_stream.pix_fmt}")
            else:
                self.v_stream.pix_fmt = user_pf

        # what Nova's pictures are converted to. Remembered here: once a hardware encoder that was handed a
        # device is open, the stream names the GPU format (vaapi), which no picture can be converted to.
        self._v_pix_fmt = self.v_stream.pix_fmt

        # fine-grained timestamps: real frame timing is kept even when the camera's declared
        # frame rate is wrong; `framerate` still tells the encoder the nominal rate.
        try:
            v_ctx.time_base = self.V_TB
            v_ctx.framerate = Fraction(self.fps, 1)
        except Exception as e:
            logging.debug(f"[AVWriter] time_base/framerate not settable: {e}")
            self.V_TB = Fraction(1, self.fps)
        self.v_time_base = self.V_TB

        if not any(k in self.enc_v_opts for k in ('g', 'gop_size')):
            try: v_ctx.gop_size = self.fps * 2       # keyframe every 2 s (YouTube: <= 4 s)
            except Exception: pass

        final_v_opts = {}
        if "nvenc" in c_name:
            # the one encoder with a device option. The VAAPI encoders get their device as a hardware
            # context (_encoder_hwaccel); qsv, amf and mf open on the system's default device
            # (-qsv_device / -vaapi_device / -hwaccel_device are options of the ffmpeg program)
            final_v_opts['gpu'] = str(self.gpu_idx)
        # options of other encoders (-cq for libx264, -crf for nvenc) are meant for them, not for this one
        self._applied_v, skipped = for_codec(v_ctx, self.enc_v_opts)
        if skipped:
            logging.debug(f"[AVWriter] {c_name}: --ffmpeg options of other encoders left out: {skipped}")
        for k, v in self._applied_v.items():
            if k == 'pix_fmt': continue
            if k in ('gop_size',):
                try: v_ctx.gop_size = int(v)
                except Exception: pass
                continue
            final_v_opts[k] = v
        if c_name == "libx265" and 'x265-params' not in final_v_opts:
            final_v_opts['x265-params'] = 'log-level=warning'     # x265 is very chatty at INFO
        if c_name.endswith("_qsv") and not any(k in final_v_opts for k in ('bf', 'max_b_frames')):
            # Quick Sync uses B-frames by default and derives each packet's dts from its pts and the nominal
            # frame rate. Frames keep the camera's real times here, so that dts runs backwards whenever two
            # frames come closer together than nominal or one is late (Intel UHD 620: 2 to 26 of 400 packets).
            final_v_opts['bf'] = '0'
        if c_name.endswith("_mf"):
            final_v_opts = _mf_opts(c_name, final_v_opts)
        if c_name.endswith(DEVICE_PACKET_ENCODERS) and 'num_capture_buffers' not in final_v_opts:
            # four by default, and all of them are out while the encoder is emptied at the end of a file
            # (FFmpeg: "All capture buffers returned to userspace", a warning in the log every segment)
            final_v_opts['num_capture_buffers'] = '16'
        if c_name.endswith(BITRATE_NEEDED) and not any(k in final_v_opts for k in ('b', 'bit_rate')):
            try: v_ctx.bit_rate = _default_bitrate(self.width, self.height, self.fps)
            except Exception as e: logging.debug(f"[AVWriter] bit_rate not settable: {e}")
        v_ctx.options.update(final_v_opts)

        tag = v_tag
        if not tag and _family(c_name) == "hevc" and self.fmt in ("mp4", "mov", "hls"):
            tag = "hvc1"                                  # Apple/YouTube-compatible HEVC tag
        if tag:
            try: v_ctx.codec_tag = tag
            except Exception as e: logging.debug(f"[AVWriter] codec_tag {tag} rejected: {e}")
        logging.debug(f"[AVWriter] video {c_name} pix_fmt={self.v_stream.pix_fmt} gop={getattr(v_ctx, 'gop_size', '?')} opts={v_ctx.options}")

    def _setup_audio(self, audio_enable, a_encoder):
        self.a_stream = None
        self.a_codec = a_encoder or "aac"
        have_src_audio = audio_enable and self.sample_rate > 0 and self.channels > 0
        if not audio_enable:
            return
        if not have_src_audio:
            if self.audio_wait > 0:
                self._audio_pending = True
                return
            if not self.live:
                return
            # live target without camera audio -> silent AAC so the ingest server accepts the stream
            self.silence = True
            self.sample_rate, self.channels = 48000, 1
        # -ar / -ac: PyAV resamples the camera audio to the encoder's rate/layout
        try: self.sample_rate = int(self.enc_a_opts.get('ar') or self.sample_rate)
        except ValueError: logging.warning(f"[AVWriter] ignoring invalid -ar {self.enc_a_opts.get('ar')}")
        try: self.channels = int(self.enc_a_opts.get('ac') or self.channels)
        except ValueError: logging.warning(f"[AVWriter] ignoring invalid -ac {self.enc_a_opts.get('ac')}")
        self.a_time_base = Fraction(1, self.sample_rate)
        self._add_audio_stream()

    def _audio_stream(self, container, quiet=False):
        stream = container.add_stream(self.a_codec, rate=self.sample_rate)
        a_ctx = stream.codec_context
        a_ctx.sample_rate = self.sample_rate
        layout = {1: "mono", 2: "stereo"}.get(self.channels, f"{self.channels}c")
        try: a_ctx.layout = layout
        except Exception as e: logging.debug(f"[AVWriter] layout {layout} rejected: {e}")
        self._applied_a, skipped = for_codec(a_ctx, {k: v for k, v in self.enc_a_opts.items() if k not in ('ar', 'ac')})
        if skipped and not quiet:
            logging.debug(f"[AVWriter] {self.a_codec}: --ffmpeg options of other encoders left out: {skipped}")
        a_ctx.options.update(self._applied_a)
        if self.a_codec == "aac":
            a_ctx.options.setdefault("aac_coder", AAC_CODER)
            self._cap_aac_bitrate(a_ctx, layout, quiet)
        return stream

    def _add_audio_stream(self):
        """The audio encoder is opened here, not lazily: PyAV retries avcodec_open2() on every encode
        and again when the header is written, and a second open of a context whose first open failed
        segfaults (AAC at an unsupported -ar, say). The settings are tried on a throw-away stream
        first; an audio track that cannot open is left out rather than costing the video."""
        self.a_stream = None
        with logsync.av_quiet():                       # a throw-away context: its complaints are ours to report
            scratch = av.open(self.path, mode="w", format=self.fmt)    # never started: nothing is written
            try:
                self._audio_stream(scratch, quiet=True).codec_context.open()
            except Exception as e:
                logging.warning(f"[AVWriter] ⚠️ audio encoder {self.a_codec} does not open at {self.sample_rate} Hz, "
                                f"{self.channels} ch ({_short_err(e)}); no audio track")
                self.silence = False
                return
            finally:
                try: scratch.close()
                except Exception: pass
        stream = self._audio_stream(self.container)
        try:
            stream.codec_context.open()
        except Exception as e:
            self.open_failed = f"audio encoder {self.a_codec} does not open: {_short_err(e)}"
            raise OutputOpenError(f"cannot start: {self.open_failed}") from e
        self.a_stream = stream
        self.audio_track = True         # this file has an audio track (stays set after close)
        a_ctx = stream.codec_context
        logging.debug(f"[AVWriter] audio {self.a_codec} {self.sample_rate}Hz {a_ctx.layout.name} opts={a_ctx.options}")

    def _decide_audio(self, frame):
        """End of the wait for camera audio: frame is its first audio frame, or None (none came)."""
        self._audio_pending = False
        self.audio_wait = 0
        if frame is not None:
            self.sample_rate = int(frame.sample_rate or 0)
            self.channels = int(frame.layout.nb_channels or 0)
        self._setup_audio(True, self.a_codec)
        if frame is None:
            logging.info(f"[AVWriter] no camera audio within {AUDIO_WAIT:g} s: "
                         f"{'silent track' if self.silence else 'no audio track'}")
        else:
            logging.info(f"[AVWriter] camera audio: {self.sample_rate} Hz, {self.channels} ch -> "
                         f"{self.a_stream.codec_context.codec.name if self.a_stream else 'no audio track'}")
        pending, self._pending = self._pending, []
        self._mux(pending)

    # ------------------------------------------------------------------ writing
    def _mux(self, packets):
        """Hand packets to the muxer.  Audio is held back until the first video packet exists:
        encoders with look-ahead (libx264: ~40 frames) emit their first packet late, and if audio
        reaches the muxer first its timestamp offset is fixed too early, which turns the video's
        B-frame DTS negative ('Packets poorly interleaved, failed to avoid negative timestamp').
        Starting with video also keeps RTMP ingest servers happy."""
        for packet in packets:
            if self._audio_pending:              # the streams are not final yet: no header possible
                self._pending.append(packet)
                continue
            if not self._video_started:
                if self.v_stream is not None and packet.stream is self.v_stream:
                    if not self.copy and self._takes_headers(packet):
                        continue
                    self._video_started = True
                    self._write(packet)
                    for a in self._held_audio:
                        self._write(a)
                    self._held_audio.clear()
                    continue
                if len(self._held_audio) < 4096:
                    self._held_audio.append(packet)
                    continue
            self._write(packet)

    def _takes_headers(self, packet):
        """Is this packet, the encoder's first, the stream's header and not a picture?  FFmpeg's V4L2
        encoders, asked for a global header (MP4, FLV), put the parameter sets into a packet of their own
        and leave the extradata empty: an MP4 written from that has no avcC and its first sample is no
        picture, so nothing plays it (Raspberry Pi 4, h264_v4l2m2m).  The packet becomes the encoder's
        extradata, which the muxer is given with the first packet that is written."""
        ctx = self.v_stream.codec_context
        try:
            if ctx.extradata:
                return False
            data = bytes(packet)
            if not _only_param_sets(data, _family(self.v_encoder) == "hevc"):
                return False
            ctx.extradata = data
        except Exception as e:
            logging.debug(f"[AVWriter] the encoder's first packet was not looked at: {_short_err(e)}")
            return False
        logging.debug(f"[AVWriter] {self.v_encoder} sent its parameter sets as a packet of their own "
                      f"({len(data)} bytes): they are the stream's header now")
        return True

    _dts_warned = set()             # encoders whose dts has been reported (once per process)

    def _monotonic(self, packet):
        """Keep the video dts increasing. A muxer refuses a packet whose dts does not ("non monotonically
        increasing dts", EINVAL): the packet is lost, and with it every picture that refers to it. An
        encoder that derives dts from pts and the nominal frame rate (Quick Sync with B-frames) makes such
        packets out of a camera's real frame times. Like the ffmpeg program: move the dts just past the
        previous one, and the pts with it where it would fall behind."""
        if self.v_stream is None or packet.stream is not self.v_stream:
            return
        dts, last = packet.dts, self._last_v_dts
        if dts is None:
            return
        if last is not None and dts <= last:
            new = last + 1
            if not self._dts_fixed and self.v_encoder not in AVWriter._dts_warned:
                AVWriter._dts_warned.add(self.v_encoder)
                logging.warning(f"[AVWriter] {self.v_encoder} gave a dts that does not increase ({dts} after {last}); "
                                f"moved to {new}. With B-frames and a camera's uneven frame times, try -bf 0")
            self._dts_fixed += 1
            if packet.pts is not None and packet.pts < new:
                packet.pts = new
            packet.dts = dts = new
        self._last_v_dts = dts

    def _write(self, packet):
        if self.first_packet_sent:
            self._monotonic(packet)
            self.container.mux(packet)
            return
        if self.open_failed:
            # PyAV would run avformat_write_header() a second time on a context whose first header
            # write failed (ENOSPC on a new segment, a publish refused after connect): a segfault
            self._refuse()
        self._monotonic(packet)
        try:
            self.container.mux(packet)     # the first packet opens the output: connect, handshake, header
        except av.error.ExitError as e:
            self.open_failed = f"no answer within {self.timeout[0]:g} s" if self.timeout else str(e)
            raise OutputOpenError(f"cannot open output: {self.open_failed}") from e
        except Exception as e:
            self.open_failed = str(e) or type(e).__name__
            raise OutputOpenError(f"cannot open output: {self.open_failed}") from e
        self.first_packet_sent = True
        self.first_packet_t = time.monotonic()
        logging.debug("[AVWriter] first packet muxed")

    def _refuse(self):
        # a failed open is final for this writer: its codec contexts and muxer must not be touched again
        raise OutputOpenError(f"cannot open output: {self.open_failed}")

    def write_video(self, frame):
        if frame is None or not self.run_flag: return
        if self.open_failed: self._refuse()
        out_t = self.v_tl.map(_src_time(frame))
        if self.v_tl.held:
            self._held_v += 1               # part of a burst beyond LIVE_MAX_LEAD
            return
        if self._held_v:
            logging.info(f"[AVWriter] source ran ahead of real time: {self._held_v} frame(s) dropped")
            self._held_v = 0

        if self.out_fps:                      # -r: keep at most out_fps frames per second
            if out_t + 1e-6 < self._next_frame_t:
                return
            step = 1.0 / self.out_fps
            self._next_frame_t = max(self._next_frame_t + step, out_t - step)

        for f in self._filter(frame):
            self._encode_video(f, out_t)
        self._after_video(out_t)

    def _after_video(self, out_t):
        """What a written picture means for the sound: the end of the wait for the camera's, more silence."""
        if self._audio_pending:
            if self._audio_deadline is None:
                self._audio_deadline = out_t + self.audio_wait
            elif out_t >= self._audio_deadline or len(self._pending) > 2000:
                self._decide_audio(None)
        if self.silence:
            self._fill_silence(out_t)

    def _fit(self, frame, pts):
        target_pf = self._v_pix_fmt
        if frame.format.name != target_pf or frame.width != self.width or frame.height != self.height:
            rng = (self._colour or _frame_colour(frame))[0]
            # the sample values stay what they are (a full-range picture is not squeezed into 16-235):
            # the encoder is told the range instead (_carry_colour)
            kw = {"src_color_range": rng, "dst_color_range": rng} if rng else {}
            frame = frame.reformat(width=self.width, height=self.height, format=target_pf,
                                   threads=CONV_THREADS, **kw)
        frame.pts = pts
        frame.time_base = self.v_time_base
        # decoded frames carry the *source* picture type (I/P/B); libx264/nvenc would treat it as
        # a forced frame type and break their own GOP/DTS logic
        try: frame.pict_type = 0
        except Exception: pass
        return frame

    def _encode_video(self, frame, out_t):
        pts = int(round(out_t / self.v_time_base))
        if pts <= self.last_v_pts:
            pts = self.last_v_pts + 1
        self.last_v_pts = pts
        if self._colour is None:
            self._colour = _frame_colour(frame)
        while True:
            ctx, f = self.v_stream.codec_context, self._fit(frame, pts)
            if ctx.is_open and self.v_frames:
                packets = self.v_stream.encode(f)
                break
            opened = ctx.is_open
            if not opened:
                self._carry_colour(ctx, f)
            try:
                packets = self.v_stream.encode(f)        # the first call opens the encoder
                break
            except Exception as e:
                # An encoder that opens and does not take its first picture is one that does not open.  A
                # board's V4L2 encoder with more cameras than it has room for opens for each and answers
                # every picture with EAGAIN (Raspberry Pi 4: two 1080p cameras at a time), and the kernel
                # writes a warning with a call trace for every try: 60 pictures a camera before the
                # recorder gave up, and again a minute later, took the board down.
                self._next_video_encoder(e, taking=opened or ctx.is_open)
        self._mux(self._owned(packets))
        self.v_frames += 1
        if not self._v_opts_checked:
            self._v_opts_checked = True
            self._report_unused(self.v_stream.codec_context, "video encoder", self._applied_v)

    def encoder_note(self):
        """For `nova status`: the encoder in use when it is not the one that was asked for (-e, or the
        hardware encoder that goes with --accel), and why; None when it is.  As VideoWorker.accel_note:
        the log's line about it is a warning, which a quiet camera does not write."""
        accel, asked, source, _, allowed = self._enc_args
        used, mgr = self.v_encoder, EncoderManager()
        cpu = " on the CPU" if any(used in names for names in SOFTWARE_ENCODERS.values()) else ""
        if asked:
            if used == asked:
                return None
            why = mgr.why(asked)
            return f"{used}{cpu} in place of -e {asked}" + (f" ({why})" if why else "")
        if not accel or not _ACCEL_ENCODER.get(accel.lower()):
            return None
        if used.endswith(tuple("_" + suf for suf in _hw_suffixes(accel))):
            return None
        tried = [n for n in mgr._candidates(accel, None, source, allowed) if mgr.why(n)]
        why = f" ({tried[0]}: {mgr.why(tried[0])})" if tried else ""
        return f"{used}{cpu}: no hardware encoder is in use for --accel {accel}{why}"

    def copy_note(self, why):
        """For `nova status`: this writer encodes where -e copy was asked for, and why."""
        used = self.v_encoder
        cpu = " on the CPU" if any(used in names for names in SOFTWARE_ENCODERS.values()) else ""
        return f"{used}{cpu} in place of -e copy ({why})"

    def _owned(self, packets):
        if packets and self.v_encoder.lower().endswith(DEVICE_PACKET_ENCODERS):
            return [_own_packet(p) for p in packets]
        return packets

    def _carry_colour(self, ctx, frame):
        """Tell an encoder that is about to open what the camera's pictures are. Without it the file says
        nothing, players assume limited range, and a camera that sends full range (yuvj420p) is played
        with its blacks and whites clipped: 6 of 255 off on average, 21 at worst, through every encoder.
        The matrix, primaries and transfer are passed on when the camera names them. An option the user
        gave (--ffmpeg "-color_range tv") stays."""
        given = getattr(ctx, "options", None) or {}
        names = ("color_range", "colorspace", "color_primaries", "color_trc")
        for name, value in zip(names, self._colour or ()):
            if value is None or name in given:
                continue
            try:
                setattr(ctx, name, value)
                if name == "color_range":
                    frame.color_range = value
            except Exception as e:
                logging.debug(f"[AVWriter] {name}={value} not settable: {e}")
        if self._colour and self._colour[0] == 2:
            logging.debug("[AVWriter] the camera sends full-range video: the recording says so")

    def _next_video_encoder(self, e, taking=False):
        """The video encoder opens at its first frame, at the real size; the probe ran at 640x360 and
        can pass where this fails (NVENC's session limit with many cameras, a GPU fault). The failed
        context must never be used again (a second avcodec_open2() on it segfaults), so the container
        is rebuilt around the next candidate, ending at libx264.  `taking`: it opened, and the first
        picture is what it refused."""
        failed, err = self.v_encoder, _short_err(e)
        does_not = "opens and takes no picture (as an encoder does that has no room for one more camera)" if taking \
            else "does not open"
        if "nvenc" in failed and self.gpu_idx not in (0, None):
            # --gpu N with fewer NVIDIA cards than that: the same encoder on GPU 0 before giving it up
            logging.warning(f"[AVWriter] ⚠️ {failed} does not open on GPU {self.gpu_idx} ({err}); trying GPU 0 "
                            f"(--gpu / NOVA_GPU_DEVICES count the NVIDIA cards, nvidia-smi lists them)")
            self.gpu_idx = 0
            self._rebuild(failed)
            return
        self._tried.add(failed)
        mgr = EncoderManager()
        first = mgr.reject(failed, f"{does_not} at {self.width}x{self.height}: {err}")
        nxt = mgr.get_encoder(*self._enc_args)
        if nxt in self._tried:
            self.open_failed = f"video encoder {failed} {does_not} at {self.width}x{self.height}: {err}"
            raise OutputOpenError(f"cannot start: {self.open_failed}") from e
        (logging.warning if first else logging.info)(
            f"[AVWriter] ⚠️ {failed} {does_not} at {self.width}x{self.height} ({err}); using {nxt}, "
            f"{failed} is tried again in {FAILED_RETRY_S // 60} min")
        self._rebuild(nxt)

    # ---- -vf support (libavfilter) -----------------------------------------------------------
    def _filter(self, frame):
        if not self.vf:
            return (frame,)
        first = self.graph is None
        if first:
            self.graph = self._build_graph(frame)
        try:
            self.graph.push(frame)
        except Exception as e:
            raise RuntimeError(f"-vf graph rejected a frame: {_short_err(e)}")
        out = []
        while True:
            try:
                out.append(self.graph.pull())
            except (BlockingIOError, EOFError):
                break
            except Exception as e:
                msg = str(e).lower()
                if "resource temporarily unavailable" in msg or "end of file" in msg:
                    break
                raise
        if first and out:
            # the graph decides the output size (transpose, scale, crop ...); the encoder is not
            # open yet, so its stream parameters can still follow the first filtered frame
            o = out[0]
            w, h = o.width - o.width % 2, o.height - o.height % 2
            if (w, h) != (self.width, self.height):
                self.width, self.height = w, h
                self.v_stream.width, self.v_stream.height = w, h
            logging.info(f"[AVWriter] -vf '{self.vf}' -> {w}x{h} {o.format.name}")
        return out

    def _build_graph(self, frame):
        import av.filter
        if frame.time_base is None:
            frame.time_base = self.v_time_base
        g = av.filter.Graph()
        # the buffer source is given the frame's colour space and range as well: PyAV's add_buffer()
        # leaves them unspecified, and libavfilter then warns at every frame that the properties change
        tb = frame.time_base
        last = g.add("buffer", f"video_size={frame.width}x{frame.height}:pix_fmt={frame.format.name}"
                               f":time_base={tb.numerator}/{tb.denominator}"
                               f":colorspace={int(frame.colorspace)}:range={int(frame.color_range)}")
        for name, args in _parse_filtergraph(self.vf):
            try:
                f = g.add(name, args) if args else g.add(name)
            except Exception as e:
                raise RuntimeError(f"-vf: filter '{name}' is not available in this FFmpeg build ({_short_err(e)})")
            last.link_to(f)
            last = f
        sink = g.add("buffersink")
        last.link_to(sink)
        g.configure()
        return g

    def _cap_aac_bitrate(self, a_ctx, layout, quiet=False):
        """FFmpeg's AAC encoder takes at most 6144 bits per frame and channel, i.e. 6 x sample rate x
        channels bit/s (48 kbit/s for an 8 kHz mono camera).  Above that it clamps and logs an ERROR at
        every start - for its own default (69 kbit/s mono, 128 stereo) too.  Pick the cap up front."""
        cap = 6 * self.sample_rate * self.channels
        raw = self.enc_a_opts.get('b')
        want = parse_bitrate(raw) if raw is not None else {1: 69000, 2: 128000}.get(self.channels, 64000 * self.channels)
        if want is None or not 0 < cap < want:
            return
        a_ctx.options.pop('b', None)
        a_ctx.bit_rate = cap
        if quiet: return
        logging.info(f"[AVWriter] aac {self.sample_rate} Hz {layout}: {want // 1000} kbit/s exceeds the "
                     f"encoder's limit, using {cap // 1000} kbit/s")

    def _report_unused(self, ctx, what, user_opts):
        """After open(), PyAV leaves the options the encoder did not recognise in ctx.options.
        Only the user's own --ffmpeg keys are reported (PyAV also parks container options there)."""
        try:
            unused = {k: v for k, v in dict(ctx.options).items() if k in user_opts}
        except Exception:
            return
        if unused:
            logging.warning(f"[AVWriter] ⚠️ {what} {ctx.codec.name} ignored unknown --ffmpeg options: {unused}")

    def write_audio(self, frame):
        if frame is None or not self.run_flag: return
        if self.open_failed: self._refuse()
        if self._audio_pending:
            self._decide_audio(frame)
        if not self.a_stream: return
        if self.silence:
            # the camera's audio started later than audio_wait: it replaces the silent track (it used
            # to be dropped for the rest of the stream)
            self.silence = False
            logging.info("[AVWriter] camera audio arrived; it replaces the silent track")
        out_t = self.a_tl.map(_src_time(frame), frame.samples / frame.sample_rate if frame.sample_rate else None)
        if self.a_tl.held:
            return                                # a burst beyond LIVE_MAX_LEAD (see write_video)
        pts = max(int(round(out_t * self.sample_rate)), self.last_a_end)
        frame.pts = None                          # timestamps are set on the converted frames below
        for f in self._convert(frame):
            f.pts = pts
            f.time_base = self.a_time_base
            pts += f.samples
            self._mux(self.a_stream.encode(f))
        self.last_a_end = pts
        self.a_frames += 1
        if not self._a_opts_checked:
            self._a_opts_checked = True
            self._report_unused(self.a_stream.codec_context, "audio encoder", self._applied_a)

    def _convert(self, frame):
        """Camera audio in the encoder's own format, layout and rate. PyAV's encoder-side resampler
        is set up from the first frame and refuses every other format ('Frame does not match
        AudioResampler setup'), so a camera whose audio changed after a reconnect lost the track."""
        key = (frame.format.name, frame.layout.name, frame.sample_rate)
        if key != self._a_conv_key:
            if self._a_conv_key is not None:
                logging.info(f"[AVWriter] camera audio changed to {key[2]} Hz {key[1]} {key[0]}")
            ctx = self.a_stream.codec_context
            self._a_conv = av.AudioResampler(format=ctx.format, layout=ctx.layout, rate=ctx.sample_rate)
            self._a_conv_key = key
        return self._a_conv.resample(frame)

    def _fill_silence(self, out_t):
        target = int(out_t * self.sample_rate)
        if target - self.last_a_end > self.sample_rate * _Timeline.MAX_JUMP:
            # a camera outage, which the video timeline re-anchors over: leave the same gap instead of
            # encoding it as silence in one burst (10 h took ~33 s while the decoder's queue overflowed)
            self.last_a_end = target
        ctx = self.a_stream.codec_context
        n = 1024
        while self.last_a_end < target:
            # in the encoder's format, like converted camera audio, so either can follow the other
            f = av.AudioFrame(format=ctx.format.name, layout=ctx.layout.name, samples=n)
            f.sample_rate = self.sample_rate
            for p in f.planes:
                p.update(bytes(p.buffer_size))
            f.pts = self.last_a_end
            f.time_base = self.a_time_base
            self.last_a_end += n
            self._mux(self.a_stream.encode(f))
            self.a_frames += 1

    def _flush(self):
        if self.open_failed:
            return                                    # flushing would mux again (see _write): only free it
        if self._audio_pending:                       # closed before any camera audio: no track
            self._audio_pending = False
            pending, self._pending = self._pending, []
            self._mux(pending)
        if self.v_frames and self.v_stream and not self.copy:
            self._mux(self._owned(self.v_stream.encode()))
        if self.a_frames and self.a_stream:
            self._mux(self.a_stream.encode())
        if self._held_audio:                          # no video packet ever came out
            for a in self._held_audio: self._write(a)
            self._held_audio.clear()

    def close(self):
        if not self.run_flag: return
        self.run_flag = False
        try:
            self._flush()
        except Exception as e:
            logging.debug(f"[AVWriter] flush: {e}")
        finally:
            try: self.container.close()
            except Exception as e: logging.debug(f"[AVWriter] container close: {e}")
            self._release()
        fixed = f", {self._dts_fixed} dts moved" if self._dts_fixed else ""
        watch = getattr(self, "watch", None)
        steps = watch.taken - self._taken0 if watch else 0
        if steps:
            fixed += f", {steps} step(s) of the source's timestamps not written as gaps"
        logging.debug(f"[AVWriter] closed ({self.v_frames} video / {self.a_frames} audio frames{fixed})")

    def _release(self):
        """Free the codecs, filter graphs and resamplers now, not hours later.  PyAV's AudioResampler
        (ours, and the one its audio encoders keep inside) runs a libavfilter graph with a thread pool
        of one thread per core, and a Graph and its filter contexts refer to each other: only the cyclic
        garbage collector frees them, and in a long-running process it may not run a full collection for
        hours.  A stream that reconnected 20 times in a morning kept 315 idle threads and 85 MB."""
        self.container = self.v_stream = self.a_stream = None
        self._a_conv = self.graph = None
        self._pending, self._held_audio = [], []
        t = time.monotonic()
        n = gc.collect()
        logging.debug(f"[AVWriter] released: {n} objects collected in {1000 * (time.monotonic() - t):.0f} ms")
