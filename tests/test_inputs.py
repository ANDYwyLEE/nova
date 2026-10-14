"""Everything Nova parses from outside itself, fed what it must survive: cameras.ini values, layout
files, --geometry / --zoom / --record-interval / --ffmpeg / -vf strings, shortcut URIs and device
answers.  A bad value is refused with a message, never a traceback, and no secret leaks."""
import json
import logging
import os
import random
import sys
import time
from fractions import Fraction

import numpy as np
import pytest

import av
import av.logging

import auth
import cams
import ffparser
import util
from avwriter import AVWriter, _parse_filtergraph

NASTY = ",:/@%&=?#[]\"' \t\n0123456789abcxyz.-_\x00\x1bé\U0001f4f7"


def fuzz(n=300, seed=0, maxlen=40):
    rnd = random.Random(seed)
    for _ in range(n):
        yield "".join(rnd.choice(NASTY) for _ in range(rnd.randint(0, maxlen)))


# ---------------------------------------------------------------- geometry, zoom, names, intervals
@pytest.mark.parametrize("text, expect", [
    ("1,2,3,4", (1, 2, 3, 4)), (" 10,-20,640,360 ", (10, -20, 640, 360)), ("0,0,100000,100000", (0, 0, 100000, 100000)),
    ("1,2,3", None), ("1,2,3,4,5", None), ("a,b,c,d", None), ("1,2,0,4", None), ("1,2,3,-4", None),
    ("1,2,100001,4", None), ("99999999999,2,3,4", None), ("nan,2,3,4", None), ("", None), (None, None),
    ([1, 2, 3, 4], None), ({"x": 1}, None), ("1.5,2,3,4", None),
])
def test_parse_geom(text, expect):
    assert util.parse_geom(text) == expect


@pytest.mark.parametrize("text, expect", [
    ("0,0,1,1", (0.0, 0.0, 1.0, 1.0)), ("0.25,0.5,0.5,0.25", (0.25, 0.5, 0.5, 0.25)), ("0,0,0.001,0.001", (0.0, 0.0, 0.001, 0.001)),
    ("nan,0,1,1", None), ("0,inf,1,1", None), ("0,0,1e400,1", None), ("-0.1,0,1,1", None), ("1,0,1,1", None),
    ("0,0,0,1", None), ("0,0,1.5,1", None), ("0,0,1", None), ("x", None), ("", None), (None, None), ([0, 0, 1, 1], None),
])
def test_parse_zoom(text, expect):
    assert util.parse_zoom(text) == expect


def test_printable_strips_terminal_escapes():
    assert util.printable("\x1b[31mred\x1b[0m\n\r\x00 cam\x7f") == "red cam"
    assert util.printable("é 📷 Garage") == "é 📷 Garage"
    assert util.printable("x" * 500) == "x" * 200
    assert util.printable(None) == "" and util.printable(12) == "12"
    assert util.window_title("\x1b]0;evil\x07Front") == "Nova - Front"        # an OSC title-setting sequence
    assert util.printable("\x1b(Bplain\x1b7 \x9b31m") == "plain 31m"


@pytest.mark.parametrize("value, expect", [("30", 30), (30, 30), ("5m", 300), ("1h", 3600), ("90s", 90), (" 2M ", 120)])
def test_parse_time(value, expect):
    assert util.parse_time(value) == expect


@pytest.mark.parametrize("value", ["1d", "", "-5", "5 m", None, "m", "1.5h", "0x10", [], "9" * 40 + "x"])
def test_parse_time_refuses(value):
    with pytest.raises(ValueError):
        util.parse_time(value)


# ---------------------------------------------------------------- --ffmpeg and -vf
def test_ffmpeg_unbalanced_quotes_are_a_warning():
    o = ffparser.FFmpegParser.parse('-rtsp_transport "tcp')
    assert any("cannot read" in w for w in o.warnings) and not o.open and not o.enc_v and not o.dec


