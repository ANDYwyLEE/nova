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
import secrets
import socket
import tempfile
import threading
import contextlib
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
PIDS_LOCK = os.path.join(HERE, ".nova-pids.lock")
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
LOG_ROTATE = 50 * 1024 * 1024   # a log above this size moves to ID.log.1 when the camera starts
CAMERA_KEYS = ("name", "url", "opts", "enabled")
DEFAULT_KEYS = ("opts", "python", "stagger", "camera")

USAGE = """\
nova - start and stop the Nova cameras listed in cameras.ini (template: cameras.example.ini)

  nova start  [ID ...] [nova.py options]   a window per camera: every enabled camera, or the given ids
  nova record [ID ...] [nova.py options]   the same without windows, recording from the first frame
  nova run    [ID]     [nova.py options]   one camera in this terminal: log on screen, Ctrl-C quits
  nova stop   [ID ...] [--timeout S]       stop cameras started by start/record (saves state like Ctrl-C)
  nova restart [ID ...] [nova.py options]  stop, then start again as launched (record stays record, layout stays)
  nova status                              launched cameras: running or exited, uptime, recording/stream state
  nova list                                cameras, enabled state and options (passwords masked)
  nova layout save NAME                    remember which cameras are open and where (layouts/NAME.json)
  nova layout list                         saved layouts
  nova start --layout NAME                 open exactly those cameras where they were saved
  nova layout apply NAME                   move the open windows to that layout's places now
  nova layout export NAME FORMAT           window rules that place that layout on a Wayland desktop
                                           (FORMAT: sway, kwin, hyprland = Lua 0.55+, hyprlang = 0.53-0.54)
  nova -h                                  this text          nova.py --help    every camera option
  -c FILE                                  use FILE instead of cameras.ini

Words that do not start with '-' are camera ids; everything from the first option on goes to nova.py:
  nova start frontdoor -r                  window and recording
  nova record yard -t rtmp://...           headless recording plus a live stream
Options a camera should always get belong in its cameras.ini section (opts = -r).
run ID --layout NAME puts one camera at its place in that layout.
start/record detach from the terminal and write logs/ID.log; run stays attached.
restart remembers each camera's mode and layout, not options typed after start: give those again.
"""

sys.path.insert(0, HERE)
try:
    from util import mask_url, window_title
except Exception:  # pragma: no cover - util.py missing only in a broken checkout
    def mask_url(u): return u
    def window_title(n): return f"Nova - {n}" if n else "Nova"


class Camera:
    def __init__(self, cid, sec):
        self.id = cid
        self.name = sec.get("name", cid).strip()
        self.url = (sec.get("url") or "").strip()
        self.opts = sec.get("opts", "").strip()
        v = str(sec.get("enabled", "yes")).strip().lower()
        if v not in configparser.ConfigParser.BOOLEAN_STATES:
            sys.exit(f"[{cid}] enabled = {v!r}: use yes or no")
        self.enabled = configparser.ConfigParser.BOOLEAN_STATES[v]


def keys_of(cp, sec, known, quiet=False):
    """A section as a dict with lower-case keys (`Enabled = no` counts); unknown keys are reported,
    since a misspelt `enable = no` would otherwise start the camera anyway."""
    d = {k.lower(): v for k, v in cp[sec].items()}
    for k in d:
        if k not in known and not quiet:
            print(f"warning: [{sec}] unknown key {k!r} ignored (known: {', '.join(known)})", file=sys.stderr)
    return d


