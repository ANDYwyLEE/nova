#!/usr/bin/env python3
"""cams.py - start, stop, list and run Nova cameras from cameras.ini.

cameras.ini holds the camera URLs *with* their passwords and is listed in .gitignore, so
credentials never reach the repository.  cameras.example.ini is the committed template.

    python cams.py list                 show the cameras (passwords masked)
    python cams.py start [ID ...]       start every enabled camera (or the given ones) in the background
    python cams.py stop  [ID ...]       stop cameras started by this launcher (Ctrl-C semantics)
    python cams.py status               which launched cameras are still running
    python cams.py run [ID] [nova args] run one camera in the foreground, e.g.  run frontdoor -r -t rtmp://...

Extra arguments after the command are passed straight to nova.py.
"""
import os
import sys
import json
import time
import shlex
import signal
import subprocess
import configparser

HERE = os.path.dirname(os.path.abspath(__file__))
INI = os.path.join(HERE, "cameras.ini")
EXAMPLE = os.path.join(HERE, "cameras.example.ini")
PIDS = os.path.join(HERE, ".nova-pids.json")
LOGDIR = os.path.join(HERE, "logs")
WINDOWS = os.name == "nt"
RESERVED = ("defaults", "env")

sys.path.insert(0, HERE)
try:
    from util import mask_url
except Exception:  # pragma: no cover - util.py missing only in a broken checkout
    def mask_url(u): return u


class Camera:
    def __init__(self, cid, sec):
        self.id = cid
        self.name = sec.get("name", cid).strip()
        self.url = (sec.get("url") or "").strip()
        self.opts = sec.get("opts", "").strip()
        self.enabled = sec.getboolean("enabled", fallback=True)


def load(path):
    if not os.path.exists(path):
        sys.exit(f"{os.path.basename(path)} not found.\n"
                 f"  cp {os.path.basename(EXAMPLE)} {os.path.basename(path)}   and put your cameras in it (it is git-ignored).")
    cp = configparser.ConfigParser(interpolation=None)   # keep '%' and '$' in URLs untouched
    cp.optionxform = str                                 # environment variable names keep their case
    cp.read(path, encoding="utf-8")
    defaults = cp["defaults"] if cp.has_section("defaults") else {}
    env = dict(cp["env"]) if cp.has_section("env") else {}
    cams = []
    for sec in cp.sections():
        if sec in RESERVED:
            continue
        cam = Camera(sec, cp[sec])
        if not cam.url:
            print(f"warning: [{sec}] has no url, skipped", file=sys.stderr)
            continue
        cams.append(cam)
    return defaults, env, cams


def python_exe(defaults):
    p = defaults.get("python") if defaults else None
    return os.path.join(HERE, p) if p and not os.path.isabs(p) else (p or sys.executable)


def command(cam, defaults, extra):
    argv = [python_exe(defaults), "-u", os.path.join(HERE, "nova.py")]
    argv += shlex.split(defaults.get("opts", "") if defaults else "")
    argv += shlex.split(cam.opts)
    argv += ["-n", cam.name, "-i", cam.url]
    argv += extra
    return argv


def child_env(env):
    e = os.environ.copy()
    e.update({k: str(v) for k, v in env.items()})
    e.setdefault("PYTHONPATH", HERE)
    return e


