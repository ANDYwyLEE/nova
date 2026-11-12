"""For the install-guide workflow: make a short test clip, and check that a folder holds a recording of it.

    python tools/ci_clip.py clip.mp4        30 s of 640x360 at 10 fps (libx264 from the av wheel)
    python tools/ci_clip.py --check DIR     the first .mp4 under DIR decodes to more than 30 pictures
"""
import glob
import os
import sys
from fractions import Fraction

import av
import numpy as np

if sys.argv[1] == "--check":
    files = sorted(glob.glob(os.path.join(sys.argv[2], "**", "*.mp4"), recursive=True))
    if not files:
        sys.exit(f"no recording under {sys.argv[2]}")
    with av.open(files[0]) as c:
        n = sum(1 for _ in c.decode(video=0))
    print(files[0], n, "pictures")
    sys.exit(0 if n > 30 else f"only {n} pictures")

with av.open(sys.argv[1], "w") as c:
    s = c.add_stream("libx264", rate=10)
    s.width, s.height, s.pix_fmt = 640, 360, "yuv420p"
    for i in range(300):
        f = av.VideoFrame.from_ndarray(np.full((360, 640, 3), (i * 3) % 255, np.uint8), format="rgb24")
        f.pts, f.time_base = i, Fraction(1, 10)
        for p in s.encode(f):
            c.mux(p)
    for p in s.encode():
        c.mux(p)