def load(path, quiet=False):
    """(defaults, env, cameras) from cameras.ini; quiet: no warnings (nova.py reads it again)."""
    if not os.path.exists(path):
        copy = "copy" if WINDOWS else "cp"
        sys.exit(f"{os.path.basename(path)} not found.\n"
                 f"  {copy} {os.path.basename(EXAMPLE)} {os.path.basename(path)}   and put your cameras in it (it is git-ignored)"
                 + ("." if WINDOWS else f";\n  chmod 600 {os.path.basename(path)}   since it holds their passwords."))
    if not WINDOWS and not quiet and os.stat(path).st_mode & 0o077:
        print(f"warning: {os.path.basename(path)} holds passwords but other users can read it: "
              f"chmod 600 {os.path.basename(path)}", file=sys.stderr)
    cp = configparser.ConfigParser(interpolation=None)   # keep '%' and '$' in URLs untouched
    cp.optionxform = str                                 # [env] names keep their case; see keys_of
    try:
        with open(path, encoding="utf-8-sig") as f:      # -sig: Notepad and PowerShell write a BOM
            cp.read_file(f, source=path)
    except configparser.Error as e:                      # the message quotes the line: mask its URL
        sys.exit(f"{os.path.basename(path)}: {mask_url(str(e))}")
    defaults = keys_of(cp, "defaults", DEFAULT_KEYS, quiet) if cp.has_section("defaults") else {}
    env = dict(cp["env"]) if cp.has_section("env") else {}
    cams = []
    for sec in cp.sections():
        if sec in RESERVED:
            continue
        if re.search(r"[/\\]", sec) or sec.startswith("."):
            if not quiet:
                print(f"warning: [{sec}] skipped: a camera id names its log file, so it cannot contain "
                      f"'/' or '\\' or start with '.'; put the display name in `name =`", file=sys.stderr)
            continue
        cam = Camera(sec, keys_of(cp, sec, CAMERA_KEYS, quiet))
        if not cam.url:
            if not quiet:
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
    try:
        with open(p, encoding="utf-8") as f:
            lay = json.load(f)
    except (OSError, ValueError) as e:
        sys.exit(f"layouts/{name}.json: {e}")
    if not isinstance(lay, dict) or not isinstance(lay.get("cameras"), dict):
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


def umask():
    """077 (files for this user only) unless NOVA_UMASK says otherwise, e.g. 022 when another account or
    a service (Samba, a media server, backups) reads videos/."""
    v = os.environ.get("NOVA_UMASK", "077")
    try:
        return int(v, 8) & 0o777
    except ValueError:
        print(f"warning: NOVA_UMASK={v!r} is not octal; using 077", file=sys.stderr)
        return 0o077


def split_opts(s):
    """An `opts =` line as arguments, with shell quoting. On Windows backslashes are path separators
    (--recording-dir D:\\videos), not escapes."""
    return shlex.split(s.replace("\\", "\\\\") if WINDOWS else s)


def camera_args(cam, defaults):
    """The options cameras.ini gives a camera: [defaults] opts, its own opts, its name and URL."""
    return (split_opts(defaults.get("opts", "") if defaults else "") + split_opts(cam.opts)
            + ["-n", cam.name, "-i", cam.url])


def command(cam, defaults, extra, record=False, layout=None):
    """The complete nova.py command line: what the camera runs with. The launcher reads mode and
    GPU from it; the process itself gets launch_argv(), which carries no URL or stream key."""
    argv = [python_exe(defaults), "-u", os.path.join(HERE, "nova.py")] + camera_args(cam, defaults)
    if record:
        argv += ["--headless", "-r"]
    argv += layout_flags(layout, cam)
    argv += extra
    return argv


def launch_argv(cam, defaults, extra, config, control, record=False, layout=None):
    """What the camera process is started with: its id, and nova.py reads URL, name and options from
    the config itself, because a process's command line is visible to every local user (ps)."""
    argv = [python_exe(defaults), "-u", os.path.join(HERE, "nova.py"),
            "--camera", cam.id, "--config", os.path.abspath(config), "--control", control]
    if record:
        argv += ["--headless", "-r"]
    return argv + layout_flags(layout, cam) + list(extra)


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
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        # "no cameras" would make start launch every camera a second time
        sys.exit(f"{os.path.basename(PIDS)} is unreadable ({e}); check `ps` for running nova.py, then delete it")
    return {k: (v if isinstance(v, dict) else {"pid": int(v), "mode": "?"}) for k, v in raw.items()}


