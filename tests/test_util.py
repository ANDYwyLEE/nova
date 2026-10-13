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
    # an '@' inside the password: FFmpeg splits at the last '@', so must the mask
    ("rtsp://admin:Admin@123@h:554/x", "rtsp://admin:***@h:554/x"),
    ("rtsp://me@mail.com:pw@h/x", "rtsp://me@mail.com:***@h/x"),
    ("rtsp://:pw@h/", "rtsp://:***@h/"),
    ("rtsp://admin:pa/ss@h:554/x", "rtsp://admin:***@h:554/x"),          # '/' in the password, unencoded
    ("rtsp://admin:a/b@c@h/x", "rtsp://admin:***@h/x"),
    ("http://h:8080/snap?email=a@b.c", "http://h:***@b.c"),              # over-masks, never under-masks
    # XM paths separate with '_', other key names, SRT secrets
    ("rtsp://h/user=admin_password=Xy7_channel=1_stream=0.sdp", "rtsp://h/user=admin_password=***_channel=1_stream=0.sdp"),
    ("rtsp://h/user=a&psw=abc&ch=1", "rtsp://h/user=a&psw=***&ch=1"),
    ("srt://h:9000?streamid=#!::r=live/K,m=publish&passphrase=longsecret1&latency=200",
     "srt://h:9000?streamid=***&passphrase=***&latency=200"),
    # dict / argv forms in debug lines (--ffmpeg "-passphrase x")
    ("{'password': 'x1', 'b': 1}", "{'password': '***', 'b': 1}"),
    ("['-passphrase', 'x2', '-pbkeylen', '32']", "['-passphrase', '***', '-pbkeylen', '32']"),
    ('--ffmpeg "-passphrase x3 -pbkeylen 32"', '--ffmpeg "-passphrase *** -pbkeylen 32"'),
    # any rtmp(s) stream key: the last path element after the app
    ("rtmps://live-api-s.facebook.com:443/rtmp/FB-123-0-abc", "rtmps://live-api-s.facebook.com:443/rtmp/***"),
    ("rtmp://live.restream.io/live/re_123?x=1", "rtmp://live.restream.io/live/***?x=1"),
    ("rtsp://u:p@h/x and rtsp://v:q@k/y", "rtsp://u:***@h/x and rtsp://v:***@k/y"),
    # nothing to hide
    ("rtmp://h/app", "rtmp://h/app"),
    ("rtsp://user@h:554/x", "rtsp://user@h:554/x"),
    ("compass=1 bypass=2", "compass=1 bypass=2"),
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
