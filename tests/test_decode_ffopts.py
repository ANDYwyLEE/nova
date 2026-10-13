"""--ffmpeg keys valid for both the camera input and an output muxer stay on the input."""
from ffparser import FFmpegParser


def test_camera_headers_and_timeouts_stay_on_the_input():
    o = FFmpegParser.parse('-headers "Authorization: Basic YWRtaW46cHc=" -timeout 3000000 -rw_timeout 3000000 '
                           '-f hls -hls_time 2 -method PUT')
    assert o.open == {"headers": "Authorization: Basic YWRtaW46cHc=", "timeout": "3000000", "rw_timeout": "3000000"}
    # the HLS ingest gets neither the camera's credentials nor a microsecond value as its seconds timeout
    assert o.mux == {"f": "hls", "hls_time": "2", "method": "PUT"}
    assert "headers" not in o.enc_v and "timeout" not in o.enc_v


def test_input_protocol_keys():
    o = FFmpegParser.parse("-tcp_nodelay 1 -rtmp_live live -streamid x")
    assert o.open == {"tcp_nodelay": "1", "rtmp_live": "live", "streamid": "x"}
    assert o.mux == {}


def test_keys_meant_for_both_sides_still_reach_both():
    o = FFmpegParser.parse("-http_persistent 0")
    assert o.open == {"http_persistent": "0"} and o.mux == {"http_persistent": "0"}