def write_pids(d):
    tmp = PIDS + ".tmp"                  # replace, never truncate: a reader must not see a half-written file
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, PIDS)


@contextlib.contextmanager
def pids_locked():
    """One launcher command at a time may change the pid file: two parallel `start`s used to launch a
    camera twice and lose one of the pids."""
    with open(PIDS_LOCK, "a+") as f:
        if WINDOWS:
            import msvcrt
            f.seek(0)
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)    # retries for 10 s, then raises
                    break
                except OSError:
                    print("waiting for another nova command ...", file=sys.stderr)
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("waiting for another nova command ...", file=sys.stderr)
                fcntl.flock(f, fcntl.LOCK_EX)
            yield                                                    # closing the file releases the lock


def cmdline(pid):
    """pid's command line; None if there is no such process, '' if it cannot be read here."""
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                if f.read().rsplit(b")", 1)[-1].split()[:1] == [b"Z"]:
                    return None                  # exited, not yet reaped by its parent
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except FileNotFoundError:
            return None
        except OSError:
            return ""
    try:
        r = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() or None


def alive(pid):
    """pid runs and is a Nova camera. The pid file outlives reboots and crashes, and a recycled pid of
    another program must neither be stopped nor make `start` skip the camera."""
    if WINDOWS:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass                                 # someone else's process: its command line still tells
    cl = cmdline(pid)
    return cl is not None and (cl == "" or "nova.py" in cl)


def control_address(cid):
    """Where a camera's control channel listens: a socket in the user's private runtime directory, or a
    named pipe on Windows. The launcher picks it and records it in the pid file. Unique per launch:
    another Nova with the same camera id (a test checkout, `nova run` next to `nova start`) must not
    receive this one's stop."""
    tag = f"{re.sub(r'[^\w.-]', '_', cid)}-{secrets.token_hex(4)}"
    if WINDOWS:
        user = re.sub(r"\W", "_", os.environ.get("USERNAME", "user"))
        return f"nova-{user}-{tag}"
    base = os.environ.get("XDG_RUNTIME_DIR") or os.path.join(tempfile.gettempdir(), f"nova-{os.getuid()}")
    return os.path.join(base, "nova", f"{tag}.sock")


def control(address, request, timeout=3.0):
    """Send one request to a camera's control channel (control.py); its reply as a dict, None if no
    camera answers there (not running, or started before the channel existed)."""
    if not address:
        return None
    line = (json.dumps(request) + "\n").encode()
    try:
        if WINDOWS:
            box = []

            def talk():                                 # a pipe read has no timeout: bound it with a thread
                try:
                    with open("\\\\.\\pipe\\" + address, "r+b", buffering=0) as f:
                        f.write(line)
                        got = b""
                        while not got.endswith(b"\n"):
                            c = f.read(1)
                            if not c:
                                break
                            got += c
                        box.append(got)
                except OSError:
                    pass                                # no such pipe: no camera listens there

            t = threading.Thread(target=talk, daemon=True)
            t.start()
            t.join(timeout)
            buf = box[0] if box else b""
        else:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect(address)
                s.sendall(line)
                buf = b""
                while not buf.endswith(b"\n"):
                    c = s.recv(4096)
                    if not c:
                        break
                    buf += c
        return json.loads(buf.decode("utf-8")) if buf.strip() else None
    except (OSError, ValueError):
        return None


def forget_control(entry):
    """Remove the socket of a camera that is gone (a clean exit removes it itself, a crash does not)."""
    a = entry.get("control")
    if a and os.path.isabs(a):
        try:
            os.remove(a)
        except OSError:
            pass


