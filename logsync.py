# logsync.py
import logging
import os
import sys
import threading
import traceback
import contextlib
from util import mask_url


try:
    import av
    from av.codec.hwaccel import HWAccel, hwdevices_available
except ImportError as e:
    stack_trace = traceback.format_exc()
    print(r"Error: av is required. Run 'pip install av'(>=14.x.x)")
    sys.exit(1)


# Define custom logging levels
#logging.CRITICAL = 50
#logging.ERROR = 40
#logging.WARNING = 30
#logging.INFO = 20
VERBOSE_LEVEL = 15
#logging.DEBUG = 10
TRACE_LEVEL = 5


def install_custom_levels():
    """Add logging.verbose()/logging.trace() (idempotent). Imported by every module via logsync."""
    if getattr(logging, "_nova_levels_installed", False):
        return
    def verbose(self, message, *args, **kws):
        if self.isEnabledFor(VERBOSE_LEVEL): self._log(VERBOSE_LEVEL, message, args, **kws)
    def trace(self, message, *args, **kws):
        if self.isEnabledFor(TRACE_LEVEL): self._log(TRACE_LEVEL, message, args, **kws)
    logging.Logger.verbose = verbose
    logging.Logger.trace = trace
    logging.verbose = lambda msg, *args, **kwargs: logging.getLogger().verbose(msg, *args, **kwargs)
    logging.trace = lambda msg, *args, **kwargs: logging.getLogger().trace(msg, *args, **kwargs)
    logging.addLevelName(logging.WARNING, "WARN ")
    logging.addLevelName(logging.INFO, "INFO ")
    logging.addLevelName(TRACE_LEVEL, "TRACE")
    logging.addLevelName(VERBOSE_LEVEL, "VERBO")
    logging._nova_levels_installed = True

install_custom_levels()


class CredentialMaskFilter(logging.Filter):
    """Masks passwords and stream keys in every log line, whichever module wrote it: PyAV embeds the
    full URL in its exception texts, and those end up in error logs."""
    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        masked = mask_url(msg)
        if masked != msg:
            record.msg = masked
            record.args = ()
        if record.exc_info or record.exc_text:            # logging.exception(): the traceback quotes URLs too
            record.exc_text = mask_url(record.exc_text or logging.Formatter().formatException(record.exc_info))
        if record.stack_info:
            record.stack_info = mask_url(record.stack_info)
        return True


def install_excepthooks():
    """Uncaught exceptions (a Qt slot, a QThread's run(), a thread) are printed by Python straight
    to stderr, which the launcher redirects into logs/ID.log, past the masking filter. Route them
    through logging instead."""
    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logging.getLogger().error("Uncaught exception", exc_info=(exc_type, exc, tb))
    sys.excepthook = hook
    threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)


# ---------------------------------------------------------------- FFmpeg's log, through PyAV
# PyAV hands every FFmpeg line at or above a threshold to Python logging (logger "libav.<context>",
# e.g. libav.h264, libav.rtsp) with the level translated: PANIC/FATAL -> CRITICAL, ERROR -> ERROR,
# WARNING -> WARNING, INFO -> INFO, VERBOSE -> DEBUG (10), DEBUG -> 5, TRACE -> 1.  Below the
# threshold nothing is even formatted; above it every line costs a vsnprintf and the GIL (one line
# per NAL unit at FFmpeg DEBUG).  The threshold is therefore the most verbose FFmpeg level whose
# lines the Nova level would still show.  PyAV collapses identical consecutive lines by itself
# ("... (repeated N more times)").
AV_THRESHOLDS = (                              # (Nova level, FFmpeg threshold), most severe first
    (logging.CRITICAL, av.logging.FATAL),
    (logging.ERROR, av.logging.ERROR),
    (logging.WARNING, av.logging.WARNING),
    (logging.INFO, av.logging.INFO),
    (VERBOSE_LEVEL, av.logging.INFO),          # FFmpeg VERBOSE arrives as Python DEBUG: hidden at 15 anyway
    (logging.DEBUG, av.logging.VERBOSE),
    (TRACE_LEVEL, av.logging.DEBUG),           # FFmpeg TRACE (bit level) only through NOVA_AV_LOG
)
AV_LEVEL_NAMES = {"quiet": None, "panic": av.logging.PANIC, "fatal": av.logging.FATAL, "error": av.logging.ERROR,
                  "warning": av.logging.WARNING, "info": av.logging.INFO, "verbose": av.logging.VERBOSE,
                  "debug": av.logging.DEBUG, "trace": av.logging.TRACE}


