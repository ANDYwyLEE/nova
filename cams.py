#!/usr/bin/env python3
"""cams.py - implementation of the `nova` launcher (./nova on Linux and macOS, nova.bat on Windows).

The launcher is driven by cameras.ini, which holds the camera URLs *with* their passwords and is
listed in .gitignore, so credentials never reach the repository.  cameras.example.ini is the
committed template.  `nova -h` prints the commands.
"""
import os
import re
import sys
import json
import hashlib
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
LAYOUTS = os.path.join(HERE, "layouts")
WINDOWS = os.name == "nt"
RESERVED = ("defaults", "env")
VENV_PYTHON = (("venv/Scripts/python.exe",) if WINDOWS else ("venv/bin/python", "venv/bin/python3"))
HEADLESS_FLAGS = ("--headless", "--no-gui")
RECORD_FLAGS = ("-r", "--record", "--auto-record")
STREAM_FLAGS = ("-t", "--target")
GPU_FLAGS = ("--gpu",)
EARLY_EXIT_GRACE = 1.5      # seconds a camera must survive before `start` reports it as running

USAGE = """\
nova - start and stop the Nova cameras listed in cameras.ini (template: cameras.example.ini)

  nova start  [ID ...] [nova.py options]   a window per camera: every enabled camera, or the given ids
  nova record [ID ...] [nova.py options]   the same without windows, recording from the first frame
  nova run    [ID]     [nova.py options]   one camera in this terminal: log on screen, Ctrl-C quits
  nova stop   [ID ...] [--timeout S]       stop cameras started by start/record (saves state like Ctrl-C)
  nova status                              which launched cameras are still running, and in which mode
  nova list                                cameras, enabled state and options (passwords masked)
  nova layout save NAME                    remember which cameras are open and where (layouts/NAME.json)
  nova layout list                         saved layouts
  nova start --layout NAME                 open exactly those cameras where they were saved
  nova -h                                  this text          nova.py --help    every camera option
  -c FILE                                  use FILE instead of cameras.ini

Words that do not start with '-' are camera ids; everything from the first option on goes to nova.py:
  nova start frontdoor -r                  window and recording
  nova record yard -t rtmp://...           headless recording plus a live stream
Options a camera should always get belong in its cameras.ini section (opts = -r).
run ID --layout NAME puts one camera at its place in that layout.
start/record detach from the terminal and write logs/ID.log; run stays attached.
"""

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
        copy = "copy" if WINDOWS else "cp"
        sys.exit(f"{os.path.basename(path)} not found.\n"
                 f"  {copy} {os.path.basename(EXAMPLE)} {os.path.basename(path)}   and put your cameras in it (it is git-ignored).")
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
    """`[defaults] python` if set, else the project venv next to this file, else whatever runs cams.py."""
    p = defaults.get("python") if defaults else None
    if p:
        return p if os.path.isabs(p) else os.path.join(HERE, p)
    for rel in VENV_PYTHON:
        cand = os.path.join(HERE, *rel.split("/"))
        if os.path.exists(cand):
            return cand
    return sys.executable


def url_hash(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]     # the key nova.py uses in QSettings


def window_settings():
    """Nova's per-window settings store (QSettings 'judvi' / 'Nova'), or None without PySide6."""
    try:
        from PySide6.QtCore import QSettings
    except ImportError:
        return None
    return QSettings("judvi", "Nova")


def layout_names():
    return sorted(f[:-5] for f in os.listdir(LAYOUTS) if f.endswith(".json")) if os.path.isdir(LAYOUTS) else []


def layout_path(name):
    if not re.fullmatch(r"[\w.-]+", name):
        sys.exit(f"layout names use letters, digits, '.', '_' or '-': {name!r}")
    return os.path.join(LAYOUTS, f"{name}.json")


def load_layout(name):
    p = layout_path(name)
    if not os.path.exists(p):
        have = layout_names()
        sys.exit(f"no layout {name!r}" + (f"; saved layouts: {', '.join(have)}" if have
                                           else "; save one with `nova layout save NAME`"))
    with open(p, encoding="utf-8") as f:
        lay = json.load(f)
    if not isinstance(lay.get("cameras"), dict):
        sys.exit(f"layouts/{name}.json has no \"cameras\" object")
    return lay


def layout_flags(layout, cam):
    """--geometry / --zoom for a camera from a loaded layout; [] when the layout has no entry for it."""
    e = (layout or {}).get("cameras", {}).get(cam.id) or {}
    flags = []
    if e.get("geometry"):
        flags += ["--geometry", str(e["geometry"])]
    if e.get("zoom"):
        flags += ["--zoom", str(e["zoom"])]
    return flags