def interrupt(pid, address=None):
    """Ask a camera to quit the way Ctrl-C would (window geometry and recordings are saved): through its
    control channel, else with a signal (cameras started before the channel existed)."""
    reply = control(address, {"cmd": "stop"})
    if reply and reply.get("ok"):
        return
    if WINDOWS:
        os.kill(pid, signal.CTRL_BREAK_EVENT)
    else:
        os.kill(pid, signal.SIGINT)


def open_log(cid):
    """logs/ID.log for appending; a log above LOG_ROTATE moves to ID.log.1 first (one generation)."""
    path = os.path.join(LOGDIR, f"{cid}.log")
    try:
        if os.path.getsize(path) > LOG_ROTATE:
            os.replace(path, path + ".1")
    except OSError:
        pass
    return open(path, "ab")


LIVE_OPEN_TIMEOUT = 30.0        # avwriter: how long a stream's connect may take


def stop_timeout(env, streaming=False):
    """Default --timeout of stop/restart: a camera stopped while its stream is stalled needs up to
    2 x NOVA_TIMEOUT (the blocked write, then closing the dead connection; see avwriter._net_timeouts),
    and one caught in a stream's connect another LIVE_OPEN_TIMEOUT."""
    try:
        t = int(env.get("NOVA_TIMEOUT", os.environ.get("NOVA_TIMEOUT", "10000000"))) / 1_000_000
    except ValueError:
        t = 10.0
    return max(15.0, 2 * t + 5 + (LIVE_OPEN_TIMEOUT if streaming else 0))


