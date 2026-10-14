"""onvif:// hik:// dahua:// xmeye:// resolution against canned device answers (a local HTTP server):
device-reported ports and URIs never steer the password elsewhere, XML is unescaped, credentials are
decoded/encoded, fallbacks are not cached, TRACE logs carry no WS-Security secrets, stop is honoured."""
import base64
import hashlib
import json
import logging
import os
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import auth


@pytest.fixture(autouse=True)
def cache_file(tmp_path, monkeypatch):
    path = tmp_path / "uri_cache.json"
    monkeypatch.setattr(auth, "CACHE_FILE", str(path))      # never the checkout's .uri_cache.json
    monkeypatch.setattr(auth, "_sleep", lambda s, stop: None)
    return path


class Device:
    """A fake camera web server: answer(handler, body) -> (status, headers, text)."""
    def __init__(self, answer):
        self.answer = answer
        self.requests = []
        dev = self

        class H(BaseHTTPRequestHandler):
            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode() if n else ""
                dev.requests.append(SimpleNamespace(method=self.command, path=self.path,
                                                    headers=dict(self.headers), body=body))
                status, headers, text = dev.answer(self, body)
                data = text.encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            do_GET = do_POST = _do
            def log_message(self, *a): pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def device():
    made = []
    def make(answer):
        d = Device(answer)
        made.append(d)
        return d
    yield make
    for d in made:
        d.close()


def digest_only(ok_text, paths=None):
    """HTTP Digest like a Hikvision / Dahua / Axis camera: 401 with a challenge until Digest credentials
    come.  paths: {path: text} answered by path (query string ignored); other paths 404."""
    def answer(h, body):
        if not h.headers.get("Authorization", "").startswith("Digest "):
            return 401, {"WWW-Authenticate": 'Digest realm="cam", nonce="abc", qop="auth"'}, ""
        if paths is None:
            return 200, {}, ok_text
        text = paths.get(h.path.split("?")[0])
        return (200, {}, text) if text is not None else (404, {}, "")
    return answer


# ---------------------------------------------------------------- parsing
def test_parse_uri_credentials_and_hosts():
    i = auth.parse_uri("onvif://admin:Admin@123@10.0.0.5:8080/1", None)
    assert (i["user"], i["pwd"], i["ip"], i["port"], i["stream_idx"]) == ("admin", "Admin@123", "10.0.0.5", 8080, 1)
    i = auth.parse_uri("onvif://admin:Admin%40123@10.0.0.5", None)
    assert i["pwd"] == "Admin@123" and i["port"] is None and i["stream_idx"] == 0
    i = auth.parse_uri("hik://ad%3Amin:p/w@[fe80::1]:80/0", 80)
    assert (i["user"], i["pwd"], i["ip"], i["host"], i["port"]) == ("ad:min", "p/w", "fe80::1", "[fe80::1]", 80)
    assert auth.parse_uri("onvif://admin:pw@10.0.0.5:99999", None) is None
    assert auth.parse_uri("onvif://admin:pw@10.0.0.5:80x", None) is None
    assert auth.parse_uri("onvif://10.0.0.5", None) is None


@pytest.mark.parametrize("tail, channel, given, idx", [
    ("", 1, False, 0), ("/", 1, False, 0), ("/0", 1, False, 0), ("/main", 1, False, 0), ("/sub", 1, False, 1),
    ("/third", 1, False, 2), ("/2", 1, False, 2), ("/7", 1, False, 7),          # 7: an ONVIF profile index
    ("/3/1", 3, True, 1), ("/12/main", 12, True, 0), ("/3/1/", 3, True, 1),
])
def test_parse_uri_channel_and_stream(tail, channel, given, idx):
    i = auth.parse_uri(f"hik://u:p@10.0.0.5{tail}", 80)
    assert (i["channel"], i["channel_given"], i["stream_idx"]) == (channel, given, idx), tail


