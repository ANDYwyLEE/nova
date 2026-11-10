"""Windows end to end on a CI runner: real Nova windows started by the launcher, fed by a local RTSP server.

mediamtx serves two looped clips (cam1 H.264+AAC, cam2 H.264 only), published with the ffmpeg binary.
Five cameras: QPainter, OpenGL, --accel d3d11va, --accel qsv (both fall back to software on a GPU-less
runner) and one recording 5 s segments. Checks start, status over the named pipe, restart, recording,
stop, logs, and saves screenshots. Every check is reported; the exit code is 1 if any failed.

    python .github/wintest/e2e.py --launcher "cmd /c nova.bat" --mediamtx mediamtx.exe --out e2e-out
"""
import argparse
import glob
import os
import shlex
import shutil
import subprocess
import sys
import time

import av
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHECKS = []
INI = """\
[defaults]
stagger = 0.3
opts = -l DEBUG

[env]
NOVA_MIN_RECORD_INTERVAL = 1

[qp]
name = QPainter
url = rtsp://127.0.0.1:8554/cam1
opts = --geometry 0,0,400,225

[gl]
name = OpenGL
url = rtsp://127.0.0.1:8554/cam2
opts = --opengl --geometry 410,0,400,225

[d3d]
name = D3D11VA
url = rtsp://127.0.0.1:8554/cam1
opts = --accel d3d11va --geometry 0,240,400,225

[qsv]
name = QSV
url = rtsp://127.0.0.1:8554/cam2
opts = --accel qsv --geometry 410,240,400,225

[rec]
name = Recorder
url = rtsp://127.0.0.1:8554/cam1
opts = -r --record-interval 5s --geometry 0,480,400,225
"""
CAMS = ["qp", "gl", "d3d", "qsv", "rec"]
MEDIAMTX_YML = """\
logLevel: info
api: no
metrics: no
pprof: no
playback: no
rtmp: no
hls: no
webrtc: no
srt: no
rtspAddress: :8554
paths:
  all_others:
"""


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""), flush=True)
    return ok


