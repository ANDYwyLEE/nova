import re
import av
import sys
import os
import traceback
import threading
import tempfile
import numpy as np
import logging
from fractions import Fraction
from util import LINUX, WINDOWS, MAC, WSL, IS_PI, SOLARIS, AIX, HPUX, get_os_type


try: from filelock import FileLock
except ImportError:
    print("Error: requests is required. Run 'pip install filelock'")
    sys.exit(1)

def _get_best_pix_fmt(encoder_name: str) -> str:
    e = encoder_name.lower()
    if any(x in e for x in ["nvenc", "qsv", "rkmpp", "videotoolbox", "amf", "vaapi", ]):
        return "nv12"
    return "yuv420p"

PATTERN = re.compile(r'^\[[^\]]+\]\s*([^:]+)')

def _short_err(e: Exception) -> str:
    m = PATTERN.search(str(e))
    return m.group(1).strip() if m else str(e)


class EncoderManager:
    _instance = None
    _lock = threading.Lock()
    _best_encoder_cache = None

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(EncoderManager, cls).__new__(cls)
            return cls._instance

    def get_encoder(self, accel, user_encoder, source_codec, enc_opts):
        if self._best_encoder_cache:
            logging.debug(f"[EncoderManager] ⚡ Cache Hit! Returning: {self._best_encoder_cache}")
            return self._best_encoder_cache
        lock_path = os.path.join(tempfile.gettempdir(), "nova_encoder_init.lock")
        file_lock = FileLock(lock_path, timeout=15)
        with self._lock:
            if self._best_encoder_cache:
                logging.debug(f"[EncoderManager] ⚡ Using cached encoder: {self._best_encoder_cache}")
                return self._best_encoder_cache
            try:
                with file_lock:
                    if self._best_encoder_cache:
                        logging.debug(f"[EncoderManager] ⚡ Using cached encoder: {self._best_encoder_cache}")
                        return self._best_encoder_cache

                    old_level = av.logging.get_level()
                    av.logging.set_level(av.logging.PANIC)
                    self._best_encoder_cache = self._perform_hw_test(accel, user_encoder, source_codec, enc_opts)
                    av.logging.set_level(old_level)
                    return self._best_encoder_cache
            except Exception as e:
                logging.error(f"[EncoderManager] Lock acquisition failed: {e}")
                return "libx264"

    def _perform_hw_test(self, accel, encoder, source_codec, enc_opts):
        os_name = get_os_type()
        logging.debug(f"[EncoderManager] Platform Check - OS:{os_name}")
        target = "hevc" if any(x in source_codec.lower() for x in ["265", "hevc"]) else "h264"
        candidates = []
        if accel:
            if IS_PI:
                candidates += [
                        "libsvtav1", "libdav1d",
                        f"{target}_rkmpp", f"{target}_v4l2m2m",
                        f"{target}_nvmpi", f"{target}_omx"
                ]
            elif WINDOWS or LINUX or WSL:
                candidates += [
                    "av1_nvenc", "av1_qsv", "av1_vaapi", "av1_amf",
                    "hevc_nvenc", "hevc_qsv", "hevc_vaapi", "hevc_amf",
                    "h264_nvenc", "h264_qsv", "h264_vaapi", "h264_amf",
                    f"{target}_nvenc", f"{target}_qsv",
                ]
            elif MAC:
                candidates += ["av1_videotoolbox", "hevc_videotoolbox", "h264_videotoolbox"]
            elif SOLARIS or AIX or HPUX:
                candidates += [f"{target}_vaapi"]
            candidates += ["libx264"]
        else:
            candidates += ["libx264", "h264"]

        u_enc = encoder or enc_opts.get('c:v') or enc_opts.get('vcodec')
        encoder_list = [u_enc] if u_enc else []
        for c in candidates:
            if c not in encoder_list: encoder_list.append(c)
        for encoder_name in encoder_list:
            ctx = None
            try:
                logging.debug(f"[EncoderManager] 🔍 Testing encoder: {encoder_name}...")
                codec = av.Codec(encoder_name, "w")
                ctx = av.CodecContext.create(codec)
                test_pix_fmt = _get_best_pix_fmt(encoder_name)
                ctx.width = 640
                ctx.height = 360
                ctx.pix_fmt = test_pix_fmt
                ctx.time_base = Fraction(1, 30)
                ctx.options.update(enc_opts)
                ctx.open()
                if hasattr(ctx, 'close'): ctx.close()
                del ctx
                logging.verbose(f"[EncoderManager] 🚀 Success! Using {encoder_name} (Format: {test_pix_fmt})")
                return encoder_name
            except Exception as e:
                if ctx is not None:
                    if hasattr(ctx, 'close'): ctx.close()
                    del ctx
                logging.debug(f"[EncoderManager] ❌ {encoder_name} check failed: {e}")
                continue
        logging.debug(f"[EncoderManager] ⚠️ All candidates failed. Final fallback: libx264")
        return "libx264"


