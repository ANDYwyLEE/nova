"""AVWriter helpers that need no real container: bitrate parsing, the AAC bitrate cap, network deadlines, open errors."""
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


class Container:
    def __init__(self, error):
        self.error, self.muxed = error, []

    def mux(self, packet):
        if self.error: raise self.error
        self.muxed.append(packet)


def test_a_failed_first_packet_is_an_open_error_later_ones_are_not():
    w = avwriter.AVWriter.__new__(avwriter.AVWriter)
    w.first_packet_sent, w.timeout = False, (30.0, 10.0)
    w.container = Container(ConnectionRefusedError(111, "Connection refused"))
    try:
        w._write("p1")
        assert False, "no OutputOpenError"
    except avwriter.OutputOpenError as e:
        assert "Connection refused" in str(e)
    w.container.error = None
    w._write("p1")
    assert w.first_packet_sent and w.container.muxed == ["p1"]
    w.container.error = BrokenPipeError(32, "Broken pipe")
    try:
        w._write("p2")
        assert False, "no BrokenPipeError"
    except BrokenPipeError:
        pass