def log_tail(cid, n=5):
    try:
        with open(os.path.join(LOGDIR, f"{cid}.log"), "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4000))
            lines = f.read().decode("utf-8", "replace").splitlines()
        return [l for l in lines if l.strip()][-n:]
    except OSError:
        return []


def fmt_uptime(seconds):
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return (f"{d}d " if d else "") + f"{h}:{m:02d}:{s:02d}"


class Args:
    """A parsed command line: camera ids, extra options for nova.py, --layout, --timeout, run's id."""
    def __init__(self, ids=(), extra=(), layout=None, timeout=None, id=None, config=INI):
        self.ids, self.extra, self.layout, self.timeout, self.id = list(ids), list(extra), layout, timeout, id
        self.config = config


def stop_entries(pids, targets, timeout):
    """Interrupt the running processes among targets (id -> pid-file entry), wait up to timeout seconds
    and drop the finished ones from pids.  Returns the ids that are still running."""
    for cid, e in targets.items():
        if alive(e["pid"]):
            try:
                interrupt(e["pid"], e.get("control"))
                print(f"{cid}: stopping (pid {e['pid']})")
            except Exception as ex:
                print(f"{cid}: could not signal pid {e['pid']}: {ex}")
        else:
            print(f"{cid}: not running")
    timeout = 15.0 if timeout is None else timeout
    deadline = time.time() + float(timeout)
    while time.time() < deadline and any(alive(e["pid"]) for e in targets.values()):
        time.sleep(0.25)
    stuck = []
    for cid, e in targets.items():
        if alive(e["pid"]):
            print(f"{cid}: still running after {timeout}s (pid {e['pid']}); kill it manually if needed")
            stuck.append(cid)
        else:
            pids.pop(cid, None)
            forget_control(e)
    write_pids(pids)
    return stuck


def restart_groups(ids, entries, layout):
    """How to launch each camera again, (record, layout) -> ids: a camera that `record` started stays
    headless, a window keeps its layout unless one is given now, one never launched opens as a window."""
    groups = {}
    for cid in ids:
        e = entries.get(cid) or {}
        record = str(e.get("mode", "")).startswith("headless+rec")
        groups.setdefault((record, None if record else (layout or e.get("layout"))), []).append(cid)
    return groups


# ---------------------------------------------------------------- layout export (Wayland window rules)
EXPORT_FORMATS = ("sway", "kwin", "hyprland", "hyprlang")


def re_literal(s):
    """s as a regular expression matching exactly s (only the metacharacters escaped, so names stay readable)."""
    return re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", s)


def parse_geometry(s):
    x, y, w, h = (int(float(v)) for v in str(s).split(","))
    return x, y, w, h


def export_rules(layout, name, cams, fmt):
    """Window rules that give a Wayland compositor the layout's placement, since there Nova cannot place
    its own windows.  The rules match the window title, 'Nova - NAME'."""
    if fmt not in EXPORT_FORMATS:
        sys.exit(f"unknown format {fmt!r}; one of: {', '.join(EXPORT_FORMATS)}")
    stamp = f"Nova layout '{name}', exported {time.strftime('%Y-%m-%d %H:%M')} by `nova layout export`."
    c = "--" if fmt == "hyprland" else "#"
    notes = {
        "sway": ["Append to ~/.config/sway/config and reload (swaymsg reload).  Positions are absolute",
                 "(over all outputs), as Nova saved them.  Also works for i3 without `absolute`."],
        "hyprland": ["Hyprland >= 0.55 (Lua config): add to your config; 0.53-0.54 need `hyprlang` instead.",
                     "`move` is monitor-local in Hyprland: with more than one monitor subtract the monitor's",
                     "position (hyprctl monitors)."],
        "hyprlang": ["Hyprland 0.53-0.54 (hyprland.conf; Ubuntu 26.04 ships 0.53): add to your config, 0.55 and",
                     "later use Lua (`hyprland`).  `move` is monitor-local in Hyprland: with more than one",
                     "monitor subtract the monitor's position (hyprctl monitors)."],
        "kwin": ["Import in System Settings > Window Management > Window Rules > Import.  Positions apply",
                 "when a window opens (positionrule=3); you can still drag the windows afterwards."],
    }[fmt]
    out = [f"{c} {stamp}"] + [f"{c} {n}" for n in notes] + [""]
    byid = {cam.id: cam for cam in cams}
    for cid, e in layout.get("cameras", {}).items():
        cam = byid.get(cid)
        if cam is None or not e.get("geometry"):
            continue
        try:
            x, y, w, h = parse_geometry(e["geometry"])
        except ValueError:
            continue
        title = window_title(cam.name)
        if fmt == "sway":
            sway_title = re_literal(title).replace('"', '.')   # sway's config parser takes no quote escape
            out.append(f'for_window [title="^{sway_title}$"] floating enable, border none, '
                       f'resize set {w} {h}, move absolute position {x} {y}')
        elif fmt == "hyprland":
            lua = re_literal(title).replace("\\", "\\\\").replace('"', '\\"')
            out.append(f'hl.window_rule({{ match = {{ title = "^{lua}$" }}, float = true, border_size = 0, '
                       f'size = {{{w}, {h}}}, move = {{{x}, {y}}} }})')
        elif fmt == "hyprlang":
            out += ["windowrule {", f"    name = nova-{re.sub(r'[^\w-]', '_', cid)}",
                    f"    match:title = ^{re_literal(title).replace('#', '##')}$",
                    "    float = yes", f"    move = {x} {y}", f"    size = {w} {h}", "    border_size = 0", "}", ""]
        else:
            out += [f"[nova-{cid}]", f"Description={title}", f"title={title}", "titlematch=1",
                    f"position={x},{y}", "positionrule=3", f"size={w},{h}", "sizerule=3",
                    "noborder=true", "noborderrule=2", ""]
    return "\n".join(out).rstrip() + "\n"


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
              + (f"\n{'':<{w}}       opts: {mask_url(c.opts)}" if c.opts else ""))
    if defaults and defaults.get("opts"):
        print(f"\ndefault opts: {mask_url(defaults.get('opts'))}")
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
        address = control_address(cam.id)
        run = launch_argv(cam, defaults, args.extra, args.config, address, record, layout)
        with open_log(cam.id) as log:
            log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} nova {'record' if record else 'start'} "
                      f"{cam.id} ({mode}) =====\n".encode())
            log.flush()
            kw = {"cwd": HERE, "env": process_env(env, gpu), "stdout": log, "stderr": subprocess.STDOUT,
                  "stdin": subprocess.DEVNULL}
            if WINDOWS:
                # no console of its own: closing the terminal no longer takes the cameras with it;
                # stop goes through the control channel
                kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            else:
                kw["start_new_session"] = True
            p = subprocess.Popen(run, **kw)
        pids[cam.id] = {"pid": p.pid, "mode": mode, "gpu": gpu, "layout": args.layout, "started": int(time.time()),
                        "control": address}
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
    return 1 if stop_entries(pids, targets, default_timeout(args, env, targets)) else 0


