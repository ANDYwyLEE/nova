# logsync.py
import logging
import os
import sys
import threading
import traceback
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


class GlobalSingleWarningFilter(logging.Filter):
    def __init__(self, targets):
        super().__init__()
        self.targets = targets
        self.shown_keys = set()

    def filter(self, record):
        msg = record.getMessage()
        for target in self.targets:
            if target in msg:
                if target in self.shown_keys:
                    return False
                self.shown_keys.add(target)
                return True
        return True

def setup_av_logging():
    # Map Python logging levels to PyAV (libav) logging levels
    # PyAV levels: PANIC(0), FATAL(8), ERROR(16), WARNING(32), INFO(40), VERBOSE(48), DEBUG(56), TRACE(64)
    LOG_LEVEL_MAP = {
        logging.CRITICAL: av.logging.PANIC,    # 50 -> 0
        logging.ERROR:    av.logging.ERROR,    # 40 -> 16
        logging.WARNING:  av.logging.WARNING,  # 30 -> 32
        logging.INFO:     av.logging.INFO,     # 20 -> 40
        VERBOSE_LEVEL:    av.logging.VERBOSE,  # 15 (VERBO) -> 48
        logging.DEBUG:    av.logging.DEBUG,    # 10 -> 56
        TRACE_LEVEL:      av.logging.TRACE,    # 5  (TRACE) -> 64
    }

    targets = [
        "deprecated pixel format used",
        "Could not find ref with POC",
        "Error constructing the frame RPS",
        "Skipping invalid undecodable NALU",
        "More data is required to decode header",
        "Invalid number of merging MVP candidates",
        "(Coded slice of a non-IDR picture)",
        "(IDR), nal_ref_idc",
        "overread 8",
        # joining a live stream mid-GOP: libavcodec complains until the first keyframe arrives
        "non-existing PPS",
        "non-existing SPS",
        "no frame!",
        "decode_slice_header error",
        "Invalid pkt_timebase",
        # libavfilter: the buffer source template carries no colour range; harmless
        "Changing video frame properties on the fly",
        "filter context - w:",
    ]

    root_logger = logging.getLogger()
    current_level = root_logger.getEffectiveLevel()

    # Synchronize PyAV log level with the system log level
    # If the level is 5 or below, it will naturally map to av.logging.TRACE (64)
    av_level = LOG_LEVEL_MAP.get(current_level, av.logging.WARNING)

    # Manage GlobalSingleWarningFilter to prevent duplicate log messages
    for f in root_logger.filters[:]:
        if isinstance(f, GlobalSingleWarningFilter):
            root_logger.removeFilter(f)

    global_filter = GlobalSingleWarningFilter(targets)
    root_logger.addFilter(global_filter)

    # handler filters see every record, including those propagated from child loggers (libav, ...)
    mask_filter = CredentialMaskFilter()
    for handler in root_logger.handlers:
        handler.addFilter(global_filter)
        if not any(isinstance(f, CredentialMaskFilter) for f in handler.filters):
            handler.addFilter(mask_filter)

    # Apply the synchronized log level to PyAV
    av.logging.set_level(av_level)

    libav_logger = logging.getLogger('libav')
    libav_logger.addFilter(global_filter)
    libav_logger.setLevel(current_level)

    # Ensure logs are passed to the root handler
    libav_logger.propagate = True

    logging.debug(f"[LogSync] Global filter active. PyAV level: {av_level}")
    logging.debug(f"[LogSync] PyAV log level synchronized to {av_level} (SysLevel: {current_level})")
