"""--ffmpeg routing: every key goes where FFmpeg defines an option of that name."""
import pytest

import ffopts_tables as T
import ffparser
from ffparser import FFmpegParser, PREFER, destinations, codec_index, for_codec


@pytest.fixture(autouse=True)
def static_tables(request, monkeypatch):
    """Route from the generated tables (the same on every platform); the PyAV build's own tables
    differ per wheel (no qsv/amf encoders on macOS, say) and are checked separately."""
    if "pyav_build" in request.keywords:
        monkeypatch.setattr(ffparser, "_index", None)
        return
    monkeypatch.setattr(ffparser, "_index", ffparser._Index(T.CODEC_GENERIC, T.DECODER_PRIVATE, T.ENCODER_PRIVATE,
                                                            f"tables of FFmpeg {T.FFMPEG_VERSION}"))


def test_empty():
    o = FFmpegParser.parse("")
    assert o.enc_v == {} and o.enc_a == {} and o.open == {} and o.mux == {} and o.dec == {} and o.hw == {}
    assert o.v_encoder is None and o.fmt is None and o.ignored == [] and o.warnings == [] and o.notes == []


def test_routing_and_stream_specifiers():
    o = FFmpegParser.parse('-rtsp_transport udp -b:v 4M -b:a 128k -g 60 -movflags +faststart '
                           '-c:v h264_nvenc -c:a aac -f flv -vf "scale=640:-1" -metadata title=Front '
                           '-metadata comment=x=y -t 10 -re -vtag hvc1 -ar 16000 -ac 1 -an')
    assert o.open == {"rtsp_transport": "udp"}
    assert o.enc_v == {"b": "4M", "g": "60"}
    assert o.enc_a == {"b": "128k", "ar": "16000", "ac": "1"}
    assert o.mux == {"movflags": "+faststart", "f": "flv"}
    assert o.dec == {} and o.hw == {}
    assert o.v_encoder == "h264_nvenc" and o.a_encoder == "aac"
    assert o.fmt == "flv" and o.in_fmt is None
    assert o.vf == "scale=640:-1"
    assert o.metadata == {"title": "Front", "comment": "x=y"}
    assert o.v_tag == "hvc1"
    assert o.no_audio is True
    assert o.ignored == ["-t", "-re"]
    assert any("-t is an option of the ffmpeg program" in w and "--record-interval" in w for w in o.warnings)


def test_negative_numbers_are_values_not_options():
    o = FFmpegParser.parse("-crf -1 -qp 0")
    assert o.enc_v["crf"] == "-1" and o.enc_v["qp"] == "0"


def test_unknown_key_goes_to_the_video_encoder_with_a_warning():
    o = FFmpegParser.parse("-bogus_opt 1 -preset p4")
    assert o.enc_v == {"bogus_opt": "1", "preset": "p4"}
    assert len(o.warnings) == 1 and "-bogus_opt" in o.warnings[0] and "video encoder" in o.warnings[0]


def test_generic_codec_options_follow_their_flags():
    # threads, flags: decoding and encoding parameters -> the decoder and the video encoder (not audio)
    o = FFmpegParser.parse("-threads 2 -flags +low_delay -skip_frame nokey -bf 0 -refs 3 -err_detect ignore_err")
    assert o.dec == {"threads": "2", "flags": "+low_delay", "skip_frame": "nokey", "err_detect": "ignore_err"}
    # skip_frame: a decoding parameter, but also a private option of the qsv/amf encoders (the writer
    # leaves it out for encoders that do not define it: for_codec)
    assert o.enc_v == {"threads": "2", "flags": "+low_delay", "skip_frame": "nokey", "bf": "0", "refs": "3",
                       "err_detect": "ignore_err"}
    assert o.enc_a == {}
    assert o.open == {"err_detect": "ignore_err"}       # also a demuxer option (AVFormatContext)


def test_audio_only_options_find_the_audio_encoder_without_a_specifier():
    o = FFmpegParser.parse("-ab 96k -cutoff 8000 -aac_coder fast -compression_level 5")
    assert o.enc_a == {"b": "96k", "cutoff": "8000", "aac_coder": "fast"}
    assert o.enc_v == {"compression_level": "5"}        # audio and video: video unless :a


