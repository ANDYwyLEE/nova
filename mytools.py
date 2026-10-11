import time
from collections import deque
from PySide6.QtCore import QMutex, QWaitCondition

# Clock (Onetime Non-Blocking Sleep Utility)
class Clock:
    def __init__(self):
        self.m = QMutex()
        self.c = QWaitCondition()
        self._abort = False

    def sleep(self, ms):
        if self._abort: return
        self.m.lock()
        self.c.wait(self.m, ms)
        self.m.unlock()

    def wake(self):
        self._abort = True
        self.c.wakeAll()



class FrameRator():
    def __init__(self, maxlen = 120):
        self.deck_q = deque(maxlen = maxlen)
        self.maxlen = maxlen

    def get_fps(self):
        t2 = time.time()
        cnt = len(self.deck_q)
        if cnt == 0:
            self.deck_q.append(t2)
            return 0

        t1 = self.deck_q[0]
        dt = t2 - t1
        if dt <= 0: return 0
        fps =  cnt / dt
        self.deck_q.append(t2)
        return fps



class Skipper():
    def __init__(self, maxfps=25, o_fps=120):
        self.maxfps = maxfps
        self.o_fps = o_fps
        self.indices = []
        self.index = 0
        self.t1 = 0
        self.update(self.o_fps)

    def update(self, o_fps):
        t2 = int(time.time())
        if self.t1 == t2:
            return
        self.t1 = t2
        self.indices.clear()
        self.o_fps = o_fps
        #print(f'[Skipper] fps:{o_fps:0.1f}')
        self.index = 0
        for i in range(self.maxfps):
            self.indices.append(round(self.o_fps / self.maxfps * i))
        #print(f'[Skipper] self.t1:{self.t1}, o_fps={o_fps}, self.indices: {self.indices}')

    def is_skip(self):
        b_skip = not self.index in self.indices
        self.index += 1
        if self.index >= self.o_fps:
            self.index = 0
        return b_skip




if __name__ == '__main__':
    #skipper = Skipper(25)
    #skipper = Skipper(0)
    skipper = Skipper(120)
    cnt1 = cnt2 = 0
    for i in range(240):
        b = skipper.is_skip()
        if b:
            cnt1 += 1
            print(f'{i} : ')
        else:
            cnt2 += 1
            print(f'{i} : O')
        if i == 120:
            skipper.update(25)
    print(f'cnt1: {cnt1}, cnt2: {cnt2}')