def _build_conn_options(path, accel, user_open_opts, gpu_idx):
    v = os.getenv("NOVA_TIMEOUT", "10000000")
    logging.trace(f"[AVWriter] NOVA_TIMEOUT={v}")
    s = str(v)

    options = {}
    p = path.lower()
    if any(p.startswith(proto) for proto in ['rtmp', 'srt', 'http', 'https', 'tcp', 'udp']):
        options.update({
            'rw_timeout': s,  # 10sec
            'stimeout':   s,    # connection timetout
        })
    if p.startswith('rtmp'):
        options['tcp_nodelay'] = '1'
        options['rtmp_live'] = 'live'
    elif p.startswith('srt'):
        options.update({
            'transtype': 'live',
            'latency': '200000',  # 200ms
            'mode': 'caller'
        })
    elif p.startswith('http'):
        options['method'] = 'POST' # or PUT
    if p.endswith('.mp4') or p.endswith('.mkv'):
        pass
    #if accel == 'qsv':
    #    options['qsv_device'] = str(gpu_idx)
    options.update(user_open_opts)

    return options

def _determine_output_format(path, user_fmt=None):
    if user_fmt: return user_fmt
    p = path.lower()
    if p.startswith(('rtmp://', 'rtmps://')):
        return 'flv'
    if p.startswith('srt://'):
        return 'mpegts'
    if p.startswith(('http://', 'https://')):
        if '.m3u8' in p: return 'hls'
        if '.ts' in p: return 'mpegts'
    if p.endswith('.mp4'): return 'mp4'
    if p.endswith('.mkv'): return 'matroska'
    return None

