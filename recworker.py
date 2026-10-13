# recworker.py
import logging
import queue
import re
import time
import traceback
import av
from PySide6.QtCore import QThread, Signal
from avwriter import OutputOpenError, _short_err

RETRY_DELAYS = (5, 10, 20, 40, 60) # s between restarts of a failed recording/stream; the last one repeats
STABLE_AFTER = 60                  # s of output (from the first muxed packet) that start the series again


class WorkerRefused(Exception):
    """The owner cannot run now and says why itself (e.g. another process records this camera)."""


class ErrorLog:
    """Write errors per stream type: the first of each kind with its traceback, then at most one summary
    line per SUMMARY_S. The owner keeps it across restarts, so a lasting cause (a -vf filter missing from
    the FFmpeg build, a full disk) does not log a traceback per frame, nor one per retry."""
    SUMMARY_S = 30
    MAX_KINDS = 64

    def __init__(self, name):
        self.name = name
        self.seen = set()
        self.since = {}     # what -> failures not logged yet
        self.last = {}      # what -> monotonic time of the last line

    def report(self, what, e):
        key = (what, type(e).__name__, re.sub(r'\d+', '#', _short_err(e))[:200])
        now = time.monotonic()
        if key not in self.seen and len(self.seen) < self.MAX_KINDS:
            self.seen.add(key)
            logging.error(f"[{self.name}] {what} write failed: {e}\n{traceback.format_exc()}")
        else:
            self.since[what] = self.since.get(what, 0) + 1
            if now - self.last.get(what, 0) < self.SUMMARY_S:
                return
            logging.error(f"[{self.name}] {what} writes still failing ({self.since[what]}x since the "
                          f"last message): {e}")
        self.since[what] = 0
        self.last[what] = now


class AVRecWorker(QThread):
    """Drains an (typ, frame) queue into an AVRecorder/AVStreamer.

    The owner's writer is opened and closed on *this* thread so that slow operations
    (connecting to an RTMP server, initialising a GPU encoder) never block the GUI."""
    exit_sig = Signal(str)
    MAX_CONSECUTIVE_ERRORS = 60     # per stream type: video failing while audio works is a failure too

    def __init__(self, owner, av_q):
        super().__init__()
        self.owner = owner
        self.q = av_q
        self.run_flag = True
        self.failure = None         # why the worker gave up; set before exit_sig
        self.ran = 0.0              # s from the first muxed packet to the failure

    def run(self):
        name = getattr(self.owner, "name", "AVRecWorker")
        errlog = getattr(self.owner, "errlog", None) or ErrorLog(name)
        logging.verbose(f"[{name}] worker started")
        try:
            self.owner._open_writer()
        except WorkerRefused as e:
            self._fail(str(e))
            return
        except Exception as e:
            logging.error(f"[{name}] failed to start: {e}\n{traceback.format_exc()}")
            self._fail(str(e))
            return

        errors = {}
        failure = None
        live_since = None
        while self.run_flag:
            try:
                typ, frame = self.q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self.owner.write(typ, frame)
                errors[typ] = 0
            except OutputOpenError as e:
                logging.error(f"[{name}] {e}")
                logging.debug(f"[{name}] {traceback.format_exc()}")
                failure = str(e)
                break
            except av.error.ExitError as e:
                # a network write ran past its deadline: the connection is stalled and the
                # half-written packet has broken the stream, so retrying cannot help
                logging.error(f"[{name}] write timed out, giving up: {e}")
                failure = f"write timed out: {e}"
                break
            except Exception as e:
                what = "video" if typ == 1 else "audio"
                errors[typ] = errors.get(typ, 0) + 1
                errlog.report(what, e)
                if errors[typ] >= self.MAX_CONSECUTIVE_ERRORS:
                    logging.error(f"[{name}] giving up after {errors[typ]} consecutive {what} errors")
                    failure = f"{what}: {e}"
                    break
                continue
            if live_since is None and getattr(self.owner, "_output_live", lambda: True)():
                live_since = time.monotonic()

        if failure is not None:
            self.ran = time.monotonic() - live_since if live_since is not None else 0.0
        try:
            self.owner._close_writer()
        except Exception as e:
            logging.error(f"[{name}] close failed: {e}\n{traceback.format_exc()}")
        logging.verbose(f"[{name}] worker stopped")
        # only now: the owner's handler waits for this thread, which would otherwise stall the GUI
        # for as long as closing a dead connection takes
        if failure is not None:
            self._fail(failure)

    def _fail(self, failure):
        self.failure = failure
        self.exit_sig.emit(failure)

    def stop(self):
        self.run_flag = False