def test_specifier_a_sends_anything_to_the_audio_encoder():
    o = FFmpegParser.parse("-profile:a aac_low -threads:a 1 -b:a 64k")
    assert o.enc_a == {"profile": "aac_low", "threads": "1", "b": "64k"}
    assert o.enc_v == {} and o.dec == {}


def test_private_decoder_and_encoder_options():
    o = FFmpegParser.parse("-resize 640x360 -surfaces 8 -deint adaptive -cq 28 -rc vbr -preset p5 -gpu 1")
    assert o.dec == {"resize": "640x360", "surfaces": "8", "deint": "adaptive", "gpu": "1"}
    assert o.enc_v == {"cq": "28", "rc": "vbr", "preset": "p5", "gpu": "1", "surfaces": "8"}   # gpu, surfaces: cuvid and nvenc
    assert o.open == {} and o.mux == {}


def test_demuxer_protocol_and_muxer_options_stay_on_their_side():
    o = FFmpegParser.parse("-rtsp_transport tcp -buffer_size 1000000 -tcp_nodelay 1 -rw_timeout 5000000 "
                           "-probesize 500000 -analyzeduration 0 -hls_time 2 -movflags +faststart "
                           "-avoid_negative_ts make_zero -flush_packets 1")
    assert o.open == {"rtsp_transport": "tcp", "buffer_size": "1000000", "tcp_nodelay": "1",
                      "rw_timeout": "5000000", "probesize": "500000", "analyzeduration": "0"}
    assert o.mux == {"hls_time": "2", "movflags": "+faststart", "avoid_negative_ts": "make_zero", "flush_packets": "1"}
    assert o.dec == {} and o.enc_v == {}


def test_generic_format_options_valid_on_both_sides_reach_both():
    o = FFmpegParser.parse("-fflags +nobuffer -max_delay 0 -strict experimental")
    assert o.open == {"fflags": "+nobuffer", "max_delay": "0", "strict": "experimental"}
    assert o.mux == {"fflags": "+nobuffer", "max_delay": "0", "strict": "experimental"}
    assert o.dec["strict"] == "experimental" and o.enc_v["strict"] == "experimental"   # a codec option too


def test_names_with_a_different_meaning_per_side():
    o = FFmpegParser.parse('-timeout 3000000 -headers "X: y" -method PUT -http_persistent 1')
    assert o.open == {"timeout": "3000000", "headers": "X: y", "http_persistent": "1"}
    assert o.mux == {"method": "PUT", "http_persistent": "1"}


def test_hardware_device_options_and_hwaccel_device():
    o = FFmpegParser.parse("-primary_ctx 1 -kernel_driver i915 -hwaccel_device 1 -extra_hw_frames 8")
    assert o.hw == {"primary_ctx": "1", "kernel_driver": "i915"}
    assert o.hw_device == "1"
    assert o.dec == {"extra_hw_frames": "8"} and o.enc_v == {}


def test_program_only_options_are_dropped_with_hints():
    o = FFmpegParser.parse("-map 0:v -q:v 5 -hwaccel cuda -fps_mode cfr -af volume=2 -filter:a volume=2 -n")
    assert o.enc_v == {} and o.enc_a == {} and o.dec == {} and o.open == {} and o.mux == {}
    assert o.ignored == ["-map", "-q:v", "-hwaccel", "-fps_mode", "-af", "-filter:a", "-n"]
    hints = " ".join(o.warnings)
    assert "-crf" in hints and "--accel" in hints and "-r N" in hints and "audio filters" in hints


def test_removed_options_are_not_taken_for_encoder_options():
    o = FFmpegParser.parse("-stimeout 5000000 -vsync 0")
    assert o.enc_v == {} and o.open == {}
    assert o.ignored == ["-stimeout", "-vsync"]
    assert "-timeout" in o.warnings[0] and "NOVA_TIMEOUT" in o.warnings[0]


