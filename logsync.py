# logsync.py
import logging
import os
import sys
import threading
import traceback


try:
    import av
    from av.codec.hwaccel import HWAccel, hwdevices_available
except ImportError as e:
    stack_trace = traceback.format_exc()
    print(r"Error: av is required. Run 'pip install av'(>=12.x.x)")
    sys.exit(1)




# Define custom logging levels
#logging.CRITICAL = 50
#logging.ERROR = 40
#logging.WARNING = 30
#logging.INFO = 20
VERBOSE_LEVEL = 15
#logging.DEBUG = 10
TRACE_LEVEL = 5

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

    for handler in root_logger.handlers:
        handler.addFilter(global_filter)

    # Apply the synchronized log level to PyAV
    av.logging.set_level(av_level)

    libav_logger = logging.getLogger('libav')
    libav_logger.addFilter(global_filter)
    libav_logger.setLevel(current_level)

    # Ensure logs are passed to the root handler
    libav_logger.propagate = True

    logging.debug(f"[LogSync] Global filter active. PyAV level: {av_level}")
    logging.debug(f"[LogSync] PyAV log level synchronized to {av_level} (SysLevel: {current_level})")