@pytest.mark.parametrize("tail", ["/x", "/0/1", "/a/1", "/1/x", "/1/2/3", "/1/1234/"])
def test_parse_uri_rejects_bad_channel_or_stream(tail):
    assert auth.parse_uri(f"hik://u:p@10.0.0.5{tail}", 80) is None, tail


def test_shortcut_schemes_and_aliases():
    assert auth.shortcut_scheme("HIK://u:p@h") == "hik"
    assert auth.shortcut_scheme("amcrest://u:p@h") == "dahua" and auth.shortcut_scheme("annke://u:p@h") == "hik"
    assert auth.shortcut_scheme("rtsp://u:p@h/x") is None and auth.shortcut_scheme("0") is None
    assert auth.resolve_input_uri("rtsp://u:p@h/x") == "rtsp://u:p@h/x"            # not a shortcut: untouched
    assert set(auth._RESOLVERS) == set(auth.SHORTCUT_DEFAULT_PORTS)


def test_ipv6_brackets_are_kept():
    assert auth.clean_rtsp_554_url("rtsp://u:p@[fe80::1]:554/s") == "rtsp://u:p@[fe80::1]/s"
    assert auth.clean_rtsp_554_url("rtsps://[fe80::1]:554/s") == "rtsps://[fe80::1]:554/s"
    info = auth.parse_uri("onvif://u:p@[fe80::1]", None)
    assert auth._check_stream_uri("rtsp://[FE80:0::1]:8554/x", info) == "rtsp://[fe80:0::1]:8554/x"   # same host
    assert auth._check_stream_uri("rtsp://192.0.2.7/x", info) == "rtsp://[fe80::1]/x"
    assert auth._points_to_camera("rtsp://u:p@[fe80::1]:8554/x", info)


@pytest.mark.parametrize("uri, expect", [
    ("rtsp://10.0.0.5:554/a?x=1&amp;y=2", "rtsp://10.0.0.5/a?x=1&y=2"),
    ("<![CDATA[rtsp://10.0.0.5/a?x=1&y=2]]>", "rtsp://10.0.0.5/a?x=1&y=2"),
    ("rtsps://10.0.0.5:322/a", "rtsps://10.0.0.5:322/a"),
    ("rtsp://192.168.1.10:8554/a", "rtsp://10.0.0.5:8554/a"),          # other host: back to the camera
    ("rtsp://10.0.0.5:554@198.51.100.9:80/a", None),                  # '@': credentials go elsewhere
    ("rtsp://user@10.0.0.5/a", None),
    ("http://10.0.0.5/video.mjpg", None),
    ("file:///etc/passwd", None),
    ("rtsp://10.0.0.5:0/a", None),
    ("rtsp://10.0.0.5:70000/a", None),
])
def test_device_stream_uris_are_checked(uri, expect):
    assert auth._check_stream_uri(uri, auth.parse_uri("onvif://u:p@10.0.0.5", None)) == expect


def test_device_ports_must_be_port_numbers():
    assert auth._device_port("8554", "[t]", "ip") == (8554, True)
    assert auth._device_port(" 554 ", "[t]", "ip") == (554, True)
    assert auth._device_port(None, "[t]", "ip") == (554, True)
    for bad in ("554@127.0.0.1:9", "0", "65536", "-1", "55a", "５５４"):
        assert auth._device_port(bad, "[t]", "ip") == (554, False), bad


# ---------------------------------------------------------------- Hikvision
def hik_channels(*ids):
    """/ISAPI/Streaming/channels as a camera (101, 102) or an NVR (101, 102, 201, ...) lists it."""
    items = "".join(f"<StreamingChannel version=\"2.0\"><id>{i}</id><channelName>Camera</channelName>"
                    "<Transport><ControlProtocolList><ControlProtocol><streamingTransport>RTSP</streamingTransport>"
                    "</ControlProtocol></ControlProtocolList></Transport></StreamingChannel>" for i in ids)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<StreamingChannelList version="2.0" '
            f'xmlns="http://www.hikvision.com/ver20/XMLSchema">{items}</StreamingChannelList>')