def make_clip(path, seconds=10, audio=True):
    with av.open(path, "w") as out:
        v = out.add_stream("libx264", rate=25)
        v.width, v.height, v.pix_fmt = 640, 360, "yuv420p"
        v.codec_context.gop_size = 25
        v.codec_context.options = {"preset": "ultrafast", "tune": "zerolatency"}
        a = None
        if audio:
            a = out.add_stream("aac", rate=48000)
            a.layout = "stereo"
        n = seconds * 25
        apts = 0
        for i in range(n):
            img = np.zeros((360, 640, 3), dtype=np.uint8)
            img[:, :, 0] = np.linspace(0, 255, 640, dtype=np.uint8)[None, :]
            img[:, :, 2] = 255 - (i * 4) % 256
            x = (i * 8) % 640
            img[:, x:x + 16, :] = 255
            f = av.VideoFrame.from_ndarray(img, format="rgb24")
            f.pts = i
            for p in v.encode(f):
                out.mux(p)
            while a and apts < (i + 1) * 48000 // 25:
                t = (apts + np.arange(1024)) / 48000
                s = (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
                af = av.AudioFrame.from_ndarray(np.stack([s, s]), format="fltp", layout="stereo")
                af.sample_rate, af.pts = 48000, apts
                apts += 1024
                for p in a.encode(af):
                    out.mux(p)
        for p in v.encode(None):
            out.mux(p)
        if a:
            for p in a.encode(None):
                out.mux(p)


def rtsp_ready(url, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with av.open(url, options={"rtsp_transport": "tcp"}, timeout=5) as c:
                if c.streams.video:
                    return True
        except av.error.FFmpegError:
            pass
        time.sleep(1)
    return False


def launcher(cmd, *args, timeout=180):
    t = time.time()
    r = subprocess.run(LAUNCHER + [cmd, *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    out = (r.stdout + r.stderr).strip()
    print(f"$ nova {cmd} {' '.join(args)}  -> rc {r.returncode} in {time.time() - t:.1f}s\n{out}\n", flush=True)
    return r.returncode, out, time.time() - t


def status_blocks(out):
    """{camera id: its status text (the pid line and the live line under it)}."""
    blocks, cur = {}, None
    for line in out.splitlines():
        if line and not line[0].isspace() and not line.startswith("warning:"):
            cur = line.split()[0]
            blocks[cur] = line
        elif cur:
            blocks[cur] += "\n" + line
    return blocks


def wait_video(ids, timeout=60):
    end, out = time.time() + timeout, ""
    while time.time() < end:
        _, out, _ = launcher("status")
        b = status_blocks(out)
        if all("video 640x360" in b.get(i, "") for i in ids):
            return True, out
        time.sleep(3)
    return False, out


def screenshot(path):
    code = ("import sys; from PySide6.QtWidgets import QApplication; app = QApplication([]); "
            "s = app.primaryScreen(); print(s.geometry()); sys.exit(0 if s.grabWindow(0).save(sys.argv[1]) else 1)")
    env = dict(os.environ)
    env.pop("QT_QPA_PLATFORM", None)
    r = subprocess.run([sys.executable, "-c", code, path], capture_output=True, text=True, env=env, timeout=60)
    return check(f"screenshot {os.path.basename(path)}", r.returncode == 0 and os.path.exists(path),
                 (r.stdout + r.stderr).strip())


def recordings():
    return sorted(f for f in glob.glob(os.path.join(ROOT, "videos", "**", "*.mp4"), recursive=True)
                  if not f.endswith("_output.mp4"))


def grep(path, *words, limit=12):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    return [l for l in lines if any(w.lower() in l.lower() for w in words)][:limit]


def main():
    global LAUNCHER
    ap = argparse.ArgumentParser()
    ap.add_argument("--launcher", required=True, help='e.g. "cmd /c nova.bat" or "sh ./nova"')
    ap.add_argument("--mediamtx", required=True)
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--out", required=True)
    ap.add_argument("--env", action="append", default=[], help="KEY=VALUE for cameras.ini [env]")
    ap.add_argument("--gl", choices=("works", "fails"), required=True,
                    help="fails: no usable OpenGL here, the --opengl camera must fail cleanly at start")
    a = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="backslashreplace")
    if os.environ.get("GITHUB_ACTIONS") != "true":
        sys.exit("CI only: this rewrites cameras.ini, logs/ and videos/ of the checkout")
    for d in ("logs", "videos"):
        shutil.rmtree(os.path.join(ROOT, d), ignore_errors=True)
    gl_ok = a.gl == "works"
    live = CAMS if gl_ok else [c for c in CAMS if c != "gl"]
    LAUNCHER = shlex.split(a.launcher)
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    print("python", sys.version, sys.executable, "| av", av.__version__, av.ffmpeg_version_info, flush=True)

    ini = os.path.join(ROOT, "cameras.ini")
    assert not os.path.exists(ini), "cameras.ini already exists; this script writes its own"
    with open(ini, "w") as f:
        f.write(INI.replace("[env]\n", "[env]\n" + "".join(e.replace("=", " = ", 1) + "\n" for e in a.env)))
    if os.name != "nt":
        os.chmod(ini, 0o600)
    a.mediamtx = os.path.normpath(os.path.abspath(a.mediamtx))
    with open(os.path.join(out, "mediamtx.yml"), "w") as f:
        f.write(MEDIAMTX_YML)
    procs = []
    try:
        make_clip(os.path.join(out, "cam1.mp4"), audio=True)
        make_clip(os.path.join(out, "cam2.mp4"), audio=False)
        mlog = open(os.path.join(out, "mediamtx.log"), "w")
        procs.append(subprocess.Popen([a.mediamtx, os.path.join(out, "mediamtx.yml")], stdout=mlog, stderr=mlog))
        time.sleep(2)
        for cam in ("cam1", "cam2"):
            flog = open(os.path.join(out, f"ffmpeg-{cam}.log"), "w")
            procs.append(subprocess.Popen(
                [a.ffmpeg, "-hide_banner", "-loglevel", "warning", "-re", "-stream_loop", "-1",
                 "-i", os.path.join(out, f"{cam}.mp4"), "-c", "copy", "-f", "rtsp", "-rtsp_transport", "tcp",
                 f"rtsp://127.0.0.1:8554/{cam}"], stdout=flog, stderr=flog, stdin=subprocess.DEVNULL))
        for cam in ("cam1", "cam2"):
            if not check(f"RTSP source {cam} up", rtsp_ready(f"rtsp://127.0.0.1:8554/{cam}")):
                return 1

        rc, txt, _ = launcher("list")
        check("nova list", rc == 0 and all(c in txt for c in CAMS))
        rc, txt, dt = launcher("start")
        if gl_ok:
            check("nova start: 5 running", rc == 0 and "5 running" in txt, f"{dt:.1f}s")
        else:
            # within EARLY_EXIT_GRACE (1.5 s) `start` reports it; a slower GL failure shows in status
            check("nova start: the others running", "gl: starting" in txt and ("5 running" in txt or "4 running" in txt),
                  f"rc {rc}, {dt:.1f}s")
        ok, txt = wait_video(live)
        check("status: every camera reports video 640x360 over its control channel", ok)
        b = status_blocks(txt)
        check("status: rec camera reports recording", "record" in b.get("rec", ""), b.get("rec", "").strip())
        check("status: cam1 cameras report audio", all("audio" in b.get(i, "") for i in ("qp", "rec")))
        time.sleep(3)
        screenshot(os.path.join(out, "wall-1.png"))

        rc, txt, dt = launcher("restart", "qp" if not gl_ok else "gl")
        check(f"nova restart {'gl' if gl_ok else 'qp'}", rc == 0, f"{dt:.1f}s")
        ok, _ = wait_video(["gl" if gl_ok else "qp"])
        check("back after restart", ok)

        end = time.time() + 40
        while time.time() < end and len(recordings()) < 2:
            time.sleep(1)
        check("rec: two segments closed", len(recordings()) >= 2, ", ".join(os.path.relpath(f, ROOT) for f in recordings()))
        screenshot(os.path.join(out, "wall-2.png"))

        rc, txt, dt = launcher("stop")
        check("nova stop", rc == 0, f"{dt:.1f}s")
        check("nova stop under 10 s", dt < 10, f"{dt:.1f}s")
        rc, txt, _ = launcher("status")
        check("status after stop: nothing launched", "no launched cameras" in txt, txt)

        for cid in CAMS:
            log = os.path.join(ROOT, "logs", f"{cid}.log")
            check(f"{cid}.log: no logging errors (encoding)", not grep(log, "--- Logging error ---"))
            if cid == "gl" and not gl_ok:
                # no usable OpenGL: a clean CRITICAL and exit 1, not a crash
                check("gl.log: ends cleanly (OpenGL could not be initialised, or a fallback that exited gracefully)",
                      grep(log, "OpenGL could not be initialised") or grep(log, "exited gracefully"),
                      " | ".join(grep(log, "CRITICAL", "exited", limit=4)))
            else:
                tb = grep(log, "Traceback")
                check(f"{cid}.log: no Traceback", not tb, "; ".join(tb))
                check(f"{cid}.log: exited gracefully", grep(log, "exited gracefully"))
            print(f"--- {cid}.log highlights", *grep(log, "decoder", "accel", "hwaccel", "opengl", "GL_", "renderer",
                                                     "encoder", "critical", "error", "saved", limit=25),
                  sep="\n  ", flush=True)

        files = recordings()
        frames = []
        for fpath in files:
            try:
                with av.open(fpath) as c:
                    vs = c.streams.video[0]
                    n = sum(1 for _ in c.decode(vs))
                    frames.append(n)
                    print(f"{os.path.relpath(fpath, ROOT)}: {vs.codec_context.name} {vs.width}x{vs.height}, {n} frames, "
                          f"audio {[s.codec_context.name for s in c.streams.audio]}", flush=True)
            except av.error.FFmpegError as e:
                check(f"recording {os.path.basename(fpath)} decodes", False, str(e))
        check("a recorded segment holds >= 100 frames (5 s at 25 fps)", frames and max(frames) >= 100, str(frames))
        wdir = os.path.join(os.environ.get("APPDATA", ""), "nova", "windows") if os.name == "nt" else None
        if wdir:
            inis = glob.glob(os.path.join(wdir, "*.conf"))
            for f in inis:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    print(f"--- {f}\n{fh.read().strip()}", flush=True)
            check("window places saved in %APPDATA%\\nova\\windows", len(inis) >= 2, f"{len(inis)} files")
    finally:
        try:
            launcher("stop", timeout=60)
        except Exception as e:                                      # noqa: BLE001
            print("final stop:", e)
        for p in procs:
            p.kill()
        os.makedirs(os.path.join(out, "logs"), exist_ok=True)
        for f in glob.glob(os.path.join(ROOT, "logs", "*")):
            shutil.copy(f, os.path.join(out, "logs"))
        os.remove(ini)

    failed = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed" + (f"; FAILED: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