def test_dash_i_splits_input_and_output_like_an_ffmpeg_command_line():
    o = FFmpegParser.parse("-f v4l2 -s 1280x720 -r 30 -pix_fmt yuyv422 -threads 1 -fflags +nobuffer "
                           "-i -threads 4 -b:v 2M -f mp4 -s 640x360 -r 10 -fflags +flush_packets")
    assert o.in_fmt == "v4l2" and o.fmt == "mp4"
    assert o.open == {"video_size": "1280x720", "framerate": "30", "pixel_format": "yuyv422", "fflags": "+nobuffer"}
    assert o.dec == {"threads": "1"}
    assert o.enc_v == {"threads": "4", "b": "2M", "s": "640x360", "r": "10"}
    assert o.mux == {"f": "mp4", "fflags": "+flush_packets"}
    assert o.notes == [] and o.warnings == []


def test_dash_i_with_a_url_and_misplaced_options():
    o = FFmpegParser.parse("-i rtsp://user:secret@cam/live -rtsp_transport udp -b:v 1M")
    assert o.open == {"rtsp_transport": "udp"} and o.enc_v == {"b": "1M"}
    assert len(o.notes) == 2
    assert "secret" not in o.notes[0] and "ignored" in o.notes[0]
    assert "-rtsp_transport is an input-side option" in o.notes[1] and "after -i" in o.notes[1]


def test_preferences_cover_every_name_defined_on_both_sides():
    inp = set(T.DEMUXER_PRIVATE) | set(T.PROTOCOL)
    both = inp & set(T.MUXER_PRIVATE)
    harmless = {"http_persistent", "start_number"}          # the same meaning on both sides
    assert both == set(PREFER) | harmless, f"decide a side for {both - set(PREFER) - harmless}"
    for name, side in PREFER.items():
        d = destinations(name)
        assert d and (d <= {"open", "dec", "hw"} if side == "in" else d <= {"enc_v", "enc_a", "mux"})


@pytest.mark.pyav_build
def test_static_codec_tables_match_the_pyav_build():
    """The generated tables and the PyAV wheel bundle the same FFmpeg: their codec option lists must
    agree, or the tables are stale (tools/gen_ffopts.py)."""
    av = pytest.importorskip("av")
    if tuple(av.library_versions.get("libavcodec", ())) != tuple(T.LIBAVCODEC):
        pytest.skip(f"PyAV has libavcodec {av.library_versions.get('libavcodec')}, the tables {T.LIBAVCODEC}")
    idx = codec_index()
    assert "PyAV" in idx.source
    # ffmpeg -h lists only options flagged for encoding or decoding; the rest are never routed
    assert {k: f for k, f in idx.generic.items() if f} == {k: f for k, f in T.CODEC_GENERIC.items() if f}
    for table, static in ((idx.dec, T.DECODER_PRIVATE), (idx.enc, T.ENCODER_PRIVATE)):
        for name, (flags, owners_) in table.items():
            if 'E' in flags or 'D' in flags:            # export-only options are not listed by ffmpeg -h
                assert name in static, f"{name} ({owners_}) is missing from ffopts_tables.py"


@pytest.mark.pyav_build
def test_options_of_other_codecs_are_left_out_for_the_codec_in_use():
    av = pytest.importorskip("av")
    from av.codec.context import CodecContext
    x264 = CodecContext.create("libx264", "w")
    apply, skipped = for_codec(x264, {"crf": "23", "cq": "28", "preset": "veryfast", "resize": "1x1", "bogus": "1"})
    assert apply == {"crf": "23", "preset": "veryfast", "bogus": "1"}     # bogus: nobody's, left for the report
    assert skipped == {"cq": "28", "resize": "1x1"}                         # nvenc's and cuvid's
    h264 = CodecContext.create("h264", "r")
    apply, skipped = for_codec(h264, {"skip_frame": "nokey", "resize": "640x360", "threads": "2"})
    assert apply == {"skip_frame": "nokey", "threads": "2"} and skipped == {"resize": "640x360"}