def hik_access(rtsp_port, enabled="true"):
    """/ISAPI/Security/adminAccesses: the one place ISAPI reports the RTSP port."""
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<AdminAccessProtocolList version="2.0" '
            'xmlns="http://www.hikvision.com/ver20/XMLSchema">'
            '<AdminAccessProtocol version="2.0"><id>1</id><enabled>true</enabled><protocol>HTTP</protocol><portNo>80</portNo></AdminAccessProtocol>'
            f'<AdminAccessProtocol version="2.0"><id>3</id><enabled>{enabled}</enabled><protocol>RTSP</protocol><portNo>{rtsp_port}</portNo></AdminAccessProtocol>'
            '</AdminAccessProtocolList>')

def hik_device(device, rtsp_port="554", ids=(101, 102), access=True, enabled="true"):
    paths = {"/ISAPI/Streaming/channels": hik_channels(*ids)}
    if access:
        paths["/ISAPI/Security/adminAccesses"] = hik_access(rtsp_port, enabled)
    return device(digest_only(None, paths))


def test_hikvision_port_from_the_device(device, cache_file):
    d = hik_device(device, "8554")
    uri = f"hik://admin:p%40ss/w@127.0.0.1:{d.port}/0"
    url = auth.resolve_input_uri(uri)
    assert url == "rtsp://admin:p%40ss%2Fw@127.0.0.1:8554/Streaming/Channels/101"
    assert [r.path for r in d.requests if r.headers.get("Authorization", "").startswith("Digest")] == \
        ["/ISAPI/Streaming/channels", "/ISAPI/Security/adminAccesses"]
    cached = json.loads(cache_file.read_text())
    assert "p@ss" not in cache_file.read_text() and "p%40ss" not in cache_file.read_text()
    assert list(cached.values())[0]["url"].startswith("rtsp://admin:***@127.0.0.1:8554/")
    assert auth.resolve_input_uri(uri) == url                       # from the cache, password restored


def test_hikvision_hostile_port_is_rejected(device, cache_file):
    d = hik_device(device, "554@127.0.0.1:9")
    url = auth.resolve_input_uri(f"hik://admin:pw@127.0.0.1:{d.port}/0")
    assert url == "rtsp://admin:pw@127.0.0.1/Streaming/Channels/101"
    assert not cache_file.exists()                                  # a fallback is not cached


def test_hikvision_without_admin_accesses_assumes_554(device, caplog):
    d = hik_device(device, access=False)
    assert auth.resolve_input_uri(f"hik://admin:pw@127.0.0.1:{d.port}/1") == \
        "rtsp://admin:pw@127.0.0.1/Streaming/Channels/102"


def test_hikvision_nvr_channels(device, caplog):
    d = hik_device(device, "10554", ids=(101, 102, 201, 202, 203))
    assert auth.resolve_input_uri(f"hik://admin:pw@127.0.0.1:{d.port}/2/third") == \
        "rtsp://admin:pw@127.0.0.1:10554/Streaming/Channels/203"
    with caplog.at_level(logging.ERROR):
        assert auth.resolve_input_uri(f"hik://admin:pw@127.0.0.1:{d.port}/3/0") is None
    assert "no stream 301" in caplog.text and "101, 102, 201" in caplog.text
    assert auth.resolve_input_uri(f"hik://admin:pw@127.0.0.1:{d.port}/1/5") is None     # no such stream


def test_hikvision_warns_when_rtsp_is_off(device, caplog):
    d = hik_device(device, "554", enabled="false")
    with caplog.at_level(logging.WARNING):
        assert auth.resolve_input_uri(f"hik://admin:pw@127.0.0.1:{d.port}").endswith("/Streaming/Channels/101")
    assert "RTSP is switched off" in caplog.text


