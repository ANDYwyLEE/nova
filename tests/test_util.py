import pytest

from util import mask_url, display_url


@pytest.mark.parametrize("raw, masked", [
    ("rtsp://admin:s3cret@192.168.1.10:554/stream_0", "rtsp://admin:***@192.168.1.10:554/stream_0"),
    ("rtsp://192.168.1.24:554/user=andy&password=abc&channel=1", "rtsp://192.168.1.24:554/user=andy&password=***&channel=1"),
    # whole log lines, as the logging filter sees them
    ("open failed: 'rtsp://u:pw@h/x'; retry", "open failed: 'rtsp://u:***@h/x'; retry"),
    # stream keys
    ("rtmp://a.rtmp.youtube.com/live2/abcd-efgh-ijkl", "rtmp://a.rtmp.youtube.com/live2/***"),
    ("rtmp://b.rtmp.youtube.com/live2?backup=1/abcd-efgh", "rtmp://b.rtmp.youtube.com/live2?backup=1/***"),
    ("https://a.upload.youtube.com/http_upload_hls?cid=abcd-efgh&copy=0&file=temp.m3u8",
     "https://a.upload.youtube.com/http_upload_hls?cid=***&copy=0&file=temp.m3u8"),
    ("rtmp://live.twitch.tv/app/live_123_abc", "rtmp://live.twitch.tv/app/***"),
    # nothing to hide
    ("rtsp://192.168.1.10:554/stream_0", "rtsp://192.168.1.10:554/stream_0"),
    ("http://h/app/other", "http://h/app/other"),
    ("0", "0"),
    ("", ""),
])
def test_mask_url(raw, masked):
    assert mask_url(raw) == masked


def test_mask_url_none():
    assert mask_url(None) == ""


def test_display_url_drops_credentials():
    assert display_url("rtsp://u:pw@h:554/x") == "h:554/x"
    assert display_url("0") == "camera 0"