class AVWriter:
    _cached_encoder = None

    def __init__(
            self, accel,
            path, width, height, fps,
            encoder=None,
            source_codec="h264",
            open_opts={},
            enc_v_opts={},
            enc_a_opts={},
            sample_rate=0, channels=0, audio_enable=True,
            gpu_idx=0
        ):
        self.path = path
        self.width = width
        self.height = height
        self.fps = fps
        self.encoder = encoder
        self.source_codec = source_codec
        self.open_opts = open_opts
        self.enc_v_opts = enc_v_opts
        self.enc_a_opts = enc_a_opts
        self.sample_rate = sample_rate or 0
        self.channels = channels or 0
        self.audio_enable = audio_enable and self.sample_rate > 0 and self.channels > 0
        self.gpu_idx = gpu_idx
        self.encoder_initialized = False
        self.run_flag = True
        self.first_packet_sent = False

        user_f = self.enc_v_opts.get('f')
        fmt = _determine_output_format(self.path, user_f)

        open_opts = _build_conn_options(self.path, accel, self.open_opts, self.gpu_idx)
        self.container = av.open(self.path, mode="w", format=fmt, options=open_opts)

        manager = EncoderManager()
        self.v_encoder = manager.get_encoder(accel, encoder, source_codec, enc_v_opts)

        self.v_stream = self.container.add_stream(self.v_encoder, rate=self.fps)
        self.v_stream.width = self.width
        self.v_stream.height = self.height

        c_name = self.v_encoder.lower()
        self.v_stream.pix_fmt = _get_best_pix_fmt(c_name)
        logging.debug(f"[AVWriter] 🚀 Encoder: {self.v_encoder} | Selected pix_fmt: {self.v_stream.pix_fmt}")

        v_ctx = self.v_stream.codec_context
        gpu_id_str = str(self.gpu_idx)


        final_v_opts = {}
        if "nvenc" in c_name:
            final_v_opts['gpu'] = gpu_id_str
        elif "qsv" in c_name:
            final_v_opts['qsv_device'] = gpu_id_str
        elif "vaapi" in c_name:
            final_v_opts['vaapi_device'] = f"/dev/dri/renderD{128 + self.gpu_idx}"
        elif "amf" in c_name or "_mf" in c_name:
            final_v_opts['hwaccel_device'] = gpu_id_str

        for k, v in self.enc_v_opts.items():
            if k in final_v_opts and final_v_opts[k] != v:
                logging.info(f"[AVWriter] 🔄 Overriding default {k}: {final_v_opts[k]} -> {v}")
            final_v_opts[k] = v

        v_ctx.options.update(final_v_opts)
        logging.debug(f"[AVWriter] Applied Video Encoder Config ({c_name}): {v_ctx.options}")


        if self.audio_enable:
            self.a_stream = self.container.add_stream("aac", rate=self.sample_rate)
            a_ctx = self.a_stream.codec_context
            a_ctx.sample_rate = self.sample_rate
            if self.channels == 1: a_ctx.layout = "mono"
            elif self.channels == 2: a_ctx.layout = "stereo"
            else: a_ctx.layout = f"{self.channels}c"
            self.a_time_base = Fraction(1, self.sample_rate)
            a_ctx.options.update(self.enc_a_opts)
            logging.debug(f"[AVWriter] Applied Audio Encoder Config ({a_ctx.codec.name}): {a_ctx.options}")
            logging.debug(f"[AVWriter] Audio Initialized: {a_ctx.layout} @ {self.sample_rate}Hz")

        else:
            self.a_stream = None

        self.v_pts = 0
        self.a_pts = 0
        self.v_time_base = Fraction(1, max(self.fps, 1))


    def write_video(self, frame):
        if frame is None: return
        if not self.encoder_initialized:
            codec = self.v_stream.codec_context.codec
            supported_formats = [
                f.name for f in (
                    getattr(codec, 'video_formats', []) or
                    getattr(codec, 'pix_fmts', [])
                )
            ]
            if supported_formats:
                fmt_names = ", ".join(supported_formats)
                logging.debug(f"[AVWriter] 🟢 {codec.name} supported formats: [{fmt_names}]")
            else:
                logging.debug(f"[AVWriter] ⚠️ {codec.name} - No format info available")

            incoming_pf = frame.format.name
            if incoming_pf in supported_formats:
                self.v_stream.pix_fmt = incoming_pf
                logging.debug(f"[AVWriter] 🔄 Format Matched: {incoming_pf} is supported.")
            else:
                target_pf = _get_best_pix_fmt(self.v_encoder)
                old_pf = self.v_stream.pix_fmt
                self.v_stream.pix_fmt = target_pf
                logging.debug(f"[AVWriter] ⚠️ Format Override: Source {incoming_pf} -> Target {target_pf}.")
            self.encoder_initialized = True

        if frame.format.name != self.v_stream.pix_fmt:
            target_format = self.v_stream.pix_fmt
            frame = frame.reformat(format=target_format)

        frame.pts = self.v_pts
        frame.time_base = self.v_time_base
        self.v_pts += 1

        for packet in self.v_stream.encode(frame):
            self.container.mux(packet)
            if not self.first_packet_sent:
                self.first_packet_sent = True
                logging.debug("[AVWriter] Real connection established (First packet muxed).")

    def write_audio(self, frame):
        if not self.a_stream: return
        frame.pts = self.a_pts
        frame.time_base = self.a_time_base
        self.a_pts += frame.samples
        for packet in self.a_stream.encode(frame):
            packet.stream = self.a_stream
            if packet.pts is None:
                packet.pts = frame.pts
                packet.dts = frame.pts
            self.container.mux(packet)

    def close(self):
        if not self.run_flag: return
        self.run_flag = False
        try:
            if self.first_packet_sent:
                if self.v_stream:
                    for packet in self.v_stream.encode():
                        self.container.mux(packet)
                if self.a_stream:
                    for packet in self.a_stream.encode():
                        self.container.mux(packet)
        except Exception: pass
        finally:
            self.container.close()
        logging.debug(f"[AVWriter] closed")