def test_ffmpeg_string_never_raises():
    for s in fuzz(400, seed=1, maxlen=60):
        ffparser.FFmpegParser.parse(s)
    ffparser.FFmpegParser.parse(" ".join(f"-k{i} v{i}" for i in range(5000)))     # many tokens


@pytest.mark.parametrize("vf, expect", [
    ("transpose=1,scale=640:-2", [("transpose", "1"), ("scale", "640:-2")]),
    ("drawbox=x=10:y=10:w=100:h=100:color=red@0.5", [("drawbox", "x=10:y=10:w=100:h=100:color=red@0.5")]),
    ("scale='min(iw,640)':-2,hflip", [("scale", "'min(iw,640)':-2"), ("hflip", None)]),
    ("crop=iw/2:ih/2:0:0,  null ,", [("crop", "iw/2:ih/2:0:0"), ("null", None)]),
    ("", []), (",,,", []),
])
def test_parse_filtergraph(vf, expect):
    assert _parse_filtergraph(vf) == expect


def test_parse_filtergraph_never_raises():
    for s in fuzz(300, seed=2):
        _parse_filtergraph(s)


def frame(i=0):
    f = av.VideoFrame.from_ndarray(np.zeros((64, 64, 3), np.uint8), format="rgb24").reformat(format="yuv420p")
    f.pts, f.time_base, f.color_range, f.colorspace = i * 3000, Fraction(1, 90000), 2, 1
    return f


def test_vf_graph_keeps_colour_properties_and_refuses_unknown_filters():
    from types import SimpleNamespace
    me = SimpleNamespace(vf="scale=32:32", v_time_base=Fraction(1, 90000))
    seen = []
    h = logging.Handler(); h.emit = lambda r: seen.append(r.getMessage())
    lib = logging.getLogger("libav"); lib.addHandler(h)
    old_level, old_av = lib.level, av.logging.get_level()
    lib.setLevel(1); av.logging.set_level(av.logging.DEBUG)
    try:
        g = AVWriter._build_graph(me, frame())
        for i in range(3):
            g.push(frame(i)); out = g.pull()
    finally:
        lib.removeHandler(h); lib.setLevel(old_level); av.logging.set_level(old_av)
    assert (out.width, out.height, out.color_range, out.colorspace) == (32, 32, 2, 1)
    assert not any("Changing video frame properties" in m for m in seen)    # the buffer source got range and colorspace
    with pytest.raises(RuntimeError, match="not available"):
        AVWriter._build_graph(SimpleNamespace(vf="nosuchfilter_xyz=1", v_time_base=Fraction(1, 90000)), frame())


# ---------------------------------------------------------------- cameras.ini and layouts
def write_ini(tmp_path, text):
    p = tmp_path / "cameras.ini"
    p.write_text(text, encoding="utf-8")
    if os.name != "nt":
        os.chmod(p, 0o600)
    return str(p)


def test_names_and_ids_from_cameras_ini(tmp_path, capsys):
    long_id = "x" * (cams.MAX_ID_LEN + 1)
    ini = ("[defaults]\nopts = -l ERROR\n\n[front]\nname = \x1b[31mFront\x1b[0m\tdoor\nurl = rtsp://u:S3cret@10.0.0.1/s\n"
           f"\n[{long_id}]\nurl = rtsp://10.0.0.2/s\n\n[bad\x01id]\nurl = rtsp://10.0.0.3/s\n\n[ok-id.2]\nurl = rtsp://10.0.0.4/s\n")
    _, _, cs = cams.load(write_ini(tmp_path, ini))
    assert [c.id for c in cs] == ["front", "ok-id.2"]
    assert cs[0].name == "Front\tdoor".replace("\t", "")       # control characters gone
    err = capsys.readouterr().err
    assert "exceed 64 bytes" in err and "\x1b" not in err and "\x01" not in err and "S3cret" not in err