def av_threshold(level):
    """The FFmpeg threshold for a Nova log level (any number: 25 is treated like WARNING)."""
    chosen = av.logging.FATAL
    for nova_level, threshold in AV_THRESHOLDS:
        if level <= nova_level:
            chosen = threshold
    return chosen


DEMOTE, DROP = "demote", "drop"
# FFmpeg lines Nova knows to be noise for a live camera viewer: demoted to DEBUG (still there with
# -l DEBUG) or dropped.  Each with its source file and FFmpeg level, so the list can be checked
# against a new FFmpeg.  What matters is reported by Nova itself: packets that keep failing to
# decode, a stream that stops, an encoder that does not open.
AV_NOISE = (
    # joining a live H.264 stream between two keyframes: errors until the first keyframe arrives
    # (h264_slice.c, h264dec.c, h264_sei.c; AV_LOG_ERROR)
    ("non-existing PPS", DEMOTE),
    ("non-existing SPS", DEMOTE),
    ("no frame!", DEMOTE),
    ("decode_slice_header error", DEMOTE),
    # the same for H.265 (hevc/refs.c, hevc/hevcdec.c; AV_LOG_ERROR)
    ("Could not find ref with POC", DEMOTE),
    ("Error constructing the frame RPS", DEMOTE),
    ("Invalid number of merging MVP candidates", DEMOTE),
    # cameras put private NAL units into H.265 streams (hevc/hevcdec.c; AV_LOG_WARNING, every packet)
    ("Skipping invalid undecodable NALU", DEMOTE),
    # camera SEI padded with extra bits (h264_sei.c; AV_LOG_WARNING, every keyframe)
    ("overread by", DEMOTE),
    # the AAC encoder's statistics when it closes (aacenc.c; AV_LOG_INFO)
    ("Qavg:", DEMOTE),
    # FLV to an RTMP server cannot rewrite its header on close; a file could (flvenc.c; AV_LOG_WARNING)
    ("Failed to update header with correct", DEMOTE),
    # one line per NAL unit (h2645_parse.c; AV_LOG_DEBUG): 30+ a second per camera at -l TRACE
    ("nal_unit_type:", DROP),
)


class AVNoiseFilter(logging.Filter):
    """AV_NOISE applied to PyAV's libav.* records.  A handler filter: a filter on the "libav" logger
    never sees the records of its children (libav.h264, ...)."""
    def filter(self, record):
        if not record.name.startswith("libav"):
            return True
        msg = record.getMessage()
        for needle, action in AV_NOISE:
            if needle in msg:
                if action == DROP:
                    return False
                if record.levelno > logging.DEBUG:
                    record.levelno = logging.DEBUG
                    record.levelname = logging.getLevelName(logging.DEBUG)
                return logging.getLogger().isEnabledFor(logging.DEBUG)
        return True


@contextlib.contextmanager
def av_quiet(level=av.logging.FATAL):
    """FFmpeg's log held at `level` for a block: probes that are expected to fail, throw-away
    contexts.  The threshold is global, so other threads' FFmpeg lines are held too, briefly."""
    old = av.logging.get_level()
    av.logging.set_level(level)
    try:
        yield
    finally:
        av.logging.set_level(old)


def setup_av_logging():
    """FFmpeg's log follows -l: the threshold from AV_THRESHOLDS, the libav logger at the Nova level
    (PyAV pins it to WARNING at import), AVNoiseFilter and CredentialMaskFilter on every root
    handler.  NOVA_AV_LOG=panic|fatal|error|warning|info|verbose|debug|trace|quiet sets the FFmpeg
    threshold directly and switches the noise rules off: FFmpeg raw, for a bug report."""
    root = logging.getLogger()
    level = root.getEffectiveLevel()
    raw = os.environ.get("NOVA_AV_LOG", "").strip().lower()
    if raw and raw not in AV_LEVEL_NAMES:
        logging.warning(f"[LogSync] ⚠️ NOVA_AV_LOG={raw!r} is not one of {' '.join(AV_LEVEL_NAMES)}; ignored")
        raw = ""
    threshold = AV_LEVEL_NAMES[raw] if raw else av_threshold(level)
    for handler in root.handlers:
        for f in handler.filters[:]:
            if isinstance(f, (AVNoiseFilter, CredentialMaskFilter)):
                handler.removeFilter(f)
        if not raw:
            handler.addFilter(AVNoiseFilter())
        handler.addFilter(CredentialMaskFilter())     # masks every record, PyAV's included
    av.logging.set_level(threshold)
    libav = logging.getLogger("libav")
    libav.setLevel(1 if raw else level)
    libav.propagate = True
    logging.debug(f"[LogSync] FFmpeg log threshold {threshold} for level {level}"
                  + (f" (NOVA_AV_LOG={raw}: raw)" if raw else ""))