def test_hikvision_never_sends_basic_auth(device):
    d = device(lambda h, body: (401, {"WWW-Authenticate": 'Basic realm="cam"'}, ""))
    assert auth.resolve_input_uri(f"hik://admin:secret@127.0.0.1:{d.port}/0") is None
    sent = [r.headers.get("Authorization", "") for r in d.requests]
    assert sent and not any(a.startswith("Basic") for a in sent)


def test_annke_is_hikvision(device):
    d = hik_device(device, "8554")
    assert auth.resolve_input_uri(f"annke://admin:pw@127.0.0.1:{d.port}") == \
        "rtsp://admin:pw@127.0.0.1:8554/Streaming/Channels/101"


# ---------------------------------------------------------------- Dahua
def test_dahua_hostile_port_and_encoding(device, cache_file):
    d = device(digest_only("table.RTSP.Enable=true\r\ntable.RTSP.Port=554@evil.example:80\r\n"))
    url = auth.resolve_input_uri(f"dahua://admin:Adm%40in@127.0.0.1:{d.port}/1")
    assert url == "rtsp://admin:Adm%40in@127.0.0.1/cam/realmonitor?channel=1&subtype=1"
    assert not cache_file.exists()


def test_dahua_port_and_channels(device):
    d = device(digest_only("table.RTSP.Port=10554\r\n"))
    assert auth.resolve_input_uri(f"dahua://admin:pw@127.0.0.1:{d.port}") == \
        "rtsp://admin:pw@127.0.0.1:10554/cam/realmonitor?channel=1&subtype=0"
    assert d.requests[-1].path == "/cgi-bin/configManager.cgi?action=getConfig&name=RTSP"
    assert auth.resolve_input_uri(f"amcrest://admin:pw@127.0.0.1:{d.port}/4/2") == \
        "rtsp://admin:pw@127.0.0.1:10554/cam/realmonitor?channel=4&subtype=2"
    assert auth.resolve_input_uri(f"dahua://admin:pw@127.0.0.1:{d.port}/4/3") is None


# ---------------------------------------------------------------- Reolink
def reolink_device(device, rtsp_port=554, rtsp_enable=1, channels=1, rtsp_url=True, login_ok=True, vtype="h264"):
    """The JSON API of a Reolink camera / NVR on plain HTTP (HTTPS is tried first and fails here)."""
    def answer(h, body):
        q = dict(p.split("=", 1) for p in h.path.split("?", 1)[1].split("&"))
        cmd = q["cmd"]
        if cmd == "Login":
            if not login_ok:
                return 200, {}, json.dumps([{"cmd": "Login", "code": 1, "error": {"detail": "login failed", "rspCode": -7}}])
            return 200, {}, json.dumps([{"cmd": "Login", "code": 0, "value": {"Token": {"leaseTime": 3600, "name": "T0kenValue"}}}])
        if q.get("token") != "T0kenValue":
            return 200, {}, json.dumps([{"cmd": cmd, "code": 1, "error": {"detail": "please login first", "rspCode": -6}}])
        ch = json.loads(body)[0].get("param", {}).get("channel", 0)
        value = {
            "GetNetPort": {"NetPort": {"httpPort": 80, "httpsPort": 443, "rtspEnable": rtsp_enable, "rtspPort": rtsp_port,
                                       "onvifPort": 8000, "rtmpPort": 1935, "mediaPort": 9000}},
            "GetDevInfo": {"DevInfo": {"channelNum": channels, "model": "RLN8-410", "type": "NVR"}},
            "GetRtspUrl": {"rtspUrl": {"channel": ch, "mainStream": f"rtsp://192.0.2.9:{rtsp_port}/h264Preview_{ch + 1:02d}_main",
                                       "subStream": f"rtsp://192.0.2.9:{rtsp_port}/h264Preview_{ch + 1:02d}_sub"}} if rtsp_url else None,
            "GetEnc": {"Enc": {"channel": ch, "mainStream": {"vType": vtype}, "subStream": {"vType": "h264"}}},
            "Logout": {},
        }.get(cmd)
        if value is None:
            return 200, {}, json.dumps([{"cmd": cmd, "code": 1, "error": {"detail": "not support", "rspCode": -9}}])
        return 200, {}, json.dumps([{"cmd": cmd, "code": 0, "value": value}])
    return device(answer)


