"""The launcher's pure logic: cameras.ini parsing, id/option splitting, GPU dealing, layouts."""
import hashlib
import json
import sys

import pytest

import cams

INI = """\
[defaults]
opts = --opengl -f 30 -l ERROR
python = PYTHON_EXE
stagger = 0

[env]
NOVA_TIMEOUT = 5000000
NOVA_GPU_DEVICES = 0,1

[a]
url = rtsp://admin:pw@10.0.0.1:554/s

[b]
name = Camera B
url = rtsp://10.0.0.2:554/user=x&password=%41$y&channel=1
opts = -r --gpu 1

[c]
url = rtsp://admin:pw@10.0.0.3:554/s
enabled = no

[d]
url = rtsp://admin:pw@10.0.0.4:554/s

[nourl]
name = forgotten
""".replace("PYTHON_EXE", sys.executable)      # an absolute path on every OS


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "cameras.ini"
    p.write_text(INI, encoding="utf-8")
    return cams.load(str(p))


def test_load(tmp_path, capsys):
    p = tmp_path / "cameras.ini"
    p.write_text(INI, encoding="utf-8")
    defaults, env, cs = cams.load(str(p))
    assert [c.id for c in cs] == ["a", "b", "c", "d"]            # [nourl] skipped with a warning
    assert "no url" in capsys.readouterr().err
    assert env["NOVA_TIMEOUT"] == "5000000"                       # key case preserved
    assert "%41$y" in cs[1].url                                   # no interpolation of % and $
    assert cs[0].name == "a" and cs[1].name == "Camera B"
    assert [c.enabled for c in cs] == [True, True, False, True]
    assert cams.python_exe(defaults) == sys.executable
    assert defaults.get("stagger") == "0"


def test_missing_ini_exits(tmp_path):
    with pytest.raises(SystemExit):
        cams.load(str(tmp_path / "nope.ini"))


def test_select(cfg):
    _, _, cs = cfg
    assert [c.id for c in cams.select(cs, [], only_enabled=True)] == ["a", "b", "d"]
    assert [c.id for c in cams.select(cs, ["c"], only_enabled=True)] == ["c"]   # by id, even if disabled
    with pytest.raises(SystemExit):
        cams.select(cs, ["zzz"], only_enabled=False)


def test_command_and_mode(cfg):
    defaults, env, cs = cfg
    a, b = cs[0], cs[1]
    argv = cams.command(a, defaults, ["-t", "rtmp://x/live2/k"])
    assert argv[:2] == [sys.executable, "-u"] and argv[2].endswith("nova.py")
    assert argv[3:7] == ["--opengl", "-f", "30", "-l"]
    assert argv[-6:] == ["-n", "a", "-i", a.url, "-t", "rtmp://x/live2/k"]
    assert cams.mode_of(argv) == "window+stream"
    assert cams.mode_of(cams.command(a, defaults, [])) == "window"
    assert cams.mode_of(cams.command(a, defaults, [], record=True)) == "headless+rec"
    assert cams.mode_of(cams.command(b, defaults, [])) == "window+rec"
    assert cams.mode_of(cams.command(a, defaults, ["--headless"])) == "headless"     # refused by start


def test_gpu_dealing(cfg):
    defaults, env, cs = cfg
    a, b, c, d = cs
    assert cams.gpu_list(env) == ["0", "1"]
    assert cams.gpu_list({"NOVA_GPU_DEVICES": "x"}) == []
    plan = {cam.id: cams.assign_gpu(cam, cs, env, cams.command(cam, defaults, [])) for cam in cs}
    assert plan == {"a": "0", "b": "1", "c": "0", "d": "0"}       # a,b,d dealt in order; b pinned; c by file order
    assert cams.assign_gpu(a, cs, env, cams.command(a, defaults, ["--gpu=1"])) == "1"
    assert cams.assign_gpu(a, cs, {"NOVA_GPU_DEVICES": "0"}, cams.command(a, defaults, [])) is None
    assert cams.assign_gpu(a, cs, {}, cams.command(a, defaults, [])) is None
    e = cams.process_env({"NOVA_GPU_DEVICES": "0,1"}, "1")
    assert e["NOVA_GPU_DEVICES"] == "1"


def test_split_ids_and_take_option():
    assert cams.split_ids(["a", "b", "-r", "-t", "x"]) == (["a", "b"], ["-r", "-t", "x"])
    assert cams.split_ids(["--", "a"]) == ([], ["a"])
    assert cams.split_ids([]) == ([], [])
    extra = ["-r", "--layout", "night", "-l", "INFO"]
    assert cams.take_option(extra, "--layout")[0] == "night" and extra == ["-r", "-l", "INFO"]
    extra = ["--layout=work"]
    assert cams.take_option(extra, "--layout")[0] == "work" and extra == []
    assert cams.take_option(["-r"], "--layout") == (None, None)


