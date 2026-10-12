"""AVWriter helpers that need no container: bitrate parsing and the AAC bitrate cap."""
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