def test_reolink_url_from_the_device(device, cache_file, caplog):
    import logsync
    mask = logsync.CredentialMaskFilter()                          # Nova's handlers mask every record, urllib3's too
    caplog.handler.addFilter(mask)
    d = reolink_device(device, rtsp_port=8554)
    uri = f"reolink://admin:p%40ss@127.0.0.1:{d.port}"
    try:
        with caplog.at_level(logging.DEBUG):
            url = auth.resolve_input_uri(uri)
    finally:
        caplog.handler.removeFilter(mask)
    assert url == "rtsp://admin:p%40ss@127.0.0.1:8554/h264Preview_01_main"     # 192.0.2.9 pointed back at the camera
    cmds = [r.path.split("cmd=")[1].split("&")[0] for r in d.requests]
    assert cmds == ["Login", "GetNetPort", "GetDevInfo", "GetRtspUrl", "Logout"]
    assert all(r.path.startswith("/cgi-bin/api.cgi?") for r in d.requests)
    assert "token=T0kenValue" in "".join(r.path for r in d.requests)          # the API wants it in the query string
    assert "p@ss" not in caplog.text and "T0kenValue" not in caplog.text and "token=***" in caplog.text
    assert "plain HTTP" in caplog.text                                          # HTTPS failed, HTTP was used
    assert cache_file.exists() and auth.resolve_input_uri(uri) == url


def test_reolink_channels_and_sub_stream(device):
    d = reolink_device(device, channels=8)
    assert auth.resolve_input_uri(f"reolink://admin:pw@127.0.0.1:{d.port}/3/sub") == \
        "rtsp://admin:pw@127.0.0.1/h264Preview_03_sub"
    assert auth.resolve_input_uri(f"reolink://admin:pw@127.0.0.1:{d.port}/9/0") is None    # 8 channels
    assert auth.resolve_input_uri(f"reolink://admin:pw@127.0.0.1:{d.port}/2") is None      # no third stream


def test_reolink_old_firmware_builds_the_path(device, cache_file):
    d = reolink_device(device, rtsp_url=False, vtype="h265")
    assert auth.resolve_input_uri(f"reolink://admin:pw@127.0.0.1:{d.port}/main") == \
        "rtsp://admin:pw@127.0.0.1/h265Preview_01_main"
    assert auth.resolve_input_uri(f"reolink://admin:pw@127.0.0.1:{d.port}/sub") == \
        "rtsp://admin:pw@127.0.0.1/h264Preview_01_sub"


def test_reolink_login_refused_and_rtsp_off(device, caplog):
    d = reolink_device(device, login_ok=False)
    with caplog.at_level(logging.ERROR):
        assert auth.resolve_input_uri(f"reolink://admin:pw@127.0.0.1:{d.port}") is None
    assert "login refused" in caplog.text and "rspCode -7" in caplog.text
    d = reolink_device(device, rtsp_enable=0)
    with caplog.at_level(logging.WARNING):
        assert auth.resolve_input_uri(f"reolink://admin:pw@127.0.0.1:{d.port}") is not None
    assert "RTSP is switched off" in caplog.text


def test_reolink_token_is_masked_in_urls():
    from util import mask_url
    assert mask_url("http://10.0.0.5/cgi-bin/api.cgi?cmd=GetNetPort&token=T0kenValue") == \
        "http://10.0.0.5/cgi-bin/api.cgi?cmd=GetNetPort&token=***"


