"""AVWriter helpers that need no real container: bitrate parsing, the AAC bitrate cap, network deadlines, open errors,
the video dts guard."""
import logging
from types import SimpleNamespace

import avwriter


def test_parse_bitrate():
    assert avwriter.parse_bitrate("48k") == 48000
    assert avwriter.parse_bitrate("0.5M") == 500000
    assert avwriter.parse_bitrate(" 48000 ") == 48000
    assert avwriter.parse_bitrate("lots") is None


class Ctx:
    def __init__(self, **options):
        self.options = options
        self.bit_rate = 0


def writer(rate, channels, **opts):
    w = avwriter.AVWriter.__new__(avwriter.AVWriter)
    w.sample_rate, w.channels, w.enc_a_opts = rate, channels, dict(opts)
    return w


def test_aac_cap_applies_only_above_the_encoder_limit():
    ctx = Ctx()
    writer(8000, 1)._cap_aac_bitrate(ctx, "mono")               # FFmpeg's default 69k > 6*8000 = 48k
    assert ctx.bit_rate == 48000
    ctx = Ctx()
    writer(16000, 1)._cap_aac_bitrate(ctx, "mono")              # 69k <= 96k: FFmpeg's default stays
    assert ctx.bit_rate == 0
    ctx = Ctx(b="128k")
    writer(8000, 2, b="128k")._cap_aac_bitrate(ctx, "stereo")   # explicit -b:a above the 96k cap
    assert ctx.bit_rate == 96000 and "b" not in ctx.options
    ctx = Ctx(b="128k")
    writer(48000, 2, b="128k")._cap_aac_bitrate(ctx, "stereo")  # explicit and fine: untouched
    assert ctx.bit_rate == 0 and ctx.options["b"] == "128k"
    ctx = Ctx(b="lots")
    writer(8000, 1, b="lots")._cap_aac_bitrate(ctx, "mono")     # not a number: FFmpeg reports it
    assert ctx.bit_rate == 0 and ctx.options["b"] == "lots"


def test_network_targets_get_pyav_deadlines_not_protocol_options(monkeypatch):
    monkeypatch.setenv("NOVA_TIMEOUT", "10000000")
    assert avwriter._net_timeouts("rtmp://a.rtmp.youtube.com/live2/KEY") == (30.0, 10.0)
    assert avwriter._net_timeouts("srt://host:9000?latency=200000") == (30.0, 10.0)
    assert avwriter._net_timeouts("videos/cam/out.mp4") is None
    # PyAV opens outputs with avio_open2(..., NULL): protocol options would only be reported unused
    assert avwriter._build_conn_options("flv", {"f": "flv", "flvflags": "no_duration_filesize"}) == \
        {"flvflags": "no_duration_filesize"}
    assert avwriter._build_conn_options("hls", {}) == {"method": "POST", "timeout": "10000000us"}
    assert avwriter._build_conn_options("flv", {}, "rtmp://a.rtmp.youtube.com/live2/k") == {"rtmp_flush_interval": "1"}
    assert avwriter._build_conn_options("flv", {}, "/tmp/x.flv") == {}


class Container:
    def __init__(self, error):
        self.error, self.muxed = error, []

    def mux(self, packet):
        if self.error: raise self.error
        self.muxed.append(packet)


def test_a_failed_first_packet_is_an_open_error_later_ones_are_not():
    w = avwriter.AVWriter.__new__(avwriter.AVWriter)
    w.first_packet_sent, w.open_failed, w.timeout, w.v_stream = False, None, (30.0, 10.0), None
    w.container = Container(ConnectionRefusedError(111, "Connection refused"))
    try:
        w._write("p1")
        assert False, "no OutputOpenError"
    except avwriter.OutputOpenError as e:
        assert "Connection refused" in str(e)
    w = avwriter.AVWriter.__new__(avwriter.AVWriter)    # (that writer is done: see test_output_openfail)
    w.first_packet_sent, w.open_failed, w.timeout, w.v_stream = False, None, (30.0, 10.0), None
    w.container = Container(None)
    w._write("p1")
    assert w.first_packet_sent and w.container.muxed == ["p1"]
    w.container.error = BrokenPipeError(32, "Broken pipe")
    try:
        w._write("p2")
        assert False, "no BrokenPipeError"
    except BrokenPipeError:
        pass


def test_video_dts_is_kept_increasing(caplog):
    """A muxer refuses a packet whose dts does not increase, and the packet is lost. Quick Sync with
    B-frames makes such packets from a camera's uneven frame times: the dts is moved, like ffmpeg does."""
    caplog.set_level(logging.DEBUG)
    avwriter.AVWriter._dts_warned.discard("h264_test")
    vs, other = object(), object()
    w = avwriter.AVWriter.__new__(avwriter.AVWriter)
    w.first_packet_sent, w.open_failed, w.timeout, w.v_stream = True, None, None, vs
    w._last_v_dts, w._dts_fixed, w.v_encoder, w.container = None, 0, "h264_test", Container(None)
    P = lambda pts, dts, stream=vs: SimpleNamespace(pts=pts, dts=dts, stream=stream)
    for p in (P(0, -9000), P(27000, 0), P(9000, 9000), P(18000, 18000),
              P(54000, 12000),            # dts behind the previous one: moved, its pts is still ahead
              P(18000, 18000),            # equal to the dts before that: moved, and the pts with it
              P(5, 5, other),             # audio: not touched
              P(36000, 36000), P(45000, None)):
        w._write(p)
    video = [(p.pts, p.dts) for p in w.container.muxed if p.stream is vs]
    assert video == [(0, -9000), (27000, 0), (9000, 9000), (18000, 18000), (54000, 18001), (18002, 18002),
                     (36000, 36000), (45000, None)]
    assert [(p.pts, p.dts) for p in w.container.muxed if p.stream is other] == [(5, 5)]
    assert w._dts_fixed == 2
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1 and "h264_test" in warns[0] and "12000 after 18000" in warns[0] and "-bf 0" in warns[0]

    w2 = avwriter.AVWriter.__new__(avwriter.AVWriter)      # the next segment's writer: no second warning
    w2.first_packet_sent, w2.open_failed, w2.timeout, w2.v_stream = True, None, None, vs
    w2._last_v_dts, w2._dts_fixed, w2.v_encoder, w2.container = None, 0, "h264_test", Container(None)
    for p in (P(0, 0), P(9000, 0)):
        w2._write(p)
    assert w2._dts_fixed == 1 and len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
