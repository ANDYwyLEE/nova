# audioworker.py

import queue
import platform
import os
import sys
import contextlib
import logging

import numpy as np
from PySide6.QtCore import QThread, Signal
from util import LINUX, WINDOWS, MAC
from logsync import TRACE_LEVEL

try: import pyaudio
except ImportError:
    print("Error: PyAudio is required. Run 'pip install PyAudio'")
    sys.exit(1)


TARGET_SR = 48000  # safe default for most backends

@contextlib.contextmanager
def quiet_alsa():
    """Temporarily hide ALSA/JACK noise on Linux."""
    if not LINUX:
        yield
        return

    try:
        old_fd = os.dup(2)          # stderr
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 2)
        os.close(devnull)
    except OSError:
        # If something goes wrong, just don't crash
        yield
        return

    try:
        yield
    finally:
        try:
            os.dup2(old_fd, 2)
            os.close(old_fd)
        except OSError:
            pass


def _list_audio_devices(pa: pyaudio.PyAudio):
    logging.trace(f"[AudioWorker] _list_audio_device")
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        name = info.get("name", "").lower()
        logging.trace(f"[AudioWorker] audio device: {i}, {name}")
    env_dev = os.environ.get("NOVA_AUDIO_DEVICE_INDEX")
    idxs = '' if env_dev is None else env_dev
    logging.trace(f"[AudioWorker] NOVA_AUDIO_DEVICE_INDEX={idxs}")


# Linux only
def find_preferred_device(pa: pyaudio.PyAudio) -> int | None:
    """Prefer PipeWire / Pulse / default-like devices on Linux."""
    logging.trace(f"[AudioWorker] find_preferred_device")
    dev_id, dev_name = 0, None
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        name = info.get("name", "").lower()

        # Very rough preference rules
        if any(key in name for key in ("pipewire", "pulse")):
            logging.debug(f"[AudioWorker] audio device selected: {i}, {name}")
            return i
        if "default" in name:
            dev_id, dev_name = i, name
    logging.debug(f"[AudioWorker] audio device selected: {dev_idx}, {dev_name}")
    return dev_id


def resample(data: bytes, src_sr: int, dst_sr: int, ch: int) -> bytes:
    """Simple linear resample int16 interleaved PCM."""
    if src_sr == dst_sr:
        return data

    pcm = np.frombuffer(data, dtype=np.int16)
    if ch <= 0:
        return data

    pcm = pcm.reshape(-1, ch)  # (frames, channels)

    ratio = dst_sr / float(src_sr)
    dst_len = int(round(pcm.shape[0] * ratio))
    if dst_len <= 0:
        return data

    x_src = np.arange(pcm.shape[0], dtype=np.float32)
    x_dst = np.linspace(0, pcm.shape[0] - 1, dst_len, dtype=np.float32)

    out = np.empty((dst_len, ch), dtype=np.int16)
    for c in range(ch):
        out[:, c] = np.interp(x_dst, x_src, pcm[:, c]).astype(np.int16)

    return out.tobytes()


class AudioWorker(QThread):
    """Cross-platform audio playback worker using PyAudio."""

    stat_sig = Signal(str)

    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q
        self.run_flag = True

        self.pa = None
        self.stream = None

        self.sr = None
        self.ch = None
        self.do_resample = False

        # init PyAudio with ALSA noise muted on Linux
        if logging.getLogger().getEffectiveLevel() <= TRACE_LEVEL:
            self.pa = pyaudio.PyAudio()
            _list_audio_devices(self.pa)
        else:
            with quiet_alsa(): self.pa = pyaudio.PyAudio()


    def _close_stream(self):
        if self.stream is not None:
            try:
                self.stream.stop_stream()
            except Exception:
                pass
            try:
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def _open_stream(self, src_sr: int, ch: int):
        self._close_stream()

        # defaults
        dev_idx = None
        rate = src_sr
        self.do_resample = False

        # allow override from env
        env_dev = os.environ.get("NOVA_AUDIO_DEVICE_INDEX")
        if env_dev is not None:
            try:
                dev_idx = int(env_dev)
            except ValueError:
                dev_idx = None
        # Linux: prefer PipeWire / Pulse / default device
        elif LINUX:
            dev_idx = find_preferred_device(self.pa)

        # Main open logic with simple fallbacks
        tried_fallback = False

        while True:
            try:
                logging.debug(f"[AudioWorker] Opening audio stream: rate={rate}, ch={ch}, dev={dev_idx}")
                self.stream = self.pa.open(
                    format=pyaudio.paInt16,
                    channels=ch,
                    rate=rate,
                    output=True,
                    output_device_index=dev_idx,
                    frames_per_buffer=1024,
                )
                self.sr = rate
                self.ch = ch
                self.stat_sig.emit(f"Audio stream opened ({rate} Hz, {ch} ch)")
                logging.info(f"[AudioWorker] Audio stream opened ({rate} Hz, {ch} ch)")
                return
            except Exception as e:
                logging.error(f"[AudioWorker] Audio open failed: {e}")

                if not tried_fallback:
                    # fallback: force TARGET_SR and enable resample
                    rate = TARGET_SR
                    self.do_resample = True
                    tried_fallback = True
                    continue

                # second fallback: drop device index and try default
                if dev_idx is not None:
                    dev_idx = None
                    tried_fallback = True
                    continue

                # give up
                self.stat_sig.emit(f"Audio open error: {e}")
                raise

    def run(self):
        self.stat_sig.emit("Audio started")
        logging.verbose("[AudioWorker] started")

        while self.run_flag:
            try:
                data, sr, ch = self.q.get(timeout=0.1)
            except queue.Empty:
                continue

            if ch <= 0 or sr <= 0 or not data:
                continue


            # (re)open stream if params changed
            if (
                self.stream is None
                or ch != self.ch
                or (not self.do_resample and sr != self.sr)
            ):
                try:
                    self._open_stream(sr, ch)
                except Exception as e:
                    # drop this buffer and keep trying next ones
                    self.stat_sig.emit(f"Audio stream open error: {e}")
                    logging.error(f"[AudioWorker] stream open error: {e}")
                    continue

            out = data
            if self.do_resample and self.sr is not None and self.sr != sr:
                out = resample(data, sr, self.sr, ch)

            try:
                self.stream.write(out)
            except Exception as e:
                logging.debug(f"[AudioWorker] write error: {e}")
                self.stat_sig.emit(f"Audio write error: {e}")
                # force reopen next time
                self._close_stream()

        self._close_stream()
        if self.pa is not None:
            try: self.pa.terminate()
            except Exception:
                pass
            self.pa = None

        self.stat_sig.emit("Audio stopped")
        logging.verbose("[AudioWorker] stopped")

    def stop(self):
        self.run_flag = False
        self.wait()