def test_enabled_and_broken_ini_say_what_is_wrong(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        cams.load(write_ini(tmp_path, "[a]\nurl = rtsp://u:S3cret@h/s\nenabled = maybe\n"))
    assert "enabled = 'maybe'" in str(e.value)
    with pytest.raises(SystemExit) as e:
        cams.load(write_ini(tmp_path, "[a]\nurl = rtsp://u:S3cret@h/s\n[a]\nurl = rtsp://u:S3cret@h/t\n"))
    assert "S3cret" not in str(e.value) and "a" in str(e.value)        # the parser's message quotes lines: masked
    with pytest.raises(SystemExit):
        cams.load(write_ini(tmp_path, "no section header\nurl = x\n"))


def test_layout_entries_are_checked(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cams, "LAYOUTS", str(tmp_path))
    cam = cams.Camera("a", {"url": "rtsp://h/s"})
    (tmp_path / "bad.json").write_text(json.dumps({"cameras": {"a": {"geometry": [1, 2, 3, 4], "zoom": "nan,0,1,1"}}}))
    assert cams.layout_flags(cams.load_layout("bad"), cam) == []
    err = capsys.readouterr().err
    assert "geometry" in err and "zoom" in err
    (tmp_path / "list.json").write_text(json.dumps({"cameras": {"a": [1, 2]}}))
    assert cams.layout_flags(cams.load_layout("list"), cam) == []
    (tmp_path / "ok.json").write_text(json.dumps({"cameras": {"a": {"geometry": "1,2,300,200", "zoom": "0.1,0.1,0.5,0.5"}}}))
    assert cams.layout_flags(cams.load_layout("ok"), cam) == ["--geometry", "1,2,300,200", "--zoom", "0.1,0.1,0.5,0.5"]
    for text in ("[]", '{"cameras": 3}', "{", ""):
        (tmp_path / "x.json").write_text(text)
        with pytest.raises(SystemExit):
            cams.load_layout("x")
    for name in ("../x", "a/b", "", "x y", "a\x00b"):
        with pytest.raises(SystemExit):
            cams.layout_path(name)


def test_env_numbers_fall_back(monkeypatch, capsys):
    monkeypatch.setenv("NOVA_UMASK", "zz")
    assert cams.umask() == 0o077
    assert cams.gpu_list({"NOVA_GPU_DEVICES": "0,x"}) == [] and cams.gpu_list({"NOVA_GPU_DEVICES": " 1, 0 "}) == ["1", "0"]
    assert cams.stop_timeout({"NOVA_TIMEOUT": "lots"}) == cams.stop_timeout({"NOVA_TIMEOUT": "10000000"})


# ---------------------------------------------------------------- control channel requests
def test_control_requests_are_checked():
    from types import SimpleNamespace
    import control
    fake = SimpleNamespace(status=lambda: {"pid": 1}, quit_fn=lambda: None, place=lambda g, z: {"ok": True, "g": g, "z": z})
    handle = control.ControlServer.handle
    assert handle(fake, "not a dict") == {"ok": False, "error": "unknown command None"}
    assert handle(fake, {"cmd": "format"}) == {"ok": False, "error": "unknown command 'format'"}
    assert handle(fake, {"cmd": "status"}) == {"ok": True, "pid": 1}
    assert handle(fake, {"cmd": "place", "geometry": "1,2,3,4"}) == {"ok": True, "g": "1,2,3,4", "z": None}


# ---------------------------------------------------------------- shortcut URIs, device answers, masking
def test_uri_parsers_never_raise():
    info = auth.parse_uri("onvif://u:p@10.0.0.5", None)
    for s in fuzz(400, seed=3, maxlen=60):
        auth.parse_uri(s, 80)
        auth.parse_uri("hik://" + s, 80)
        auth._check_stream_uri(s, info)
        auth._check_stream_uri("rtsp://" + s, info)
        auth._device_port(s, "[t]", "ip")
        util.mask_url(s)
        auth.resolve_input_uri(s) if "://" not in s else None        # plain text is returned as it is


def test_masking_a_long_line_is_quick():
    line = ("rtsp://admin:S3cret@10.0.0.1/x?password=S3cret&token=T " * 20000)
    t0 = time.monotonic()
    out = util.mask_url(line)
    assert time.monotonic() - t0 < 2.0 and "S3cret" not in out and "token=T" not in out