def command(cam, defaults, extra, record=False, layout=None):
    argv = [python_exe(defaults), "-u", os.path.join(HERE, "nova.py")]
    argv += shlex.split(defaults.get("opts", "") if defaults else "")
    argv += shlex.split(cam.opts)
    if record:
        argv += ["--headless", "-r"]
    argv += ["-n", cam.name, "-i", cam.url]
    argv += layout_flags(layout, cam)
    argv += extra
    return argv


def has_flag(argv, flags):
    return any(a in flags or a.split("=", 1)[0] in flags for a in argv[3:])   # skip python -u nova.py


def gpu_list(env):
    """GPU indices from [env] NOVA_GPU_DEVICES (or the launcher's own environment); [] if unset or invalid."""
    v = env.get("NOVA_GPU_DEVICES", os.environ.get("NOVA_GPU_DEVICES", ""))
    try:
        return [str(int(x)) for x in str(v).split(",") if x.strip()]
    except ValueError:
        return []


def pinned_gpu(argv):
    """The value of --gpu in argv, or None."""
    toks = argv[3:]
    for i, a in enumerate(toks):
        if a == "--gpu" and i + 1 < len(toks):
            return toks[i + 1]
        if a.startswith("--gpu="):
            return a.split("=", 1)[1]
    return None


def assign_gpu(cam, cams, env, argv):
    """Deal the cameras over the listed GPUs in file order, so every camera always gets the same one and
    14 cameras on 0,1 become 7 + 7.  A camera with --gpu in its options keeps that; a single GPU or no
    list leaves NOVA_GPU_DEVICES alone."""
    pinned = pinned_gpu(argv)
    if pinned is not None:
        return pinned
    devices = gpu_list(env)
    if len(devices) < 2:
        return None
    order = [c for c in cams if c.enabled]
    i = order.index(cam) if cam in order else cams.index(cam)
    return devices[i % len(devices)]


def process_env(env, gpu):
    e = child_env(env)
    if gpu is not None:
        e["NOVA_GPU_DEVICES"] = gpu
    return e


def mode_of(argv):
    parts = ["headless" if has_flag(argv, HEADLESS_FLAGS) else "window"]
    if has_flag(argv, RECORD_FLAGS):
        parts.append("rec")
    if has_flag(argv, STREAM_FLAGS):
        parts.append("stream")
    return "+".join(parts)


def child_env(env):
    e = os.environ.copy()
    e.update({k: str(v) for k, v in env.items()})
    e.setdefault("PYTHONPATH", HERE)
    return e


def read_pids():
    try:
        with open(PIDS, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}
    return {k: (v if isinstance(v, dict) else {"pid": int(v), "mode": "?"}) for k, v in raw.items()}


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