def default_timeout(args, env, targets):
    """--timeout as given, else what the slowest of these cameras may need to stop."""
    if args.timeout is not None:
        return args.timeout
    return stop_timeout(env, any("stream" in str(e.get("mode", "")) for e in targets.values()))


def cmd_restart(defaults, env, cams, args):
    pids = read_pids()
    ids = args.ids or list(pids)
    if not ids:
        print("no launched cameras (use `nova start` or `nova record`)")
        return 1
    select(cams, ids, only_enabled=False)                   # unknown ids end here
    targets = {cid: pids[cid] for cid in ids if cid in pids}
    for _, layout in restart_groups(ids, targets, args.layout):
        if layout:
            load_layout(layout)                             # a missing layout ends here, before any stop
    stuck = stop_entries(pids, targets, default_timeout(args, env, targets)) if targets else []
    if stuck:
        print(f"not restarted (still running): {', '.join(stuck)}; `nova restart {' '.join(stuck)}` once they exit")
    rc = 1 if stuck else 0
    for (record, layout), group in restart_groups([i for i in ids if i not in stuck], targets, args.layout).items():
        rc = cmd_start(defaults, env, cams, Args(group, args.extra, layout, config=args.config), record=record) or rc
    return rc


def cmd_status(defaults, env, cams, args):
    pids = read_pids()
    if not pids:
        print("no launched cameras (use `nova start` or `nova record`)")
        return
    w = max(len(k) for k in pids)
    for cid, e in pids.items():
        running = alive(e["pid"])
        state = "running" if running else "exited "
        up = f"up {fmt_uptime(time.time() - e['started'])}" if running and e.get("started") else ""
        gpu = f"gpu {e['gpu']}" if e.get("gpu") is not None else ""
        lay = f"layout {e['layout']}" if e.get("layout") else ""
        live = live_state(control(e.get("control"), {"cmd": "status"})) if running else ""
        print(f"{cid:<{w}}  pid {e['pid']:<8} {state}  {up:<14} {e['mode']:<16} {gpu:<6} {lay:<18} logs/{cid}.log"
              + (f"\n{'':<{w}}  {live}" if live else ""))


def live_state(st):
    """One line from a camera's status reply: what it is doing now, not how it was launched."""
    if not st or not st.get("ok"):
        return ""
    parts = [f"video {st['video']}" if st.get("video") else "no video yet"]
    for k in ("record", "stream"):
        if st.get(k) not in (None, "off", "n/a"):
            parts.append(f"{k} {st[k]}")
    if st.get("status"):
        parts.append(" ".join(str(st["status"]).split()))
    return ", ".join(parts)


