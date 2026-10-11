"""End to end without a camera or a GPU: decode a file, encode with libx264, mux segments, quit cleanly."""
import glob
import os
import signal
import subprocess
import sys
import time

import av

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOWS = os.name == "nt"


def test_help_lists_the_options():
    out = subprocess.run([sys.executable, os.path.join(ROOT, "nova.py"), "--help"],
                         capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert out.returncode == 0, out.stderr
    for opt in ("--opengl", "--accel", "--gpu", "--headless", "--geometry", "--zoom", "--ffmpeg", "--record-interval"):
        assert opt in out.stdout
    out = subprocess.run([sys.executable, os.path.join(ROOT, "cams.py"), "-h"],
                         capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert out.returncode == 0 and "nova record" in out.stdout


def test_modules_import():
    sys.path.insert(0, ROOT)
    import novacore, view, novagl, videoworker, avwriter, recorder, streamer, audioworker, auth, logsync  # noqa: F401


def test_headless_recording(test_clip, tmp_path):
    outdir = tmp_path / "rec"
    cmd = [sys.executable, "-u", os.path.join(ROOT, "nova.py"), "--headless", "-r", "-i", str(test_clip),
           "-n", "smoke", "--recording-dir", str(outdir), "--record-interval", "4s",
           "-e", "libx264", "--ffmpeg", "-preset ultrafast", "-l", "INFO"]
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {}
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=ROOT)
    p = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kw)
    out = ""
    try:
        # at least one 4 s segment must have been closed on its own by then
        deadline = time.time() + 40
        while time.time() < deadline and p.poll() is None:
            done = [f for f in glob.glob(str(outdir / "smoke" / "*.mp4")) if not f.endswith("_output.mp4")]
            if done:
                break
            time.sleep(0.5)
        assert p.poll() is None, "nova.py exited early:\n" + p.stdout.read()
        p.send_signal(signal.CTRL_BREAK_EVENT if WINDOWS else signal.SIGINT)
        try:
            out, _ = p.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
            out, _ = p.communicate()
            assert WINDOWS, "nova.py did not quit on SIGINT:\n" + out
    finally:
        if p.poll() is None:
            p.kill()
    if not WINDOWS:
        assert p.returncode == 0, out
    files = sorted(f for f in glob.glob(str(outdir / "smoke" / "*.mp4")) if not f.endswith("_output.mp4"))
    assert files, out
    with av.open(files[0]) as c:
        vs = c.streams.video[0]
        assert vs.codec_context.name == "h264"
        frames = sum(1 for _ in c.decode(vs))
    assert frames >= 25, (frames, out)
    assert "Traceback" not in out, out
    assert "[AVRecorder] saved" in out, out