# ---------------------------------------------------------------- Axis
def test_axis_port_and_camera_number(device, cache_file):
    d = device(digest_only("root.Network.RTSP.Port=8554\n"))
    assert auth.resolve_input_uri(f"axis://root:pw@127.0.0.1:{d.port}") == "rtsp://root:pw@127.0.0.1:8554/axis-media/media.amp"
    assert d.requests[-1].path == "/axis-cgi/param.cgi?action=list&group=Network.RTSP.Port"
    assert auth.resolve_input_uri(f"axis://root:pw@127.0.0.1:{d.port}/2/0") == \
        "rtsp://root:pw@127.0.0.1:8554/axis-media/media.amp?camera=2"
    assert auth.resolve_input_uri(f"axis://root:pw@127.0.0.1:{d.port}/1") is None     # no numbered sub stream
    assert cache_file.exists()


# ---------------------------------------------------------------- URL-only vendors
def test_tapo_and_uniview_build_urls_without_a_device(cache_file):
    assert auth.resolve_input_uri("tapo://cam:p%40w@10.0.0.9") == "rtsp://cam:p%40w@10.0.0.9/stream1"
    assert auth.resolve_input_uri("tapo://cam:pw@10.0.0.9:8554/sub") == "rtsp://cam:pw@10.0.0.9:8554/stream2"
    assert auth.resolve_input_uri("tapo://cam:pw@10.0.0.9/2") is None
    assert auth.resolve_input_uri("tapo://cam:pw@10.0.0.9/2/0") is None
    assert auth.resolve_input_uri("unv://admin:pw@10.0.0.9/third") == "rtsp://admin:pw@10.0.0.9/media/video3"
    assert auth.resolve_input_uri("unv://admin:pw@10.0.0.9/3/1") == "rtsp://admin:pw@10.0.0.9/unicast/c3/s1/live"
    assert not cache_file.exists()                                  # nothing was asked of a device


def test_bad_shortcut_uri_is_explained(caplog):
    with caplog.at_level(logging.ERROR):
        assert auth.resolve_input_uri("reolink://admin:pw@10.0.0.9/x/y") is None
    assert "@host[:port][/CHANNEL][/STREAM]" in caplog.text and "0|main, 1|sub or 2|third" in caplog.text


# ---------------------------------------------------------------- ONVIF
def onvif(stream_uri, profiles=('main&amp;1', 'sub'), xaddr="http://192.0.2.7/onvif/Media"):
    seen = SimpleNamespace(stream_uri_bodies=[], security=[])
    def answer(h, body):
        m = re.search(r"<Nonce>(.*?)</Nonce>.*?<Created[^>]*>(.*?)</Created>", body, re.S)
        pw = re.search(r"<Password[^>]*>(.*?)</Password>", body)
        if m and pw:
            seen.security.append((m.group(1), m.group(2), pw.group(1)))
        action = h.headers.get("SOAPAction", "")
        if "GetCapabilities" in action:
            text = (f'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tt="x"><s:Body>'
                    f'<tds:GetCapabilitiesResponse xmlns:tds="y"><tds:Capabilities><tt:Media><tt:XAddr>{xaddr}'
                    f'</tt:XAddr></tt:Media></tds:Capabilities></tds:GetCapabilitiesResponse></s:Body></s:Envelope>')
        elif "GetProfiles" in action:
            text = '<s:Envelope><s:Body><trt:GetProfilesResponse>' + "".join(
                f'<trt:Profiles token="{t}" fixed="true"><tt:Name>p</tt:Name></trt:Profiles>' for t in profiles) + \
                '</trt:GetProfilesResponse></s:Body></s:Envelope>'
        elif "GetStreamUri" in action:
            seen.stream_uri_bodies.append(body)
            text = (f'<s:Envelope><s:Body><trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>{stream_uri}</tt:Uri>'
                    f'</trt:MediaUri></trt:GetStreamUriResponse></s:Body></s:Envelope>')
        else:
            return 404, {}, ""
        return 200, {"Content-Type": "application/soap+xml"}, text
    return answer, seen


