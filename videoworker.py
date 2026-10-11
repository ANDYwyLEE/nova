# videoworker.py

import os
import re
import time
import sys
import traceback
import random
import logging
import numpy as np
import queue
from collections import deque
from urllib.parse import urlparse
from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

from mytools import Skipper, FrameRator, Clock
from util import LINUX, WINDOWS, MAC, WSL, MSYS, SOLARIS, AIX, HPUX, IS_PI
from codec_names import get_friendly_codec_name
from ffparser import FFmpegParser

import av
from av.codec.hwaccel import HWAccel, hwdevices_available
HWACCELS = hwdevices_available()

PATTERN = re.compile(r'^\[[^\]]+\]\s*([^:]+)')

def _short_err(e: Exception) -> str:
    m = PATTERN.search(str(e))
    return m.group(1).strip() if m else str(e)


def _queue_nowait_put(q, i):
    try:
        q.put_nowait(i)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        q.put_nowait(i)

def _pick_gpu_index(default_idx=0):
    v = os.getenv("NOVA_GPU_DEVICES")
    logging.trace(f"[VideoWorker] NOVA_GPU_DEVICES={v}")
    if not v:
        return default_idx
    try:
        nums = [int(x.strip()) for x in v.split(",")]
        return random.choice(nums)
    except Exception:
        return default_idx

def _build_conn_options(url):
    v = os.getenv("NOVA_TIMEOUT", "5000000")
    logging.trace(f"[VideoWorker] NOVA_TIMEOUT={v}")
    s = str(v)
    options = {}
    if not url: return options
    if url.startswith('rtsp'):
        options['rtsp_transport'] = 'tcp'
        options['timeout'] = s
        options['stimeout'] = s
        options['rw_timeout'] = s
    return options

def _choose_decoder(hw_backend: str, codec_name: str) -> str:
    logging.trace(f"_choose_decoder: hw_backend:{hw_backend}, codec_name:{codec_name}")
    if not hw_backend:
        return codec_name
    c = codec_name.lower()
    if 'avc' in c: target = 'h264'
    elif 'h265' in c: target = 'hevc'
    else: target = c
    hb = hw_backend.lower()
    suffix_backends = (
        "cuvid", "cuda", "qsv", "amf", "rkmpp", "omx", 
        "v4l2m2m", "nvmpi", "mmal"
    )
    if any(b in hb for b in suffix_backends):
        if "cuda" in hb or "nvdec" in hb:
            return f"{target}_cuvid"
        return f"{target}_{hb}"
    return target


def _get_platform_backends(accel_pref: str) -> list[str]:
    backends = [accel_pref]
    for b in HWACCELS:
        if b not in backends: backends.append(b)
    if WINDOWS or MSYS:
        candidates = ["cuvid", "d3d11va", "dxva2", "d3d12va", "qsv", "amf", "vulkan"]
    elif MAC:
        candidates = ["videotoolbox"] #, "vulkan"]
    elif LINUX or WSL:
        candidates = ["cuvid", "vaapi", "vdpau", "qsv", "nvmpi", "vulkan"]
    elif IS_PI:
        candidates = ["rkmpp", "v4l2m2m", "nvmpi", "mmal", "omx", "vaapi", "vulkan"]
    elif SOLARIS or AIX or HPUX:
        candidates = ["vaapi", "vdpau", "vulkan"]
    else:
        candidates = [
            "candidates", "vaapi", "vdpau", "qsv", "d3d11va", "dxva2",
            "d3d12va", "amf", "rkmpp", "v4l2m2m", "mmal", "omx","vulkan"
        ]
    for b in candidates:
        if b not in backends: backends.append(b)
    return backends

HWACCEL_UNSAFE_FOR_OPEN = {"qsv", "vulkan", "drm"}

