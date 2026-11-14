"""The launcher end to end, with a fake nova.py: start/status/stop through the control channel, stale pid
entries, the lock, restart, list masking, cameras.ini edge cases, and no secrets on the command line."""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

import pytest

import cams

FAKE_NOVA = r'''
import json, os, signal, socket, sys, threading, time
argv = sys.argv[1:]
addr = argv[argv.index("--control") + 1] if "--control" in argv else None
print("fake nova", " ".join(argv), flush=True)
stop = threading.Event()
signal.signal(signal.SIGINT, lambda *a: stop.set())
srv = None
if addr and os.name != "nt":
    os.makedirs(os.path.dirname(addr), exist_ok=True)
    if os.path.exists(addr):
        os.unlink(addr)
    srv = socket.socket(socket.AF_UNIX); srv.bind(addr); srv.listen(); srv.settimeout(0.1)
while not stop.is_set():
    if srv is None:
        time.sleep(0.1); continue
    try:
        c, _ = srv.accept()
    except socket.timeout:
        continue
    req = json.loads(c.makefile().readline())
    reply = {"ok": True, "video": "640x360", "record": "on", "stream": "n/a", "status": ""}
    if req["cmd"] == "stop":
        reply = {"ok": True}; stop.set()
    c.sendall((json.dumps(reply) + "\n").encode()); c.close()
print("fake nova: bye", flush=True)
'''

INI = """\
[defaults]
python = PYTHON_EXE
stagger = 0
opts = -l ERROR

[a]
url = rtsp://admin:S3cretA@10.0.0.1:554/s
opts = -t rtmp://a.rtmp.youtube.com/live2/abcd-efgh-ijkl-mnop

[b]
url = rtsp://10.0.0.2:554/user=x&password=S3cretB&channel=1
""".replace("PYTHON_EXE", sys.executable)