def log_tail(cid, n=5):
    try:
        with open(os.path.join(LOGDIR, f"{cid}.log"), "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4000))
            lines = f.read().decode("utf-8", "replace").splitlines()
        return [l for l in lines if l.strip()][-n:]
    except OSError:
        return []


def select(cams, ids, only_enabled):
    if ids:
        by_id = {c.id: c for c in cams}
        missing = [i for i in ids if i not in by_id]
        if missing:
            sys.exit(f"unknown camera id(s): {', '.join(missing)}  (see: nova list)")
        return [by_id[i] for i in ids]
    return [c for c in cams if c.enabled or not only_enabled]


# ------------------------------------------------------------------ commands
def cmd_list(defaults, env, cams, args):
    w = max([len(c.id) for c in cams] + [2])
    gpus = len(gpu_list(env)) > 1
    print(f"{'id':<{w}}  on   {'gpu  ' if gpus else ''}name                     url")
    for c in cams:
        g = assign_gpu(c, cams, env, command(c, defaults, [])) if gpus else None
        print(f"{c.id:<{w}}  {'yes' if c.enabled else 'no ':<3}  {(g or '-') + '    ' if gpus else ''}"
              f"{c.name[:24]:<24} {mask_url(c.url)}"
              + (f"\n{'':<{w}}       opts: {c.opts}" if c.opts else ""))
    if defaults and defaults.get("opts"):
        print(f"\ndefault opts: {defaults.get('opts')}")
    if env:
        print("env: " + " ".join(f"{k}={v}" for k, v in env.items()))
    print(f"python: {python_exe(defaults)}")


def cmd_start(defaults, env, cams, args, record=False):
    os.makedirs(LOGDIR, exist_ok=True)
    pids = read_pids()
    stagger = float(defaults.get("stagger", 0.5)) if defaults else 0.5
    launched, failed = [], 0
    layout = load_layout(args.layout) if args.layout else None
    if layout and not args.ids:
        # a layout says which cameras are open, `enabled` does not apply
        known = {c.id: c for c in cams}
        missing = [i for i in layout["cameras"] if i not in known]
        if missing:
            print(f"layout {args.layout}: not in cameras.ini, skipped: {', '.join(missing)}")
        chosen = [known[i] for i in layout["cameras"] if i in known]
    else:
        chosen = select(cams, args.ids, only_enabled=True)
    for cam in chosen:
        old = pids.get(cam.id)
        if old and alive(old["pid"]):
            print(f"{cam.id}: already running (pid {old['pid']}, {old['mode']})")
            continue
        argv = command(cam, defaults, args.extra, record, layout)
        mode = mode_of(argv)
        if mode == "headless":
            print(f"{cam.id}: refused - headless without -r or -t would neither show nor save anything "
                  f"(use `nova record`, or add -t URL)")
            failed += 1
            continue
        gpu = assign_gpu(cam, cams, env, argv)
        with open(os.path.join(LOGDIR, f"{cam.id}.log"), "ab") as log:
            kw = {"cwd": HERE, "env": process_env(env, gpu), "stdout": log, "stderr": subprocess.STDOUT,
                  "stdin": subprocess.DEVNULL}
            if WINDOWS:
                kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kw["start_new_session"] = True
            p = subprocess.Popen(argv, **kw)
        pids[cam.id] = {"pid": p.pid, "mode": mode, "gpu": gpu, "layout": args.layout}
        write_pids(pids)
        tags = mode + (f", gpu {gpu}" if gpu is not None else "") + (f", layout {args.layout}" if layout else "")
        print(f"{cam.id}: starting (pid {p.pid}, {tags}) -> logs/{cam.id}.log")
        launched.append((cam, p, time.time()))
        time.sleep(stagger)
    # A camera that dies within the grace period did not start: say so instead of "started".
    for cam, p, t0 in launched:
        try:
            p.wait(timeout=max(t0 + EARLY_EXIT_GRACE - time.time(), 0.01))
        except subprocess.TimeoutExpired:
            continue
        failed += 1
        pids.pop(cam.id, None)
        print(f"{cam.id}: exited with code {p.returncode} right after start; logs/{cam.id}.log ends with:")
        for line in log_tail(cam.id):
            print(f"    {line}")
    write_pids(pids)
    ok = sum(1 for _, p, _ in launched if p.returncode is None)
    if not launched and not failed:
        print("nothing started")
    else:
        print(f"{ok} running" + (f", {failed} failed" if failed else ""))
    return 1 if failed else 0


def cmd_record(defaults, env, cams, args):
    return cmd_start(defaults, env, cams, args, record=True)


def cmd_stop(defaults, env, cams, args):
    pids = read_pids()
    targets = {k: v for k, v in pids.items() if not args.ids or k in args.ids}
    if not targets:
        print("no launched cameras (use `nova start` or `nova record`)")
        return
    for cid, e in targets.items():
        if alive(e["pid"]):
            try:
                interrupt(e["pid"])
                print(f"{cid}: stopping (pid {e['pid']})")
            except Exception as ex:
                print(f"{cid}: could not signal pid {e['pid']}: {ex}")
        else:
            print(f"{cid}: not running")
    deadline = time.time() + float(args.timeout)
    while time.time() < deadline and any(alive(e["pid"]) for e in targets.values()):
        time.sleep(0.25)
    for cid, e in targets.items():
        if alive(e["pid"]):
            print(f"{cid}: still running after {args.timeout}s (pid {e['pid']}); kill it manually if needed")
        else:
            pids.pop(cid, None)
    write_pids(pids)


def cmd_status(defaults, env, cams, args):
    pids = read_pids()
    if not pids:
        print("no launched cameras (use `nova start` or `nova record`)")
        return
    w = max(len(k) for k in pids)
    for cid, e in pids.items():
        state = "running" if alive(e["pid"]) else "exited "
        gpu = f"gpu {e['gpu']}" if e.get("gpu") is not None else ""
        lay = f"layout {e['layout']}" if e.get("layout") else ""
        print(f"{cid:<{w}}  pid {e['pid']:<8} {state}  {e['mode']:<16} {gpu:<6} {lay:<18} logs/{cid}.log")


def cmd_run(defaults, env, cams, args):
    cid = args.id or (defaults.get("camera") if defaults else None)
    if cid:
        cam = select(cams, [cid], only_enabled=False)[0]
    else:
        enabled = [c for c in cams if c.enabled]
        if not enabled:
            sys.exit("no enabled camera in cameras.ini")
        cam = enabled[0]
    argv = command(cam, defaults, args.extra, layout=load_layout(args.layout) if args.layout else None)
    gpu = assign_gpu(cam, cams, env, argv)
    print(f"running {cam.id}" + (f" on gpu {gpu}" if gpu is not None else "") + ": "
          + " ".join(mask_url(a) for a in argv[2:]), file=sys.stderr)
    p = subprocess.Popen(argv, cwd=HERE, env=process_env(env, gpu))
    try:
        return p.wait()
    except KeyboardInterrupt:
        # Ctrl-C from the terminal reaches nova.py as well; a SIGINT aimed only at the launcher is
        # forwarded.  Either way nova.py gets time to save its state (subprocess.call would kill it
        # after 0.25 s).
        if p.poll() is None:
            try:
                interrupt(p.pid)
            except OSError:
                pass
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.terminate()
        return 130


def cmd_layout(defaults, env, cams, args):
    sub = args.ids[0] if args.ids else "list"
    if sub == "list" and len(args.ids) <= 1:
        names = layout_names()
        if not names:
            print("no layouts yet: arrange the windows, then `nova layout save NAME`")
            return
        for n in names:
            lay = load_layout(n)
            ids = list(lay["cameras"])
            print(f"{n:<20} {len(ids):>2} cameras  saved {str(lay.get('saved', '?'))[:16]}  {', '.join(ids)}"[:200])
        return
    if sub != "save" or len(args.ids) != 2:
        sys.exit("usage: nova layout save NAME | nova layout list")
    name = args.ids[1]
    path = layout_path(name)
    qs = window_settings()
    if qs is None:
        sys.exit("layout save needs PySide6: run it as ./nova (or nova.bat) so the project venv is used")
    pids = read_pids()
    running = [c for c in cams if c.id in pids and alive(pids[c.id]["pid"])
               and not pids[c.id].get("mode", "").startswith("headless")]
    chosen = running or [c for c in cams if c.enabled]
    source = "open cameras" if running else "enabled cameras (none open: positions are from their last exit)"
    entries, missing = {}, []
    for c in chosen:
        h = url_hash(c.url)
        geom = qs.value(f"geom_{h}")
        if not geom:
            missing.append(c.id)
            continue
        zoom = qs.value(f"zoom_{h}")
        entries[c.id] = {"geometry": str(geom), **({"zoom": str(zoom)} if zoom else {})}
    if not entries:
        sys.exit("no window positions on record yet: open the cameras once (nova start), then save")
    os.makedirs(LAYOUTS, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"saved": time.strftime("%Y-%m-%dT%H:%M:%S"), "cameras": entries}, f, indent=2)
    print(f"layout {name!r}: {len(entries)} {source} -> layouts/{name}.json")
    for cid, e in entries.items():
        print(f"  {cid:<20} {e['geometry']}" + (f"  zoom {e['zoom']}" if e.get("zoom") not in (None, "0.0,0.0,1.0,1.0") else ""))
    if missing:
        print(f"  skipped, never shown a window: {', '.join(missing)}")