def cmd_run(defaults, env, cams, args):
    cid = args.id or (defaults.get("camera") if defaults else None)
    if cid:
        cam = select(cams, [cid], only_enabled=False)[0]
    else:
        enabled = [c for c in cams if c.enabled]
        if not enabled:
            sys.exit("no enabled camera in cameras.ini")
        cam = enabled[0]
    layout = load_layout(args.layout) if args.layout else None
    argv = command(cam, defaults, args.extra, layout=layout)
    gpu = assign_gpu(cam, cams, env, argv)
    address = control_address(cam.id)
    print(f"running {cam.id}" + (f" on gpu {gpu}" if gpu is not None else "") + ": "
          + " ".join(mask_url(a) for a in argv[2:]), file=sys.stderr)
    p = subprocess.Popen(launch_argv(cam, defaults, args.extra, args.config, address, layout=layout),
                         cwd=HERE, env=process_env(env, gpu))
    try:
        return p.wait()
    except KeyboardInterrupt:
        # Ctrl-C from the terminal reaches nova.py as well; a SIGINT aimed only at the launcher is
        # forwarded.  Either way nova.py gets time to save its state (subprocess.call would kill it
        # after 0.25 s).
        if p.poll() is None:
            try:
                interrupt(p.pid, address)
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
    if sub == "apply":
        if len(args.ids) != 2:
            sys.exit("usage: nova layout apply NAME")
        return layout_apply(load_layout(args.ids[1]), args.ids[1])
    if sub == "export":
        if len(args.ids) != 3:
            sys.exit(f"usage: nova layout export NAME {'|'.join(EXPORT_FORMATS)}")
        sys.stdout.write(export_rules(load_layout(args.ids[1]), args.ids[1], cams, args.ids[2]))
        return
    if sub != "save" or len(args.ids) != 2:
        sys.exit("usage: nova layout save NAME | nova layout list | nova layout apply NAME"
                 " | nova layout export NAME sway|kwin|hyprland|hyprlang")
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


def layout_apply(layout, name):
    """Move the open windows to the layout's places (and zoom) without restarting them."""
    pids = read_pids()
    moved, rc = 0, 0
    for cid, e in layout["cameras"].items():
        entry = pids.get(cid)
        if not entry or not alive(entry["pid"]) or entry.get("mode", "").startswith("headless"):
            print(f"  {cid:<20} not open")
            continue
        reply = control(entry.get("control"), {"cmd": "place", "geometry": e.get("geometry"), "zoom": e.get("zoom")})
        if reply and reply.get("ok"):
            moved += 1
            print(f"  {cid:<20} {e.get('geometry')}")
        else:
            rc = 1
            why = (reply or {}).get("error") or "no control channel (started by an older Nova: restart it)"
            print(f"  {cid:<20} not moved: {why}")
    print(f"layout {name!r}: {moved} window(s) moved")
    return rc


COMMANDS = {"list": cmd_list, "start": cmd_start, "record": cmd_record, "stop": cmd_stop, "restart": cmd_restart,
            "status": cmd_status, "run": cmd_run, "layout": cmd_layout}


def take_option(extra, name):
    """Remove `--name VALUE` / `--name=VALUE` from extra (launcher option, not for nova.py); return VALUE or None."""
    for i, t in enumerate(extra):
        if t == name:
            if i + 1 >= len(extra):
                sys.exit(f"{name} needs a value")
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
    if not WINDOWS:
        os.umask(umask())        # logs, pid file and layouts are the user's own (nova.py does the same)
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

    args = Args(*split_ids(tokens), config=config)
    if cmd in ("stop", "restart"):
        t, _ = take_option(args.extra, "--timeout")
        if t is not None:
            try:
                args.timeout = float(t)
            except ValueError:
                sys.exit(f"--timeout needs seconds, not {t!r}")
    if cmd in ("start", "run", "record", "restart"):
        args.layout, _ = take_option(args.extra, "--layout")
        if cmd == "record" and args.layout:
            print("note: --layout is ignored by record (no windows)"); args.layout = None
    if cmd == "run":
        if len(args.ids) > 1:
            sys.exit("run takes one camera id")
        args.id = args.ids[0] if args.ids else None

    defaults, env, cams = load(config)
    if cmd in ("start", "record", "stop", "restart"):
        with pids_locked():
            return COMMANDS[cmd](defaults, env, cams, args)
    return COMMANDS[cmd](defaults, env, cams, args)


if __name__ == "__main__":
    sys.exit(main() or 0)