def read_pids():
    try:
        with open(PIDS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_pids(d):
    with open(PIDS, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


def alive(pid):
    if WINDOWS:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def interrupt(pid):
    """Ask a camera to quit the way Ctrl-C would (window geometry and recordings are saved)."""
    if WINDOWS:
        os.kill(pid, signal.CTRL_BREAK_EVENT)
    else:
        os.kill(pid, signal.SIGINT)


def select(cams, ids, only_enabled):
    if ids:
        by_id = {c.id: c for c in cams}
        missing = [i for i in ids if i not in by_id]
        if missing:
            sys.exit(f"unknown camera id(s): {', '.join(missing)}  (see: cams.py list)")
        return [by_id[i] for i in ids]
    return [c for c in cams if c.enabled or not only_enabled]


# ------------------------------------------------------------------ commands
def cmd_list(defaults, env, cams, args):
    w = max([len(c.id) for c in cams] + [2])
    print(f"{'id':<{w}}  on   name                     url")
    for c in cams:
        print(f"{c.id:<{w}}  {'yes' if c.enabled else 'no ':<3}  {c.name[:24]:<24} {mask_url(c.url)}"
              + (f"\n{'':<{w}}       opts: {c.opts}" if c.opts else ""))
    if defaults and defaults.get("opts"):
        print(f"\ndefault opts: {defaults.get('opts')}")
    if env:
        print("env: " + " ".join(f"{k}={v}" for k, v in env.items()))


def cmd_start(defaults, env, cams, args):
    os.makedirs(LOGDIR, exist_ok=True)
    pids = read_pids()
    started = 0
    for cam in select(cams, args.ids, only_enabled=True):
        old = pids.get(cam.id)
        if old and alive(int(old)):
            print(f"{cam.id}: already running (pid {old})")
            continue
        log = open(os.path.join(LOGDIR, f"{cam.id}.log"), "ab")
        kw = {"cwd": HERE, "env": child_env(env), "stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
        if WINDOWS:
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        p = subprocess.Popen(command(cam, defaults, args.extra), **kw)
        pids[cam.id] = p.pid
        write_pids(pids)
        print(f"{cam.id}: started (pid {p.pid}) -> logs/{cam.id}.log")
        started += 1
        time.sleep(float(defaults.get("stagger", 0.5)) if defaults else 0.5)
    if not started:
        print("nothing started")


def cmd_stop(defaults, env, cams, args):
    pids = read_pids()
    targets = {k: v for k, v in pids.items() if not args.ids or k in args.ids}
    if not targets:
        print("no launched cameras recorded (use `cams.py start`)")
        return
    for cid, pid in targets.items():
        if alive(int(pid)):
            try:
                interrupt(int(pid))
                print(f"{cid}: stopping (pid {pid})")
            except Exception as e:
                print(f"{cid}: could not signal pid {pid}: {e}")
        else:
            print(f"{cid}: not running")
    deadline = time.time() + float(args.timeout)
    while time.time() < deadline and any(alive(int(p)) for p in targets.values()):
        time.sleep(0.25)
    for cid, pid in targets.items():
        if alive(int(pid)):
            print(f"{cid}: still running after {args.timeout}s (pid {pid}); kill it manually if needed")
        else:
            pids.pop(cid, None)
    write_pids(pids)


def cmd_status(defaults, env, cams, args):
    pids = read_pids()
    if not pids:
        print("no launched cameras recorded")
        return
    for cid, pid in pids.items():
        print(f"{cid:<20} pid {pid:<8} {'running' if alive(int(pid)) else 'exited'}")


def cmd_run(defaults, env, cams, args):
    cid = args.id or (defaults.get("camera") if defaults else None)
    if cid:
        cam = select(cams, [cid], only_enabled=False)[0]
    else:
        enabled = [c for c in cams if c.enabled]
        if not enabled:
            sys.exit("no enabled camera in cameras.ini")
        cam = enabled[0]
    argv = command(cam, defaults, args.extra)
    print(f"running {cam.id}: " + " ".join(mask_url(a) for a in argv[2:]), file=sys.stderr)
    try:
        return subprocess.call(argv, cwd=HERE, env=child_env(env))
    except KeyboardInterrupt:
        return 130


USAGE = __doc__

def split_ids(tokens):
    """Leading words that do not start with '-' are camera ids; the rest (or everything after a
    literal --) is passed to nova.py untouched."""
    ids, extra = [], []
    for i, t in enumerate(tokens):
        if t == "--":
            extra = tokens[i + 1:]; break
        if t.startswith("-"):
            extra = tokens[i:]; break
        ids.append(t)
    return ids, extra


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    config = INI
    while argv and argv[0] in ("-c", "--config"):
        if len(argv) < 2: sys.exit("-c needs a file")
        config = argv[1]; argv = argv[2:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE.strip()); return 0
    cmd, tokens = argv[0], argv[1:]
    if cmd not in ("list", "start", "stop", "status", "run"):
        sys.exit(f"unknown command '{cmd}'\n\n{USAGE.strip()}")

    class Args: pass
    args = Args()
    args.ids, args.extra = split_ids(tokens)
    args.timeout = 15
    if cmd == "stop" and args.extra[:1] == ["--timeout"] and len(args.extra) > 1:
        args.timeout = float(args.extra[1]); args.extra = args.extra[2:]
    if cmd == "run":
        if len(args.ids) > 1: sys.exit("run takes one camera id")
        args.id = args.ids[0] if args.ids else None

    defaults, env, cams = load(config)
    return {"list": cmd_list, "start": cmd_start, "stop": cmd_stop, "status": cmd_status, "run": cmd_run}[cmd](defaults, env, cams, args)


if __name__ == "__main__":
    sys.exit(main() or 0)
