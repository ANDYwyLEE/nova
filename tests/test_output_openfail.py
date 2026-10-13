"""A writer whose first mux (connect + header) failed must never mux again: PyAV would run
avformat_write_header() a second time on the same context, which segfaults."""
import os
import subprocess
import sys
import textwrap

import pytest

import avwriter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCRIPT = textwrap.dedent("""
    import sys, numpy as np, av, logsync, avwriter
    from fractions import Fraction
    w = avwriter.AVWriter(None, path=sys.argv[1], width=320, height=180, fps=25, encoder="libx264",
                          enc_v_opts={"preset": "ultrafast", "tune": "zerolatency"}, audio_enable=False)
    errors = 0
    for i in range(10):
        f = av.VideoFrame.from_ndarray(np.zeros((180, 320, 3), np.uint8), format="rgb24")
        f.pts, f.time_base = i, Fraction(1, 25)
        try:
            w.write_video(f)
        except avwriter.OutputOpenError as e:
            errors += 1
            print("open error:", e, flush=True)
    w.close()                      # flushes the encoder: must not mux either
    print("closed", errors, flush=True)
""")


@pytest.mark.skipif(not os.path.exists("/dev/full"), reason="needs /dev/full")
def test_writes_and_close_after_a_failed_header_do_not_crash(tmp_path):
    target = tmp_path / "full.mp4"            # opens fine, every write fails with ENOSPC
    os.symlink("/dev/full", target)
    env = dict(os.environ, PYTHONPATH=ROOT)
    out = subprocess.run([sys.executable, "-c", SCRIPT, str(target)], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, f"exit {out.returncode}\n{out.stdout}\n{out.stderr}"
    assert "No space left on device" in out.stdout
    assert "closed 10" in out.stdout, out.stdout     # every write was refused without touching the muxer


class Container:
    def __init__(self, error):
        self.error, self.muxed, self.closed = error, 0, False

    def mux(self, packet):
        self.muxed += 1
        if self.error: raise self.error

    def close(self):
        self.closed = True


def test_failed_open_is_remembered_and_close_skips_the_flush():
    w = avwriter.AVWriter.__new__(avwriter.AVWriter)
    w.first_packet_sent, w.open_failed, w.timeout, w.run_flag = False, None, None, True
    w.v_frames, w.a_frames, w.v_stream, w.a_stream, w._held_audio = 5, 3, object(), object(), ["a"]
    w.container = Container(OSError(28, "No space left on device"))
    for _ in range(3):
        with pytest.raises(avwriter.OutputOpenError, match="No space left"):
            w._write("p")
    assert w.container.muxed == 1                   # only the first attempt reached the muxer
    w.close()                                       # would call v_stream.encode() (an object()) if it flushed
    assert w.container.closed and w.container.muxed == 1
