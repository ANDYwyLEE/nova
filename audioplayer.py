import queue
from player import Player  # Import the abstract base class
from audioworker import AudioWorker
from PySide6.QtCore import Signal

class AudioPlayer(Player):
    stat_sig = Signal(bool)

    def __init__(self, q: queue.Queue, vid_worker=None):
        super().__init__('AudioPlayer', 'snd.png')

        self.audio_q = q
        self.vid_worker = vid_worker   # tells the decoder when PCM conversion is actually needed
        self.worker = None # AudioWorker

    #@abstractmethod
    def available(self):
        return True

    #@abstractmethod
    def is_on(self):
        return self.worker is not None

    #@abstractmethod
    def is_off(self):
        return not self.is_on()

    #@abstractmethod
    def on(self):
        if self.is_on(): return
        self._drain(self.audio_q)
        if self.vid_worker: self.vid_worker.set_audio_on(True)
        self.worker = AudioWorker(self.audio_q)
        self.worker.start()
        self.stat_sig.emit(True)

    #@abstractmethod
    def off(self):
        if self.is_off(): return
        if self.vid_worker: self.vid_worker.set_audio_on(False)
        self.worker.stop()
        self.worker = None
        self.stat_sig.emit(False)

    #@abstractmethod
    def close(self):
        self.off()

    # Note super().get_icon() - the get_icon method from the base class can be used directly