def test_onvif_uri_unescaped_and_pointed_at_the_camera(device, caplog):
    answer, seen = onvif("rtsp://192.0.2.7:554/cam/realmonitor?channel=1&amp;subtype=1")
    d = device(answer)
    caplog.set_level(5)                                             # TRACE: SOAP requests are logged
    url = auth.resolve_input_uri(f"onvif://admin:Adm%40in@127.0.0.1:{d.port}/1")
    assert url == "rtsp://admin:Adm%40in@127.0.0.1/cam/realmonitor?channel=1&subtype=1"
    assert "<ProfileToken>sub</ProfileToken>" in seen.stream_uri_bodies[0]
    # the media service was asked on the camera's address, not on the advertised 192.0.2.7
    assert all(r.headers["Host"].startswith("127.0.0.1") for r in d.requests) and len(d.requests) == 3
    # WS-Security used the decoded password ...
    nonce, created, digest = seen.security[0]
    assert base64.b64encode(hashlib.sha1(base64.b64decode(nonce) + created.encode() + b"Adm@in").digest()).decode() == digest
    # ... and none of nonce / created / digest reached the log
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "GetStreamUri REQUEST" in text and '"***"' in text
    for n, c, dg in seen.security:
        assert n not in text and c not in text and dg not in text


def test_onvif_profile_token_is_escaped_back(device):
    answer, seen = onvif("rtsp://127.0.0.1/main")
    d = device(answer)
    assert auth.resolve_input_uri(f"onvif://admin:pw@127.0.0.1:{d.port}/0") == "rtsp://admin:pw@127.0.0.1/main"
    assert "<ProfileToken>main&amp;1</ProfileToken>" in seen.stream_uri_bodies[0]


@pytest.mark.parametrize("hostile", [
    "rtsp://127.0.0.1:554@198.51.100.9:80/x",
    "rtsp://someone:else@127.0.0.1/x",
    "http://127.0.0.1/snapshot.jpg",
])
def test_onvif_hostile_uri_gets_no_password(device, hostile):
    answer, _ = onvif(hostile)
    d = device(answer)
    assert auth.resolve_input_uri(f"onvif://admin:pw@127.0.0.1:{d.port}") is None


def test_poisoned_cache_entry_is_ignored(device, cache_file):
    answer, _ = onvif("rtsp://127.0.0.1/good")
    d = device(answer)
    uri = f"onvif://admin:pw@127.0.0.1:{d.port}"
    cache_file.write_text(json.dumps({auth._cache_key(uri): {
        "url": "rtsp://admin:***@127.0.0.1:554@198.51.100.9:80/x", "timestamp": "2099-01-01T00:00:00"}}))
    cache_file.write_text(cache_file.read_text().replace("2099-01-01T00:00:00", time.strftime("%Y-%m-%dT%H:%M:%S")))
    assert auth.resolve_input_uri(uri) == "rtsp://admin:pw@127.0.0.1/good"


def test_onvif_port_scan_is_parallel_and_stoppable(monkeypatch):
    # every port refused at once: the scan ends quickly instead of 25 x 1.5 s
    t0 = time.monotonic()
    assert auth.resolve_input_uri("onvif://admin:pw@127.0.0.1") is None
    # Windows retries a refused connect for ~2 s, and the SOAP fallbacks after the scan hit that too
    assert time.monotonic() - t0 < (20 if os.name == "nt" else 3)
    # a DNS lookup that hangs: stop() is honoured within a fraction of a second
    monkeypatch.setattr(auth.socket, "getaddrinfo", lambda *a, **k: time.sleep(5))
    stop_at = time.monotonic() + 0.3
    t0 = time.monotonic()
    assert auth.resolve_input_uri("onvif://admin:pw@cam.example", should_stop=lambda: time.monotonic() > stop_at) is None
    assert time.monotonic() - t0 < 1.0


def test_quiet_failures_log_at_debug(caplog):
    caplog.set_level(logging.DEBUG)
    auth.resolve_input_uri("onvif://admin:pw@127.0.0.1", quiet=True)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