class VideoWorker(QThread):
    img_sig = Signal()
    stat_sig = Signal(str) # (text0
    sz_sig = Signal(int, int) # (w, h)
    audio_sig = Signal(bytes, int, int)   # (pcm_bytes, sample_rate, channels)
    exit_sig = Signal()

    def __init__(self, url, fps_disp, opengl, accel, headless, ffmpeg):
        super().__init__()
        self.url = url
        self.fps_disp = fps_disp
        self.opengl = opengl
        self.accel = accel
        self.headless = headless

        _h, _o, _d, _ev, _ea = FFmpegParser.parse(ffmpeg)
        self.hw_opts = _h
        self.open_opts = _o
        self.dec_opts = _d
        self.enc_v_opts = _ev
        self.enc_a_opts = _ea

        self.fps = 0
        self.w_vid = 0
        self.h_vid = 0
        self.sample_rate = 0
        self.channels = 0
        self.audio_q = queue.Queue(maxsize=10)
        self.rec_q = queue.Queue(maxsize=200)
        self.stream_q = queue.Queue(maxsize=200)
        self.frame_q = deque(maxlen=1)  #queue.Queue(maxsize=5)
        self.clock = Clock()
        self.rec_on = False
        self.stream_on = False
        self.run_flag = True
        self.gpu_idx = _pick_gpu_index()
        self.codec_name = "h264"

    def width(self): return self.w_vid
    def height(self): return self.h_vid
    def ratio(self): return self.w_vid / self.h_vid if self.h_vid else 1.0

    def get_fps(self): return self.fps
    def get_sample_rate(self): return self.sample_rate
    def get_channels(self): return self.channels
    def get_queues(self): return self.audio_q, self.rec_q, self.stream_q, self.frame_q
    def set_rec_on(self, v: bool): self.rec_on = bool(v)
    def set_stream_on(self, v: bool): self.stream_on = bool(v)
    def get_gpu_idx(self): return self.gpu_idx
    def get_stream_codec(self): return self.codec_name
    def get_encoder_opts(self): return self.enc_v_opts, self.enc_a_opts
    def get_open_opts(self): return self.open_opts


    def run(self):
        logging.debug(f"[VideoWorker] started")
        src = self.url
        u_disp = src if src.isdigit() else src.split("@")[-1]

        if self.accel and self.accel not in HWACCELS:
            logging.critical(f"[VideoWorker] ❌ '{self.accel}' not available. Using '{HWACCELS}' instead.")
            self.run_flag = False
            self.exit_sig.emit()

        while self.run_flag:
            self.stat_sig.emit(f"Connecting...\n{u_disp}")

            try:
                open_opts = _build_conn_options(self.url)
                open_opts.update(self.open_opts)
                if self.accel and self.accel not in HWACCEL_UNSAFE_FOR_OPEN:
                    logging.verbose(f"[VideoWorker] Requested --accel={self.accel}")

                    logging.debug(f"[VideoWorker] Using hwaccel options: {self.hw_opts}")
                    hwaccel_obj = HWAccel(
                            device_type=self.accel,
                            options=self.hw_opts,
                            allow_software_fallback=False
                    )
                    logging.debug(f"[VideoWorker] Using av.open options: {open_opts}")
                    container = av.open(src, hwaccel=hwaccel_obj, options=open_opts)
                    logging.verbose(f"[VideoWorker] 🚀 Success! Using --accel={self.accel}")
                else:
                    logging.debug(f"[VideoWorker] Opening without hwaccel (will force decoder later): {self.accel or 'software'}")
                    logging.debug(f"[VideoWorker] Using av.open options: {open_opts}")
                    container = av.open(src, options=open_opts)
            except (av.error.TimeoutError, av.error.OSError) as e:
                errstr = _short_err(e)
                self.stat_sig.emit(f"Failed:\n{errstr}")
                #stack_trace = traceback.format_exc()
                #logging.error(f"[VideoWorker] Exception occurred: {e}, Traceback: {stack_trace}")
                self.clock.sleep(10000)
                continue
            except (av.error.InvalidDataError, RuntimeError) as e:
                errstr = _short_err(e)
                self.stat_sig.emit(f"Failed:\n{errstr}")
                stack_trace = traceback.format_exc()
                logging.error(f"[VideoWorker] Exception occurred: {e}")
                logging.trace(f"[VideoWorker] Exception Traceback: {stack_trace}")
                if "Hardware" in errstr:
                    self.run_flag = False
                    self.exit_sig.emit()
                else:
                    self.clock.sleep(10000)
                continue
            except Exception as e:
                errstr = _short_err(e)
                self.stat_sig.emit(f"Failed:\n{errstr}")
                stack_trace = traceback.format_exc()
                logging.error(f"[VideoWorker] Exception occurred: {e}")
                logging.trace(f"[VideoWorker] Exception Traceback: {stack_trace}")
                self.clock.sleep(10000)
                continue

            if not self.run_flag: continue # exit if initializeGL() failed

            video_stream = next((s for s in container.streams if s.type == "video"), None)
            audio_stream = next((s for s in container.streams if s.type == "audio"), None)

            if video_stream is None:
                self.stat_sig.emit("No video stream")
                container.close()
                self.clock.sleep(10000)
                continue

            if self.accel:
                raw_codec_name = video_stream.codec.name
                friendly = get_friendly_codec_name(raw_codec_name)
                logging.debug(f"[VideoWorker] Detected stream codec: '{raw_codec_name}', {friendly}")

                requested_decoder = _choose_decoder(self.accel, raw_codec_name)
                fallback_backends = _get_platform_backends(self.accel)
                search_list = [requested_decoder]
                for b in fallback_backends:
                    alt = _choose_decoder(b, raw_codec_name)
                    if alt not in search_list:
                        search_list.append(alt)
                #if raw_codec_name no in search_list: # test only
                #    search_list.append(raw_codec_name)
                context = None
                for d_name in search_list:
                    ctx = None
                    try:
                        logging.debug(f"[VideoWorker] testing decoder: {d_name}")
                        codec = av.Codec(d_name, 'r')
                        ctx = av.codec.context.CodecContext.create(codec)

                        ctx.thread_type = "NONE"
                        ctx.width = video_stream.codec_context.width
                        ctx.height = video_stream.codec_context.height
                        if video_stream.codec_context.extradata:
                            ctx.extradata = video_stream.codec_context.extradata

                        if "cuvid" in d_name:
                            ctx.options['gpu'] = str(self.gpu_idx)
                        elif "qsv" in d_name:
                            ctx.options['qsv_device'] = f"/dev/dri/renderD{128 + self.gpu_idx}"
                            if WINDOWS:
                                ctx.options['qsv_device'] = str(self.gpu_idx)
                                #ctx.options["extra_hw_frames"] = "64"
                            #ctx.options['hwaccel_output_format'] = "qsv"
                            #ctx.options['init_hw_device'] = "qsv:hw,child_device_type=dxva2"
                        elif "vaapi" in d_name:
                            ctx.options['vaapi_device'] = f"/dev/dri/renderD{128 + self.gpu_idx}"
                        elif "videotoolbox" in d_name:
                            ctx.options['hwaccel_device'] = str(self.gpu_idx)
                        elif "vulkan" in d_name:
                            ctx.options['hwaccel_device'] = str(self.gpu_idx)
                            #ctx.options['extra_hw_frames'] = '16'
                        elif "rkmpp" in d_name:
                            ctx.options['hwaccel_device'] = str(self.gpu_idx)

                        ctx.options.update(self.dec_opts)
                        logging.debug(f"[VideoWorker] Applied Decoder options for {d_name}: {ctx.options}")

                        ctx.open()
                        context = ctx
                        logging.verbose(f"[VideoWorker] 🚀 Success! Using {d_name}")
                        break
                    except Exception as e:
                        if ctx is not None: del ctx
                        errstr = str(e)
                        if d_name != errstr:
                            logging.debug(f"[VideoWorker] {d_name} check failed: {e}")
                        continue

                if not context:
                    logging.warning(f"[VideoWorker] ❌ All HW decoders failed. Using software.")
                    context = video_stream.codec_context
                    context.options.update(self.dec_opts)
                    logging.debug(f"[VideoWorker] Applied Decoder options for {context.name}: {context.options}")

                #context = video_stream.codec_context # test only

                logging.verbose(f"[VideoWorker] Final Decoder: {context.codec.name}")

            else:
                context = video_stream.codec_context
                context.options.update(self.dec_opts)
                logging.debug(f"[VideoWorker] Applied Decoder options for {context.codec.name}: {context.options}")

            audio_context = audio_stream.codec_context if audio_stream else None

            if video_stream.average_rate is not None:
                try:
                    fps_cap = int(float(video_stream.average_rate))
                except Exception:
                    fps_cap = 0
            else:
                fps_cap = 0
            if fps_cap <= 0:
                fps_cap = self.fps_disp
                logging.warning("[VideoWorker] can not get video fps. default: {fps_cap} fps")
            self.fps = fps_cap

            sk = Skipper(maxfps=self.fps_disp, o_fps=fps_cap)
            fr = FrameRator()
            logging.verbose(f"[VideoWorker] fps_disp:{self.fps_disp}, fps_cap:{fps_cap}")

            self.w_vid, self.h_vid = 0, 0

            streams = [video_stream]
            if audio_stream is not None:
                streams.append(audio_stream)

            try:
                first_frame_logged = False
                pix_fmt = None
                keyframe_count = 0
                for packet in container.demux(*streams):
                    if not self.run_flag: break
                    if packet.dts is None: continue

                    if packet.stream.type == 'video':
                        #logging.debug(f"video packet")
                        #if not packet.is_keyframe:
                        #    continue
                        #    logging.debug("!!! KEYFRAME DETECTED !!!")
                        if packet.is_keyframe: keyframe_count += 1
                        if not first_frame_logged and keyframe_count > 3:
                            logging.warning(f"[VideoWorker] HW decoding {context.codec.name} failed, no frame. [{u_disp}]")
                            self.accel = None # 다음 재연결 시 SW로 작동하게 유도
                            break # 루프 탈출 후 재접속
                        frames = context.decode(packet)
                        #if not frames and packet.is_keyframe:
                        #    frames = context.decode(None) 
                        #    logging.debug("Waiting for QSV internal buffer...")
                        for frame in frames:
                            #logging.debug(f"video frame:{frame}")

                            if not first_frame_logged:
                                pix_fmt = frame.format.name
                                self.codec_name = context.codec.name
                                logging.info(f"[VideoWorker] First frame arrived({self.codec_name}). pix-fmt: {frame.format.name} ({frame.width},{frame.height})")
                                first_frame_logged = True
                            w, h = frame.width, frame.height
                            if w <= 0 or h <= 0: continue

                            if w != self.w_vid or h != self.h_vid:
                                self.w_vid, self.h_vid = w, h
                                self.sz_sig.emit(w, h)
                                logging.verbose(f"[VideoWorker] emit video width:{w}, height:{h}")

                            if self.rec_on:
                                try:
                                    arr = frame.to_ndarray()
                                    cpu_frame = av.VideoFrame.from_ndarray(arr, format=frame.format.name)
                                    _queue_nowait_put(self.rec_q, (1, cpu_frame))
                                except Exception as e:
                                    logging.error(f"Failed to copy frame to CPU for recording: {e}")

                            if self.stream_on:
                                try:
                                    arr = frame.to_ndarray()
                                    cpu_frame = av.VideoFrame.from_ndarray(arr, format=frame.format.name)
                                    _queue_nowait_put(self.stream_q, (1, cpu_frame))
                                except Exception as e:
                                    logging.error(f"Failed to copy frame to CPU for streaming: {e}")

                            if self.headless: continue

                            sk.update(fr.get_fps())
                            if sk.is_skip() and not frame.key_frame: continue

                            if pix_fmt == "nv12" and self.opengl:
                                y_plane = np.frombuffer(frame.planes[0], dtype=np.uint8).reshape(h, frame.planes[0].line_size)
                                if frame.planes[0].line_size > w: y_plane = y_plane[:, :w]
                                uv_h = h // 2
                                uv_line_size = frame.planes[1].line_size
                                uv_plane = np.frombuffer(frame.planes[1], dtype=np.uint8).reshape(uv_h, uv_line_size // 2, 2)
                                if (uv_line_size // 2) > (w // 2): uv_plane = uv_plane[:, :w // 2, :]
                                self.frame_q.append( ("nv12", (y_plane, uv_plane, w, h)) )

                            else: # h264, yuvj420p
                                rgba = frame.to_ndarray(format="rgba")
                                #print(f"Mean value: {rgba.mean()}")
                                #qimg = QImage(rgba.data, w, h, QImage.Format_RGBA8888)
                                #qimg = QImage(rgba.data, w, h, QImage.Format_RGBA8888).copy()
                                h, w, c = rgba.shape
                                qimg = QImage(rgba.data, w, h, w * 4, QImage.Format_RGBA8888).copy()
                                self.frame_q.append( ("rgba", qimg) )

                            #else: # rgb
                            #    rgb = frame.to_ndarray(format="rgb24")
                            #    qimg = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()
                            #    self.frame_q.append( ("rgb", (qimg)) )

                            self.img_sig.emit()


                    elif packet.stream.type == 'audio':
                        #frames = context.decode(packet)
                        if not audio_context: continue
                        frames = audio_context.decode(packet)
                        for frame in frames:
                            sr = frame.sample_rate or (audio_stream.rate if audio_stream else 48000)
                            self.sample_rate = sr

                            layout = frame.layout
                            ch = layout.channels if layout is not None else (frame.channels or 1)

                            pcm = frame.to_ndarray()
                            if pcm.ndim == 1:
                                ch = 1
                            else:
                                ch = pcm.shape[0] # (C, N)

                            if pcm.dtype != np.int16:
                                pcm = np.clip(pcm, -1.0, 1.0)
                                pcm16 = (pcm * 32767.0).astype(np.int16)
                            else:
                                pcm16 = pcm

                            # (C, N) -> (N, C), interleaved bytes
                            if pcm16.ndim > 1:
                                pcm16 = pcm16.T # (samples, channels)
                            pcm_bytes = pcm16.tobytes()

                            self.channels = ch
                            _queue_nowait_put(self.audio_q, (pcm_bytes, sr, ch))

                            if self.rec_on or self.stream_on:
                                audio_copy = av.AudioFrame.from_ndarray(
                                    frame.to_ndarray(),
                                    format=frame.format.name,
                                    layout=frame.layout.name
                                )
                                audio_copy.sample_rate = frame.sample_rate
                                audio_copy.pts = frame.pts

                                if self.rec_on:
                                    _queue_nowait_put(self.rec_q, (2, audio_copy))
                                if self.stream_on:
                                    _queue_nowait_put(self.stream_q, (2, audio_copy))

                    else:
                        logging.debug(f"[VideoWorker] {packet}")

            except Exception as e:
                errstr = _short_err(e)
                self.stat_sig.emit(f"Stream error:\n{errstr}")
                stack_trace = traceback.format_exc()
                logging.error(f"[VideoWorker] Stream exception: {e}")
                logging.trace(f"[VideoWorker] Exception Traceback: {stack_trace}")
            finally:
                container.close()

            if self.run_flag:
                self.clock.sleep(1000)

    def stop(self):
        if self.run_flag:
            self.run_flag = False
            self.clock.wake()
            self.wait()
            logging.debug(f"[VideoWorker] stoped.")

