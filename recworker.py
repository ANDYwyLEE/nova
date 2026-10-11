import logging
import queue
from PySide6.QtCore import QThread, Signal

class AVRecWorker(QThread):
    exit_sig = Signal(str)

    def __init__(self, recorder, av_q):
        super().__init__()
        self.rec = recorder
        self.q = av_q
        self.run_flag = True

    def run(self):
        logging.verbose(f"[AVRecWorker] started")
        while self.run_flag:
            try:
                typ, frame = self.q.get(timeout=0.1)
                self.rec.write(typ, frame)
            except queue.Empty:
                pass
            except Exception as e:
                errstr = str(e)
                self.run_flag = False
                self.exit_sig.emit(errstr)
                break

    def stop(self):
        self.run_flag = False
        logging.verbose(f"[AVRecWorker] stoped")