# ---------------------------------------------------------------- XMeye (python-dvr stubbed)
def fake_dvrip(monkeypatch, behaviour, channels=None):
    class DVRIPCam:
        def __init__(self, ip, user, password, port):
            self.args = (ip, user, password, port)
        def login(self):
            if behaviour == "down":
                raise OSError("timed out")
            return True
        def get_system_info(self):
            if channels is None:                      # a device without SystemInfo: python-dvr returns the error dict
                return {"Name": "SystemInfo", "Ret": 102, "SessionID": "0x1"}
            return {"VideoInChannel": 0, "DigChannel": channels, "HardWare": "NBD8008R-U"}
        def get_info(self, name):
            self.asked = name
            if behaviour == "old":                   # firmware without NetWork.RTSP: python-dvr returns the error dict
                return {"Name": name, "Ret": 102, "SessionID": "0x1"}
            return {"Client": {"Port": 554}, "IsClient": False, "IsServer": behaviour != "off",
                    "Server": {"Port": 554 if behaviour == "off" else behaviour}} if name == "NetWork.RTSP" else None
        def close(self): pass
    monkeypatch.setitem(sys.modules, "dvrip", SimpleNamespace(DVRIPCam=DVRIPCam))


def test_xmeye_transient_failure_is_not_cached(monkeypatch, cache_file):
    fake_dvrip(monkeypatch, "down")
    url = auth.resolve_input_uri("xmeye://admin:p&w@127.0.0.1")
    assert url == "rtsp://admin:p%26w@127.0.0.1/user=admin&password=p%26w&channel=1&stream=0.sdp?real_stream"
    assert not cache_file.exists()


def test_xmeye_device_port_and_channel(monkeypatch, cache_file):
    fake_dvrip(monkeypatch, 8554)
    assert auth.resolve_input_uri("xmeye://admin:pw@127.0.0.1/1") == \
        "rtsp://admin:pw@127.0.0.1:8554/user=admin&password=pw&channel=1&stream=1.sdp?real_stream"
    assert cache_file.exists() and "pw" not in json.loads(cache_file.read_text()).popitem()[1]["url"].split("@")[0][7:]
    assert auth.resolve_input_uri("xmeye://admin:pw@127.0.0.1/5/0") == \
        "rtsp://admin:pw@127.0.0.1:8554/user=admin&password=pw&channel=5&stream=0.sdp?real_stream"
    assert auth.resolve_input_uri("xmeye://admin:pw@127.0.0.1/2") is None               # no third stream


def test_xmeye_hostile_port(monkeypatch, cache_file):
    fake_dvrip(monkeypatch, "554@198.51.100.9:80")
    assert auth.resolve_input_uri("xmeye://admin:pw@127.0.0.1").startswith("rtsp://admin:pw@127.0.0.1/user=")
    assert not cache_file.exists()


def test_xmeye_recorder_channel_count(monkeypatch, cache_file, caplog):
    fake_dvrip(monkeypatch, 554, channels=16)
    assert auth.resolve_input_uri("xmeye://admin:pw@127.0.0.1/16/1").endswith("channel=16&stream=1.sdp?real_stream")
    with caplog.at_level(logging.ERROR):
        assert auth.resolve_input_uri("xmeye://admin:pw@127.0.0.1/17/0") is None
    assert "has 16 channel(s); channel 17 does not exist" in caplog.text


def test_xmeye_old_firmware_and_rtsp_off(monkeypatch, cache_file, caplog):
    fake_dvrip(monkeypatch, "old")
    assert auth.resolve_input_uri("xmeye://admin:pw@127.0.0.1").startswith("rtsp://admin:pw@127.0.0.1/user=")
    fake_dvrip(monkeypatch, "off")
    with caplog.at_level(logging.WARNING):
        assert auth.resolve_input_uri("xmeye://admin:pw@127.0.0.1", use_cache=False) is not None
    assert "RTSP server is switched off" in caplog.text
