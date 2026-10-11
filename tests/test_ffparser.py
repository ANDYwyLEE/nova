from ffparser import FFmpegParser


def test_empty():
    o = FFmpegParser.parse("")
    assert o.enc_v == {} and o.enc_a == {} and o.open == {} and o.mux == {}
    assert o.v_encoder is None and o.fmt is None and o.ignored == []


def test_routing_and_stream_specifiers():
    o = FFmpegParser.parse('-rtsp_transport udp -b:v 4M -b:a 128k -g 60 -movflags +faststart '
                           '-c:v h264_nvenc -c:a aac -f flv -vf "scale=640:-1" -metadata title=Front '
                           '-metadata comment=x=y -t 10 -re -vtag hvc1 -ar 16000 -ac 1 -an')
    assert o.open["rtsp_transport"] == "udp"
    assert o.enc_v["b"] == "4M" and o.enc_v["g"] == "60"
    assert o.enc_a == {"b": "128k", "ar": "16000", "ac": "1"}
    assert o.mux["movflags"] == "+faststart"
    assert o.v_encoder == "h264_nvenc" and o.a_encoder == "aac"
    assert o.fmt == "flv" and o.mux["f"] == "flv"
    assert o.vf == "scale=640:-1"
    assert o.metadata == {"title": "Front", "comment": "x=y"}
    assert o.v_tag == "hvc1"
    assert o.no_audio is True
    assert set(o.ignored) == {"-t", "-re"}


def test_negative_numbers_are_values_not_options():
    o = FFmpegParser.parse("-crf -1 -qp 0")
    assert o.enc_v["crf"] == "-1" and o.enc_v["qp"] == "0"


def test_unknown_key_goes_to_the_video_encoder():
    o = FFmpegParser.parse("-spatial_aq 1 -preset p4")
    assert o.enc_v == {"spatial_aq": "1", "preset": "p4"}