posix_only = pytest.mark.skipif(os.name == "nt", reason="the fake camera serves a Unix socket")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A launcher directory of its own: fake nova.py, cameras.ini, pid file, logs, sockets."""
    (tmp_path / "nova.py").write_text(FAKE_NOVA)
    ini = tmp_path / "cameras.ini"
    ini.write_text(INI)
    if os.name != "nt":
        os.chmod(ini, 0o600)
    for name, value in (("HERE", tmp_path), ("INI", ini), ("PIDS", tmp_path / ".nova-pids.json"),
                        ("PIDS_LOCK", tmp_path / ".nova-pids.lock"), ("LOGDIR", tmp_path / "logs"),
                        ("LAYOUTS", tmp_path / "layouts")):
        monkeypatch.setattr(cams, name, str(value))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("DISPLAY", ":99")                  # `start` refuses windows in a session without a display
    monkeypatch.setattr(cams, "EARLY_EXIT_GRACE", 0.3)
    monkeypatch.setattr(cams, "START_ANSWER_S", 0.3)      # the fake camera answers on POSIX only, and not in every test
    yield tmp_path
    for e in cams.read_pids().values():                 # never leave a fake camera behind
        try:
            os.kill(e["pid"], 9)
        except OSError:
            pass


def run(home, *argv):
    return cams.main(["-c", str(home / "cameras.ini"), *argv])


def wait_gone(pid, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cams.cmdline(pid) is None:
            return True
        time.sleep(0.05)
    return False


def test_start_refuses_xmeye_without_python_dvr(home, capsys, monkeypatch):
    (home / "cameras.ini").write_text(INI + "\n[x]\nurl = xmeye://admin:S3cretX@10.0.0.9\n")
    monkeypatch.setattr(cams, "has_module", lambda python, name: name != "dvrip")
    assert run(home, "start", "x") == 1
    out = capsys.readouterr().out
    assert "x: refused - xmeye:// needs python-dvr" in out and "-m pip install python-dvr" in out
    assert "S3cretX" not in out and not cams.read_pids()
    with pytest.raises(SystemExit) as e:
        run(home, "run", "x")
    assert "refused - xmeye:// needs python-dvr" in str(e.value)


@posix_only
def test_start_without_a_display_says_so_and_record_still_runs(home, capsys, monkeypatch):
    """`nova start` over ssh on a server: every camera aborted inside Qt, and the log tail advised
    installing libxcb-cursor0 (which was there) and reinstalling the application."""
    import util
    monkeypatch.setattr(cams, "no_display", lambda env: util.no_display(env))
    monkeypatch.setattr(util, "LINUX", True)
    monkeypatch.delenv("DISPLAY"); monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False); monkeypatch.delenv("NOVA_QT_PLATFORM", raising=False)
    assert run(home, "start", "a", "b") == 1
    out = capsys.readouterr().out
    assert "a: refused - no display in this session (DISPLAY and WAYLAND_DISPLAY are not set)" in out
    assert "`nova record`" in out and "b: refused - no display in this session\n" in out and not cams.read_pids()
    assert run(home, "record", "a") == 0 and set(cams.read_pids()) == {"a"}       # no window: nothing to refuse
    run(home, "stop")
    assert util.no_display({}) and not util.no_display({"DISPLAY": ":0"}) and not util.no_display({"WAYLAND_DISPLAY": "wayland-0"})
    assert not util.no_display({"QT_QPA_PLATFORM": "eglfs"}) and not util.no_display({"NOVA_QT_PLATFORM": "offscreen"})
    assert util.no_display({"QT_QPA_PLATFORM": "xcb"})
    monkeypatch.setattr(util, "LINUX", False)
    assert not util.no_display({})                        # Windows and macOS always have a window system


def test_has_module_asks_the_interpreter():
    assert cams.has_module(sys.executable, "sys")
    assert not cams.has_module(sys.executable, "no_such_module_for_nova")
    assert not cams.has_module(os.path.join(os.sep, "no", "such", "python"), "sys")


@posix_only
def test_start_status_stop_through_the_control_channel(home, capsys):
    assert run(home, "start", "a", "b") == 0
    pids = cams.read_pids()
    assert set(pids) == {"a", "b"} and all(e.get("control") for e in pids.values())
    for e in pids.values():                               # the URL and the stream key stay off argv
        cl = cams.cmdline(e["pid"])
        assert "--camera" in cl and "S3cret" not in cl and "live2" not in cl
    for e in pids.values():                               # wait for the fake sockets
        end = time.time() + 5
        while time.time() < end and not os.path.exists(e["control"]):
            time.sleep(0.05)
    capsys.readouterr()
    run(home, "status")
    out = capsys.readouterr().out
    assert out.count("video 640x360, record on") == 2
    assert run(home, "stop") == 0
    assert all(wait_gone(e["pid"]) for e in pids.values())
    assert cams.read_pids() == {}
    assert "fake nova: bye" in (home / "logs" / "a.log").read_text()     # stopped, not killed


@posix_only
def test_a_stale_pid_is_not_a_camera(home, capsys):
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        cams.write_pids({"a": {"pid": other.pid, "mode": "window", "started": 1}})
        assert not cams.alive(other.pid)                    # alive, but not nova.py
        run(home, "stop", "a")
        assert "a: not running" in capsys.readouterr().out
        assert other.poll() is None                         # left alone
        assert run(home, "start", "a") == 0                 # and it does not block the camera
        assert "already running" not in capsys.readouterr().out
        run(home, "stop", "a")
    finally:
        other.kill()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc")
def test_a_kernel_thread_with_a_camera_s_old_pid_is_not_a_camera(monkeypatch):
    """A kernel thread has no command line. An empty one was read as "cannot tell", so a pid file from
    before a reboot could make `start` say "already running" about a kernel thread (and a test that
    looked at a process in the middle of its exec failed on a faster machine)."""
    import io
    proc = {"/proc/4242/stat": b"4242 (kworker/3:1) I 2 0 0 0 -1 69238880 0 0 0 0 0 0", "/proc/4242/cmdline": b"",
            "/proc/4243/stat": b"4243 (python) S 1 0 0 0 -1 4194304 0 0 0 0 0 0",
            "/proc/4243/cmdline": b"/usr/bin/python3\0-u\0/home/u/nova/nova.py\0--camera\0a\0"}

    def fake_open(path, mode="r", *a, **k):
        if path in proc:
            return io.BytesIO(proc[path])
        raise FileNotFoundError(path)
    monkeypatch.setattr(cams, "open", fake_open, raising=False)
    monkeypatch.setattr(cams.os, "kill", lambda pid, sig: None)
    assert cams.cmdline(4242) == cams.NO_CMDLINE and not cams.alive(4242)      # read, and not Nova's
    assert "nova.py" in cams.cmdline(4243) and cams.alive(4243)
    assert cams.cmdline(4244) is None and not cams.alive(4244)                 # no such process


def test_unreadable_pid_file_stops_instead_of_starting_everything_again(home):
    (home / ".nova-pids.json").write_text("{not json")
    with pytest.raises(SystemExit) as e:
        cams.read_pids()
    assert "unreadable" in str(e.value)
    cams.write_pids({"x": {"pid": 1, "mode": "window"}})
    assert json.loads((home / ".nova-pids.json").read_text())["x"]["pid"] == 1
    assert not (home / ".nova-pids.json.tmp").exists()


def test_one_command_at_a_time(home):
    order = []
    with cams.pids_locked():
        t = threading.Thread(target=lambda: (cams.pids_locked().__enter__(), order.append("second")))
        t.start()
        time.sleep(0.3)
        order.append("first")
    t.join(5)
    assert order == ["first", "second"]


@posix_only
def test_restart_with_a_missing_layout_stops_nothing(home):
    assert run(home, "start", "a") == 0
    pid = cams.read_pids()["a"]["pid"]
    entries = cams.read_pids()
    entries["a"]["layout"] = "gone"
    cams.write_pids(entries)
    with pytest.raises(SystemExit) as e:
        run(home, "restart", "a")
    assert "no layout 'gone'" in str(e.value)
    assert cams.alive(pid)
    run(home, "stop", "a")


def test_list_masks_options(home, capsys):
    run(home, "list")
    out = capsys.readouterr().out
    assert "S3cret" not in out and "abcd-efgh" not in out and "live2/***" in out


def test_config_edge_cases(tmp_path, capsys):
    ini = tmp_path / "c.ini"
    ini.write_text("﻿[a]\nURL = rtsp://h/x\nEnabled = no\nenable = no\n\n[bad/id]\nurl = rtsp://h/y\n"
                   "\n[front door]\nurl = rtsp://h/z\n",
                   encoding="utf-8")
    _, _, cs = cams.load(str(ini))
    err = capsys.readouterr().err
    assert [c.id for c in cs] == ["a", "front door"] and cs[0].enabled is False   # BOM, key case, spaces
    assert "unknown key 'enable'" in err and "[bad/id] skipped" in err
    ini.write_text("[a]\nurl = rtsp://u:S3cret@h/x\nurl = rtsp://u:S3cret@h/y\n")
    with pytest.raises(SystemExit) as e:
        cams.load(str(ini))
    assert "S3cret" not in str(e.value)
    ini.write_text("[a]\nurl = rtsp://h/x\nenabled = nope\n")
    with pytest.raises(SystemExit):
        cams.load(str(ini))


@pytest.mark.parametrize("argv", [("record", "--help"), ("start", "a", "-h"), ("restart", "--help"),
                                  ("run", "a", "--help"), ("stop", "-h")])
def test_help_after_a_command_starts_nothing(home, capsys, argv):
    assert run(home, *argv) == 0                    # was: every camera started with --help passed on
    assert "nova record [ID ...]" in capsys.readouterr().out and not cams.read_pids()
    assert not (home / "logs").exists()


def test_options_need_values():
    with pytest.raises(SystemExit):
        cams.take_option(["--layout"], "--layout")


def test_launch_argv_carries_no_secrets(home):
    defaults, _, cs = cams.load(str(home / "cameras.ini"))
    a = cs[0]
    full = cams.command(a, defaults, ["-r"])
    run_argv = cams.launch_argv(a, defaults, ["-r"], str(home / "cameras.ini"), "/run/x.sock")
    assert "S3cretA" in " ".join(full) and cams.mode_of(full) == "window+rec+stream"
    assert not any("S3cret" in t or "live2" in t for t in run_argv)
    assert run_argv[3:9] == ["--camera", "a", "--config", str(home / "cameras.ini"), "--control", "/run/x.sock"]


@posix_only
def test_stop_and_restart_without_ids_leave_another_configuration_s_cameras_alone(home, capsys):
    """A second configuration's cameras were started for a test next to the wall; `nova -c test.ini stop`
    then stopped the wall as well: every launched camera was in one list."""
    other = home / "test.ini"
    other.write_text(INI.replace("[a]", "[t1]").replace("[b]", "[t2]"))
    if os.name != "nt":
        os.chmod(other, 0o600)
    assert run(home, "start", "a") == 0
    assert cams.main(["-c", str(other), "record", "t1", "t2"]) == 0
    pids = cams.read_pids()
    assert set(pids) == {"a", "t1", "t2"}
    assert os.path.samefile(pids["t1"]["config"], other) and os.path.samefile(pids["a"]["config"], home / "cameras.ini")
    capsys.readouterr()

    assert cams.main(["-c", str(other), "stop"]) == 0
    out = capsys.readouterr().out
    assert set(cams.read_pids()) == {"a"} and cams.alive(pids["a"]["pid"])
    assert "left alone, launched with another configuration: a" in out
    assert wait_gone(pids["t1"]["pid"]) and wait_gone(pids["t2"]["pid"])

    assert cams.main(["-c", str(other), "stop"]) is None           # nothing of its own left
    out = capsys.readouterr().out
    assert "no launched cameras" in out and "left alone" in out and cams.alive(pids["a"]["pid"])
    assert cams.main(["-c", str(other), "restart"]) == 1 and cams.alive(pids["a"]["pid"])
    capsys.readouterr()

    cams.main(["-c", str(other), "status"])                         # status shows them, and says whose they are
    out = capsys.readouterr().out
    assert "launched with another configuration" in out and "\na " in out
    assert cams.main(["-c", str(other), "stop", "a"]) == 0          # by name, any launched camera
    assert wait_gone(pids["a"]["pid"]) and cams.read_pids() == {}


@posix_only
def test_cameras_launched_before_the_list_named_a_configuration_are_cameras_ini_s(home):
    assert run(home, "start", "a") == 0
    pids = cams.read_pids()
    del pids["a"]["config"]                                         # as an older version wrote it
    cams.write_pids(pids)
    assert run(home, "stop") == 0
    assert wait_gone(pids["a"]["pid"]) and cams.read_pids() == {}


@posix_only
def test_a_camera_that_dies_while_it_loads_did_not_start(home, capsys, monkeypatch):
    """On an Atom of 2010 Python and Qt took three seconds to load: a camera that an illegal instruction
    ended after two was "1 running" for the launcher, whose 1.5 s were over, and `exited` in nova status."""
    monkeypatch.setattr(cams, "START_ANSWER_S", 20)
    (home / "nova.py").write_text("import sys, time\ntime.sleep(1.0)\nsys.exit(3)\n")       # dies after the grace period
    t0 = time.time()
    assert run(home, "start", "a") == 1
    out = capsys.readouterr().out
    assert "a: exited with code 3 right after start" in out and "0 running, 1 failed" in out, out
    assert time.time() - t0 < 10 and not cams.read_pids()

    (home / "nova.py").write_text(FAKE_NOVA)              # one that answers is running as soon as it does
    t0 = time.time()
    assert run(home, "start", "a") == 0
    assert "1 running" in capsys.readouterr().out and time.time() - t0 < 10
    run(home, "stop", "a")


# ---------------------------------------------------------------- what a killed camera leaves behind
def wait_dead(pid, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if not cams.alive(pid):
            return True
        time.sleep(0.05)
    return False


@posix_only
def test_a_killed_camera_s_socket_goes_when_the_camera_is_started_again(home, capsys):
    """kill -9, or the kernel when memory runs out: the camera cannot remove its socket.  `nova start` gave
    the camera a new one, and no entry named the old file any more: 13 cameras killed and started again
    left 13 sockets that nothing would ever remove."""
    assert run(home, "start", "b") == 0
    old = cams.read_pids()["b"]
    assert os.path.exists(old["control"])
    os.kill(old["pid"], signal.SIGKILL)
    assert wait_dead(old["pid"]) and os.path.exists(old["control"])          # what a kill leaves
    run(home, "status")
    assert "exited" in capsys.readouterr().out and os.path.exists(old["control"])   # status only looks
    assert run(home, "start", "b") == 0
    new = cams.read_pids()["b"]
    assert new["control"] != old["control"] and os.path.exists(new["control"])
    assert not os.path.exists(old["control"])
    run(home, "stop")
    assert not os.path.exists(new["control"]) and not cams.read_pids()


@posix_only
def test_sockets_nobody_listens_on_are_swept_and_the_others_are_left(home):
    folder = os.path.dirname(cams.control_address("x"))
    os.makedirs(folder, exist_ok=True)
    made = []

    def sock(name, listen):
        path = os.path.join(folder, name)
        s = socket.socket(socket.AF_UNIX)
        s.bind(path)
        if listen:
            s.listen()
        made.append(path)
        return s, path

    hour_ago = time.time() - 3600
    try:
        s, dead = sock("t-dead.sock", False)             # its camera was killed long ago
        s.close()
        live_s, live = sock("t-live.sock", True)         # another checkout's camera, which answers
        s, fresh = sock("t-fresh.sock", False)           # a camera that is starting: bound a moment ago
        s.close()
        notes = os.path.join(folder, "t-notes.sock")     # not a socket at all
        open(notes, "w").close()
        made.append(notes)
        for path in (dead, live, notes):
            os.utime(path, (hour_ago, hour_ago))
        assert cams.listening(dead) is False and cams.listening(live) is True
        assert cams.sweep_controls({}) >= 1
        assert not os.path.exists(dead)
        assert os.path.exists(live) and os.path.exists(fresh) and os.path.exists(notes)
        # the folder an entry of the pid file names is looked at as well (XDG_RUNTIME_DIR may have changed since)
        elsewhere = tempfile.mkdtemp(prefix="nv", dir="/tmp")     # short: a socket's path holds 104 bytes on macOS
        made.append(elsewhere)
        s = socket.socket(socket.AF_UNIX)
        s.bind(os.path.join(elsewhere, "t-old.sock"))
        s.close()
        os.utime(os.path.join(elsewhere, "t-old.sock"), (hour_ago, hour_ago))
        assert cams.sweep_controls({"gone": {"pid": 1, "control": os.path.join(elsewhere, "g.sock")}}) == 1
        assert os.listdir(elsewhere) == []
        live_s.close()
    finally:
        for path in made:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.exists(path):
                os.remove(path)