def test_layouts(cfg, tmp_path, monkeypatch):
    defaults, env, cs = cfg
    monkeypatch.setattr(cams, "LAYOUTS", str(tmp_path / "layouts"))
    with pytest.raises(SystemExit):
        cams.layout_path("bad/name")
    assert cams.layout_path("work-1.v2").endswith("work-1.v2.json")
    with pytest.raises(SystemExit):
        cams.load_layout("missing")
    (tmp_path / "layouts").mkdir()
    (tmp_path / "layouts" / "night.json").write_text(json.dumps(
        {"saved": "2026-10-11T23:00:00", "cameras": {"a": {"geometry": "1,2,3,4", "zoom": "0,0,1,1"}, "zzz": {}}}))
    lay = cams.load_layout("night")
    assert cams.layout_names() == ["night"]
    assert cams.layout_flags(lay, cs[0]) == ["--geometry", "1,2,3,4", "--zoom", "0,0,1,1"]
    assert cams.layout_flags(lay, cs[1]) == []
    argv = cams.command(cs[0], defaults, ["-l", "DEBUG"], layout=lay)
    assert argv[-6:] == ["--geometry", "1,2,3,4", "--zoom", "0,0,1,1", "-l", "DEBUG"]


def test_window_title_and_export(cfg):
    defaults, env, cs = cfg
    assert cams.window_title(" Camera B ") == "Nova - Camera B" and cams.window_title("") == "Nova"
    assert cams.re_literal("a.b (1) [x]|y") == r"a\.b \(1\) \[x\]\|y" and cams.re_literal("front-desk 2") == "front-desk 2"
    lay = {"cameras": {"a": {"geometry": "300,760,391,220", "zoom": "0,0,1,1"}, "b": {"geometry": "0,0,10,10"},
                       "c": {}, "zzz": {"geometry": "1,1,1,1"}, "d": {"geometry": "junk"}}}
    sway = cams.export_rules(lay, "work", cs, "sway")
    assert ('for_window [title="^Nova - a$"] floating enable, border none, resize set 391 220, '
            'move absolute position 300 760') in sway
    assert 'for_window [title="^Nova - Camera B$"]' in sway and sway.count("for_window") == 2
    assert "zzz" not in sway and "junk" not in sway and sway.startswith("# Nova layout 'work'")
    kwin = cams.export_rules(lay, "work", cs, "kwin")
    assert ("[Nova - a]\nDescription=Nova - a\ntitle=Nova - a\ntitlematch=1\nposition=300,760\npositionrule=3\n"
            "size=391,220\nsizerule=3\nnoborder=true\nnoborderrule=2\n") in kwin
    hypr = cams.export_rules(lay, "work", cs, "hyprland")
    assert hypr.startswith("-- Nova layout")
    assert ('hl.window_rule({ match = { title = "^Nova - a$" }, float = true, border_size = 0, '
            'size = {391, 220}, move = {300, 760} })') in hypr
    hl = cams.export_rules(lay, "work", cs, "hyprlang")
    assert hl.startswith("# Nova layout") and hl.count("windowrule {") == 2
    assert ("windowrule {\n    name = nova-a\n    match:title = ^Nova - a$\n    float = yes\n    move = 300 200\n"
            "    size = 391 220\n    border_size = 0\n}\n").replace("move = 300 200", "move = 300 760") in hl
    with pytest.raises(SystemExit):
        cams.export_rules(lay, "work", cs, "gnome")


def test_uptime_and_restart_groups():
    assert cams.fmt_uptime(5) == "0:00:05" and cams.fmt_uptime(26 * 3600 + 61) == "1d 2:01:01"
    pids = {"a": {"pid": 1, "mode": "window", "layout": "work"}, "b": {"pid": 2, "mode": "headless+rec+stream"},
            "c": {"pid": 3, "mode": "window+rec"}}
    assert cams.restart_groups(["a", "b", "c", "d"], pids, None) == {
        (False, "work"): ["a"], (True, None): ["b"], (False, None): ["c", "d"]}
    assert cams.restart_groups(["a", "b"], pids, "night") == {(False, "night"): ["a"], (True, None): ["b"]}
    a = cams.Args(["x"], ["-r"], "work")
    assert (a.ids, a.extra, a.layout, a.timeout, a.id) == (["x"], ["-r"], "work", 15, None)


def test_url_hash_matches_the_view_key():
    url = "rtsp://admin:pw@10.0.0.1:554/s"
    assert cams.url_hash(url) == hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def test_pids_file_compat(tmp_path, monkeypatch):
    p = tmp_path / ".nova-pids.json"
    monkeypatch.setattr(cams, "PIDS", str(p))
    assert cams.read_pids() == {}
    p.write_text(json.dumps({"old": 123, "new": {"pid": 5, "mode": "window", "gpu": "1"}}))
    d = cams.read_pids()
    assert d["old"] == {"pid": 123, "mode": "?"} and d["new"]["gpu"] == "1"
    cams.write_pids(d)
    assert json.loads(p.read_text())["old"]["pid"] == 123


def test_log_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(cams, "LOGDIR", str(tmp_path))
    (tmp_path / "x.log").write_text("a\n\nb\nc\nd\ne\nf\n")
    assert cams.log_tail("x", 3) == ["d", "e", "f"]
    assert cams.log_tail("missing") == []