COMMANDS = {"list": cmd_list, "start": cmd_start, "record": cmd_record, "stop": cmd_stop,
            "status": cmd_status, "run": cmd_run, "layout": cmd_layout}


def take_option(extra, name):
    """Remove `--name VALUE` / `--name=VALUE` from extra (launcher option, not for nova.py); return VALUE or None."""
    for i, t in enumerate(extra):
        if t == name and i + 1 < len(extra):
            return extra.pop(i + 1), extra.pop(i)
        if t.startswith(name + "="):
            extra.pop(i)
            return t.split("=", 1)[1], None
    return None, None


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
        if len(argv) < 2:
            sys.exit("-c needs a file")
        config = argv[1]; argv = argv[2:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE.rstrip()); return 0
    cmd, tokens = argv[0], argv[1:]
    if cmd not in COMMANDS:
        sys.exit(f"unknown command '{cmd}'\n\n{USAGE.rstrip()}")

    class Args: pass
    args = Args()
    args.ids, args.extra = split_ids(tokens)
    args.timeout = 15
    if cmd == "stop" and args.extra[:1] == ["--timeout"] and len(args.extra) > 1:
        args.timeout = float(args.extra[1]); args.extra = args.extra[2:]
    args.layout = None
    if cmd in ("start", "run", "record"):
        args.layout, _ = take_option(args.extra, "--layout")
        if cmd == "record" and args.layout:
            print("note: --layout is ignored by record (no windows)"); args.layout = None
    if cmd == "run":
        if len(args.ids) > 1:
            sys.exit("run takes one camera id")
        args.id = args.ids[0] if args.ids else None

    defaults, env, cams = load(config)
    return COMMANDS[cmd](defaults, env, cams, args)


if __name__ == "__main__":
    sys.exit(main() or 0)
