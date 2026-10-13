# recworker.py
import logging
import queue
import traceback
import av
from PySide6.QtCore import QThread, Signal
from avwriter import OutputOpenError

class AVRecWorker(QThread):
    """Drains an (typ, frame) queue into an AVRecorder/AVStreamer.

    The owner's writer is opened and closed on *this* thread so that slow operations
    (connecting to an RTMP server, initialising a GPU encoder) never block the GUI."""
    exit_sig = Signal(str)
    MAX_CONSECUTIVE_ERRORS = 60

    def __init__(self, owner, av_q):
        super().__init__()
        self.owner = owner
        self.q = av_q
        self.run_flag = True

    def run(self):
        name = getattr(self.owner, "name", "AVRecWorker")
        logging.verbose(f"[{name}] worker started")
        try:
            self.owner._open_writer()
        except Exception as e:
            logging.error(f"[{name}] failed to start: {e}\n{traceback.format_exc()}")
            self.exit_sig.emit(str(e))
            return

        errors = 0
        failure = None
        while self.run_flag:
            try:
                typ, frame = self.q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self.owner.write(typ, frame)
                errors = 0
            except OutputOpenError as e:
                logging.error(f"[{name}] {e}")
                failure = str(e)
                break
            except av.error.ExitError as e:
                # a network write ran past its deadline: the connection is stalled and the
                # half-written packet has broken the stream, so retrying cannot help
                logging.error(f"[{name}] write timed out, giving up: {e}")
                failure = f"write timed out: {e}"
                break
            except Exception as e:
                errors += 1
                if errors == 1:
                    logging.error(f"[{name}] write failed: {e}\n{traceback.format_exc()}")
                elif errors % 20 == 0:
                    logging.error(f"[{name}] write still failing ({errors}x): {e}")
                if errors >= self.MAX_CONSECUTIVE_ERRORS:
                    logging.error(f"[{name}] giving up after {errors} consecutive errors")
                    failure = str(e)
                    break

        try:
            self.owner._close_writer()
        except Exception as e:
            logging.error(f"[{name}] close failed: {e}\n{traceback.format_exc()}")
        logging.verbose(f"[{name}] worker stopped")
        # only now: the owner's handler waits for this thread, which would otherwise stall the GUI
        # for as long as closing a dead connection takes
        if failure is not None:
            self.exit_sig.emit(failure)

    def stop(self):
        self.run_flag = False
