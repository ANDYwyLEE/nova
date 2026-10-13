"""The launcher end to end, with a fake nova.py: start/status/stop through the control channel, stale pid
entries, the lock, restart, list masking, cameras.ini edge cases, and no secrets on the command line."""
import json
import os
import subprocess
import sys
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
    monkeypatch.setattr(cams, "EARLY_EXIT_GRACE", 0.3)
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
    ini.write_text("﻿[a]\nURL = rtsp://h/x\nEnabled = no\nenable = no\n\n[bad id]\nurl = rtsp://h/y\n",
                   encoding="utf-8")
    _, _, cs = cams.load(str(ini))
    err = capsys.readouterr().err
    assert [c.id for c in cs] == ["a"] and cs[0].enabled is False      # BOM and key case are fine
    assert "unknown key 'enable'" in err and "[bad id] skipped" in err
    ini.write_text("[a]\nurl = rtsp://u:S3cret@h/x\nurl = rtsp://u:S3cret@h/y\n")
    with pytest.raises(SystemExit) as e:
        cams.load(str(ini))
    assert "S3cret" not in str(e.value)
    ini.write_text("[a]\nurl = rtsp://h/x\nenabled = nope\n")
    with pytest.raises(SystemExit):
        cams.load(str(ini))


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
