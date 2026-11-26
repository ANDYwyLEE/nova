"""nova find: the search, the question to a camera, and what is written to cameras.ini."""
import hashlib
import ipaddress
import os
import re
import socket
import threading
import time

import pytest

import camfind

SDP = ("v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=cam\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H265/90000\r\n"
       "m=audio 0 RTP/AVP 8\r\na=control:track2\r\n")


class Camera:
    """An RTSP server as a camera's: OPTIONS, and DESCRIBE of its paths for its user.  `refused` counts
    the logins it turned down, which is what a real one locks an account for."""

    def __init__(self, paths=("/av_stream/main",), user="admin", pwd="secret", scheme="Digest", server="Fake/1.0 (Build 7)",
                 any_path=False, in_path=False, algorithm=None):
        self.paths, self.user, self.pwd, self.scheme, self.server = paths, user, pwd, scheme, server
        self.any_path, self.in_path, self.algorithm = any_path, in_path, algorithm
        self.refused, self.asked = 0, []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.sock.close()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        with conn:
            conn.settimeout(5)
            data = b""
            while True:
                while b"\r\n\r\n" not in data:
                    try:
                        chunk = conn.recv(4096)
                    except OSError:
                        return
                    if not chunk:
                        return
                    data += chunk
                head, _, data = data.partition(b"\r\n\r\n")
                lines = head.decode().split("\r\n")
                method, url, _ = lines[0].split(" ", 2)
                headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
                status, extra, body = self._answer(method, url, headers)
                reply = [f"RTSP/1.0 {status} X", f"CSeq: {headers.get('cseq', '0')}"] + extra
                if body:
                    reply += ["Content-Type: application/sdp", f"Content-Length: {len(body)}"]
                conn.sendall(("\r\n".join(reply) + "\r\n\r\n" + body).encode())

    def _answer(self, method, url, headers):
        if method == "OPTIONS":
            return 200, ["Public: OPTIONS, DESCRIBE, SETUP, PLAY"] + ([f"Server: {self.server}"] if self.server else []), ""
        path = "/" + url.split("://", 1)[1].split("/", 1)[1] if "/" in url.split("://", 1)[1] else "/"
        self.asked.append(path)
        if self.in_path:                                         # XMeye: the login is part of the path
            good = f"user={self.user}&password={self.pwd.replace('&', '%26')}&" in path
            return (200, [], SDP) if good else (404, [], "")
        given = headers.get("authorization")
        if self.user and not given:
            realm = 'realm="cam", nonce="n0nce"' + (f", algorithm={self.algorithm}" if self.algorithm else "")
            return 401, [f"WWW-Authenticate: {self.scheme} {realm}" if self.scheme == "Digest" else 'WWW-Authenticate: Basic realm="cam"'], ""
        if self.user and not self._right(method, url, given):
            self.refused += 1
            return 401, ['WWW-Authenticate: Digest realm="cam", nonce="n0nce"'], ""
        if self.any_path or path in self.paths:
            return 200, [], SDP
        return 404, [], ""

    def _right(self, method, url, given):
        if given.lower().startswith("basic"):
            import base64
            return base64.b64decode(given.split()[1]).decode() == f"{self.user}:{self.pwd}"
        h = hashlib.sha256 if self.algorithm == "SHA-256" else hashlib.md5
        hx = lambda t: h(t.encode()).hexdigest()                 # noqa: E731
        want = hx(f"{hx(f'{self.user}:cam:{self.pwd}')}:n0nce:{hx(f'{method}:{url}')}")
        return f'response="{want}"' in given and f'username="{self.user}"' in given


@pytest.fixture
def camera():
    made = []

    def make(**kw):
        made.append(Camera(**kw))
        return made[-1]
    yield make
    for c in made:
        c.close()


def _device(cam, onvif=None, name=""):
    d = camfind.Device("127.0.0.1")
    d.rtsp_port, d.server, d.onvif, d.name = cam.port, "Fake/1.0", onvif, name
    return d


def _closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


MATCH = """<?xml version="1.0" encoding="UTF-8"?><SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
 xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><SOAP-ENV:Body><d:ProbeMatches><d:ProbeMatch>
 <wsa:EndpointReference><wsa:Address>urn:uuid:5f5a69c2</wsa:Address></wsa:EndpointReference>
 <d:Types>dn:NetworkVideoTransmitter</d:Types>
 <d:Scopes>onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/hardware/IPC-B2 onvif://www.onvif.org/name/%s
 onvif://www.onvif.org/location/country/x</d:Scopes>
 <d:XAddrs>%s</d:XAddrs><d:MetadataVersion>1</d:MetadataVersion></d:ProbeMatch></d:ProbeMatches></SOAP-ENV:Body></SOAP-ENV:Envelope>"""


# ---------------------------------------------------------------- the search
def test_what_a_device_says_to_the_discovery_probe():
    """Five cameras of the LAN this was written on answered the probe, each with a port of its own for
    the ONVIF service (80, 1000, 8080), one with `name/Unknown`."""
    parse = camfind.parse_probe_match
    assert parse(MATCH % ("Front%20door", "http://192.0.2.7:8080/onvif/device_service"), "192.0.2.7") == (8080, "Front door")
    assert parse(MATCH % ("Unknown", "http://192.0.2.7/onvif/device_service"), "192.0.2.7") == (80, "IPC-B2")
    # two addresses: the one the answer came from counts; a name of the device's own network still gives the port
    two = "http://10.0.0.9:8000/onvif/device_service http://192.0.2.7:1000/onvif/device_service"
    assert parse(MATCH % ("cam", two), "192.0.2.7") == (1000, "cam")
    assert parse(MATCH % ("cam", "http://10.0.0.9:8000/onvif/device_service"), "192.0.2.7") == (8000, "cam")
    assert parse(MATCH % ("cam", "https://192.0.2.7/onvif/device_service"), "192.0.2.7") == (443, "cam")
    # other prefixes, and no prefix at all
    plain = (MATCH % ("cam", "http://192.0.2.7:8899/onvif/device_service")).replace("d:", "").replace("dn:", "tds:")
    assert parse(plain, "192.0.2.7") == (8899, "cam")
    # a printer answers probes too; a hello is no match; an answer without an address is none
    printer = (MATCH % ("x", "http://192.0.2.7/ws")).replace("dn:NetworkVideoTransmitter", "wprt:PrintDeviceType").replace("onvif", "example")
    assert parse(printer, "192.0.2.7") is None
    assert parse("<Envelope><Hello/></Envelope>", "192.0.2.7") is None
    assert parse(MATCH % ("cam", ""), "192.0.2.7") is None and parse(MATCH % ("cam", "ftp://192.0.2.7/x"), "192.0.2.7") is None
    # a device's words reach the terminal and cameras.ini: one line, no escape sequences
    port, name = parse(MATCH % ("a%1b[31mred%0aopts%20=%20-t%20x", "http://192.0.2.7/onvif"), "192.0.2.7")
    assert name == "ared opts = -t x" and "\x1b" not in name and "\n" not in name


def test_connections_tell_who_is_there(camera, monkeypatch):
    cam, closed = camera(), _closed_port()
    # Windows says "refused" only after trying again by itself: the second try's timeout is made for that
    t0 = time.monotonic()
    with socket.socket() as probe:
        probe.settimeout(10)
        probe.connect_ex(("127.0.0.1", closed))
    took = time.monotonic() - t0
    answers = camfind.connects([("127.0.0.1", cam.port), ("127.0.0.1", closed)], timeout=camfind.RETRY_S, window=1)   # one at a time
    assert answers[("127.0.0.1", cam.port)] is True
    assert answers.get(("127.0.0.1", closed)) is False, f"a refusal takes {took:.2f} s here"      # nobody listens, the host is there
    # the question goes on the connection that was made: one connection for a camera that serves one at a time
    said = camfind.connects([("127.0.0.1", cam.port), ("127.0.0.1", closed)], timeout=camfind.RETRY_S, hello=camfind._options)
    assert said[("127.0.0.1", cam.port)].startswith(b"RTSP/1.0 200") and said[("127.0.0.1", closed)] is False
    # more targets than one selector takes on Windows: lots side by side, the same answers
    monkeypatch.setattr(camfind, "SIDE_BY_SIDE", True)
    assert camfind.connects([("127.0.0.1", cam.port), ("127.0.0.1", closed)], timeout=camfind.RETRY_S, window=1) == answers
    monkeypatch.setattr(camfind, "SIDE_BY_SIDE", os.name == "nt")
    assert camfind.server_line(said[("127.0.0.1", cam.port)]) == "Fake/1.0"         # cut before "(Build 7)"
    assert camfind.server_line(b"HTTP/1.1 400 Bad Request\r\n\r\n") is None and camfind.server_line(True) is None
    assert camfind.server_line(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\n") == "" and camfind.server_line(b"") is None
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(1)
    try:
        port = silent.getsockname()[1]
        assert camfind.connects([("127.0.0.1", port)], hello=camfind._options, answer_s=0.2) == {("127.0.0.1", port): b""}
    finally:
        silent.close()
    assert camfind.connects([]) == {}
    assert camfind._window() >= 32


def test_the_search_finds_what_answers_rtsp_and_the_probe(camera):
    cam = camera(server="H264DVR 1.0")
    responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    responder.bind(("127.0.0.1", 0))
    wsd = responder.getsockname()[1]

    def answer():
        try:
            while True:
                data, who = responder.recvfrom(65535)
                assert b"NetworkVideoTransmitter" in data and b"Probe" in data
                responder.sendto((MATCH % ("Yard", "http://127.0.0.1:8899/onvif/device_service")).encode(), who)
        except OSError:
            pass
    threading.Thread(target=answer, daemon=True).start()
    try:
        found = camfind.search(["127.0.0.1"], wait=1.0, ports=(cam.port,), wsd_port=wsd, dvrip_port=_closed_port())
    finally:
        responder.close()
    (d,) = found
    assert (d.ip, d.rtsp_port, d.server, d.onvif, d.name, d.dvrip) == ("127.0.0.1", cam.port, "H264DVR 1.0", 8899, "Yard", False)
    assert d.is_xmeye() and d.what() == f'ONVIF "Yard", RTSP on {cam.port} "H264DVR 1.0", XMeye'
    # a port that is open and does not speak RTSP is no camera; neither is an address where nothing answers
    web = socket.socket()
    web.bind(("127.0.0.1", 0))
    web.listen(4)

    def http():
        try:
            while True:
                conn, _ = web.accept()
                conn.recv(1024)
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                conn.close()
        except OSError:
            pass
    threading.Thread(target=http, daemon=True).start()
    try:
        assert camfind.search(["127.0.0.1"], wait=0.3, ports=(web.getsockname()[1],), wsd_port=_closed_port(),
                              dvrip_port=_closed_port()) == []
    finally:
        web.close()
    # RTSP's port open and silent is a camera whose server is busy: listed as that, and asked when it is picked
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(4)
    try:
        port = busy.getsockname()[1]
        (d,) = camfind.search(["127.0.0.1"], wait=0.3, ports=(port, cam.port), wsd_port=_closed_port(),
                              dvrip_port=_closed_port(), answer_s=0.3)
        assert (d.rtsp_port, d.server) == (cam.port, "H264DVR 1.0") and not d.silent     # another port of it answers
        (d,) = camfind.search(["127.0.0.1"], wait=0.3, ports=(port,), wsd_port=_closed_port(),
                              dvrip_port=_closed_port(), answer_s=0.3)
        assert d.silent and d.what() == f"port {port} open, no answer to RTSP yet"
    finally:
        busy.close()
    assert camfind.search(["127.0.0.1"], wait=0.3, ports=(_closed_port(),), wsd_port=_closed_port(), dvrip_port=_closed_port()) == []
    assert camfind.rtsp_server("127.0.0.1", cam.port) == "H264DVR 1.0" and camfind.rtsp_server("127.0.0.1", _closed_port()) is None


def test_a_large_network_is_asked_through_several_sockets(monkeypatch):
    """A /22: 1021 probes through one socket were 167 sent and 854 refused with EAGAIN (a packet to an
    address nobody has waits for ARP in the socket's send buffer), and no camera's answer came."""
    sent = []

    class Udp:
        def __init__(self, *a):
            self.n = 0
            sent.append(self)

        def bind(self, addr): pass
        def setblocking(self, flag): pass
        def setsockopt(self, *a): pass
        def close(self): pass

        def sendto(self, data, addr):
            self.n += 1

        def recvfrom(self, n):
            raise BlockingIOError

    monkeypatch.setattr(camfind.socket, "socket", Udp)
    monkeypatch.setattr(camfind, "connects", lambda targets, **kw: {})
    hosts = [str(h) for h in ipaddress.ip_network("192.0.2.0/22", strict=False).hosts()][:1021]
    assert camfind.search(hosts, own=["192.0.2.200"], wait=0) == []
    assert len(sent) == 11 and max(s.n for s in sent) <= 101 and sum(s.n for s in sent) == 1022     # 1021 + the group


def test_an_address_that_said_nothing_is_asked_once_more(monkeypatch):
    """A Mac that had not talked to the cameras before: one of them, on Wi-Fi, was not there within the
    0.7 s of the first connection in five searches of five, and answered a second later."""
    calls = []

    def connects(targets, timeout=camfind.CONNECT_S, hello=None, answer_s=None):
        calls.append((sorted(targets), timeout))
        if len(calls) == 1:
            return {("192.0.2.7", 554): b"RTSP/1.0 200 OK\r\nServer: A\r\n\r\n"}
        if len(calls) == 2:
            return {("192.0.2.8", 554): b"RTSP/1.0 200 OK\r\nServer: B\r\n\r\n"}
        return {}
    monkeypatch.setattr(camfind, "connects", connects)
    found = camfind.search(["192.0.2.7", "192.0.2.8", "192.0.2.9"], wait=0, wsd_port=_closed_port())
    assert [(d.ip, d.server) for d in found] == [("192.0.2.7", "A"), ("192.0.2.8", "B")]
    assert calls[1] == ([("192.0.2.8", 554), ("192.0.2.9", 554)], camfind.RETRY_S)        # only those that said nothing
    assert {h for h, _ in calls[2][0]} == {"192.0.2.7", "192.0.2.8"}                     # the other ports: of those that are there


def test_which_addresses_a_word_names():
    assert camfind.hosts_of("192.168.7.9") == ["192.168.7.9"] and camfind.hosts_of("8.8.8.8") == ["8.8.8.8"]
    assert camfind.hosts_of("192.168.7.9/30") == ["192.168.7.9", "192.168.7.10"]        # the network it is in
    assert len(camfind.hosts_of("10.1.2.0/24")) == 254
    assert len(camfind.hosts_of("100.64.3.0/28")) == 14                                # carrier-grade NAT, a VPN's range
    for word, why in (("8.8.8.0/24", "private"), ("fd00::/64", "IPv4"), ("10.0.0.0/8", "too many"),
                      ("no such host.invalid", "neither")):
        with pytest.raises(ValueError, match=why):
            camfind.hosts_of(word)
    nets = camfind.local_networks()
    assert all(not n.ip.is_loopback and n.version == 4 for n in nets)


# ---------------------------------------------------------------- a camera's stream
def test_the_stream_is_found_among_the_usual_paths(camera, monkeypatch):
    cam = camera(paths=("/av_stream/main",), pwd="p@ss w/ord:1")
    monkeypatch.setattr(camfind, "_resolver", lambda *a: None)
    said = []
    url, what = camfind.stream_url(_device(cam), "admin", "p@ss w/ord:1", say=said.append)
    assert url == f"rtsp://admin:p%40ss%20w%2Ford%3A1@127.0.0.1:{cam.port}/av_stream/main"
    assert what == "H.265, sound G.711 A-law" and said == ["asking for the usual paths"]
    assert cam.refused == 0 and cam.asked[0] == "/" and cam.asked[-1] == "/av_stream/main"
    # Basic, and SHA-256 as newer cameras ask
    for kw in ({"scheme": "Basic"}, {"algorithm": "SHA-256"}, {"algorithm": "MD5"}):
        other = camera(paths=("/stream1",), **kw)
        assert camfind.stream_url(_device(other), "admin", "secret")[0] == f"rtsp://admin:secret@127.0.0.1:{other.port}/stream1"
        assert other.refused == 0
    # a camera without a password
    free = camera(paths=("/live",), user="")
    assert camfind.stream_url(_device(free), "", "")[0] == f"rtsp://127.0.0.1:{free.port}/live"


def test_a_refused_password_is_one_failed_login_and_no_more(camera, monkeypatch):
    """Cameras count failed logins and lock the account (five of them, half an hour): the ONVIF service
    and seventeen paths asked with a wrong password would be that."""
    cam = camera()
    monkeypatch.setattr(camfind, "_resolver", lambda *a: pytest.fail("asked after a refusal"))
    assert camfind.stream_url(_device(cam, onvif=8899), "admin", "wrong") == (None, camfind.REFUSED)
    assert cam.refused == 1 and cam.asked == ["/", "/"]            # asked, and asked again with the login
    # a server that asks for no password at "/": the first path that does is where it ends
    assert camfind._first_path(f"rtsp://127.0.0.1:{cam.port}", ("/a", "/b", "/c"), "admin", "wrong") == (camfind.REFUSED, "")
    assert cam.refused == 2 and cam.asked[2:] == ["/a", "/a"]
    assert camfind._first_path(f"rtsp://127.0.0.1:{cam.port}", ("/a", "/b"), "admin", "secret") == (None, "")


def test_the_devices_own_answer_comes_first(camera, monkeypatch):
    cam = camera(paths=("/onvif/profile1", "/stream1"))
    asked = []

    def onvif(scheme, user, pwd, host, port):
        asked.append((scheme, host, port))
        return f"rtsp://admin:secret@127.0.0.1:{cam.port}/onvif/profile1"
    monkeypatch.setattr(camfind, "_resolver", onvif)
    assert camfind.stream_url(_device(cam, onvif=8899), "admin", "secret")[0].endswith("/onvif/profile1")
    assert asked == [("onvif", "127.0.0.1", 8899)]
    # an ONVIF service that gives nothing (its own users, as on a Hikvision): the usual paths
    monkeypatch.setattr(camfind, "_resolver", lambda *a: None)
    assert camfind.stream_url(_device(cam, onvif=8899), "admin", "secret")[0].endswith("/stream1")
    # none answered the probe: its ONVIF service is looked for last, on the usual ports
    cam2 = camera(paths=("/odd/path",))
    monkeypatch.setattr(camfind, "_resolver", lambda scheme, user, pwd, host, port: asked.append(port) or
                        f"rtsp://admin:secret@127.0.0.1:{cam2.port}/odd/path")
    said = []
    assert camfind.stream_url(_device(cam2), "admin", "secret", say=said.append)[0].endswith("/odd/path")
    assert asked[-1] is None and said == ["asking for the usual paths", "looking for an ONVIF service"]
    # nothing found: the reason says what was tried
    monkeypatch.setattr(camfind, "_resolver", lambda *a: None)
    url, why = camfind.stream_url(_device(cam2), "admin", "secret", paths=("/a", "/b"))
    assert url is None and "2 usual paths, ONVIF on the usual ports" in why and "by hand" in why
    dead = camfind.Device("127.0.0.1")
    dead.rtsp_port = _closed_port()
    assert camfind.stream_url(dead, "admin", "secret")[1].startswith("RTSP does not answer")


def test_a_device_cannot_write_lines_of_its_own(monkeypatch):
    """The stream URI is the device's answer.  One with a line end in it would be a second line of the
    camera's section: `opts = -t rtmp://…` is where that camera's picture is sent."""
    import auth
    monkeypatch.setattr(auth, "find_stream", lambda *a: "rtsp://admin:secret@192.0.2.7/x\nopts = -t rtmp://192.0.2.99/live")
    assert camfind._resolver("onvif", "admin", "secret", "192.0.2.7", 80) is None
    monkeypatch.setattr(auth, "find_stream", lambda *a: "rtsp://admin:secret@192.0.2.7/x y")
    assert camfind._resolver("onvif", "admin", "secret", "192.0.2.7", 80) is None
    monkeypatch.setattr(auth, "find_stream", lambda *a: "rtsp://admin:secret@192.0.2.7/x?y=1")
    assert camfind._resolver("onvif", "admin", "secret", "192.0.2.7", 80) == "rtsp://admin:secret@192.0.2.7/x?y=1"


def test_xmeye_and_a_server_that_plays_any_path(camera, monkeypatch):
    monkeypatch.setattr(camfind, "_resolver", lambda *a: None)
    dvr = camera(in_path=True, pwd="a&b", server="H264DVR 1.0")
    d = _device(dvr)
    d.server = "H264DVR 1.0"
    url, what = camfind.stream_url(d, "admin", "a&b")
    assert url == f"rtsp://admin:a%26b@127.0.0.1:{dvr.port}/user=admin&password=a%26b&channel=1&stream=0.sdp?real_stream"
    assert what.startswith("H.265")
    # whatever is asked plays: no path says more than "/"
    loose = camera(any_path=True)
    assert camfind.stream_url(_device(loose), "admin", "secret")[0] == f"rtsp://admin:secret@127.0.0.1:{loose.port}/"
    assert sorted(set(loose.asked))[0] == "/" and len(set(loose.asked)) == 2 and loose.asked[-1].startswith("/nova-find-")


def test_what_a_stream_sends():
    assert camfind.media(SDP) == "H.265, sound G.711 A-law"
    assert camfind.media("m=video 0 RTP/AVP 96\na=rtpmap:96 H264/90000\nm=audio 0 RTP/AVP 97\na=rtpmap:97 MPEG4-GENERIC/16000") == "H.264, sound AAC"
    assert camfind.media("m=video 0 RTP/AVP 26\n") == "MJPEG" and camfind.media("m=audio 0 RTP/AVP 0\n") == ""
    assert camfind.media("m=video 0 RTP/AVP 96\na=rtpmap:96 \x1b[31mX/90000") == "X"          # a device's words: no escape sequence


def test_the_resolver_for_find_is_quiet_and_keeps_to_the_camera(monkeypatch):
    import auth
    seen = {}

    def onvif(info, should_stop):
        seen.update(info, quiet=auth._tls.quiet)
        return f"rtsp://u:p@{info['ip']}/s", True
    monkeypatch.setitem(auth._RESOLVERS, "onvif", onvif)
    assert auth.find_stream("onvif", "u", "p", "192.0.2.7", 8080) == "rtsp://u:p@192.0.2.7/s"
    assert seen["quiet"] is True and seen["port"] == 8080 and seen["stream_idx"] == 0 and not seen["channel_given"]
    assert auth._tls.quiet is False
    monkeypatch.setitem(auth._RESOLVERS, "onvif", lambda info, stop: ("rtsp://u:p@192.0.2.99/s", True))     # another host
    assert auth.find_stream("onvif", "u", "p", "192.0.2.7") is None
    monkeypatch.setitem(auth._RESOLVERS, "onvif", lambda info, stop: (None, False))
    assert auth.find_stream("onvif", "u", "p", "192.0.2.7") is None


# ---------------------------------------------------------------- cameras.ini
def test_a_camera_is_appended_and_the_file_stays_as_it_was(tmp_path):
    ini = tmp_path / "cameras.ini"
    before = "; my cameras\n[defaults]\nopts = -f 30   ; kept\n\n[yard]\nurl = rtsp://u:p@192.0.2.1/x"
    ini.write_text(before)
    assert camfind.add_camera(str(ini), "cam7", "Front door", "rtsp://admin:p%40ss@192.0.2.7/stream1") is False
    assert ini.read_text() == before + "\n\n[cam7]\nname = Front door\nurl = rtsp://admin:p%40ss@192.0.2.7/stream1\n"
    camfind.add_camera(str(ini), "cam8", "cam8", "rtsp://192.0.2.8/live")              # a name that is the id is left out
    assert ini.read_text().endswith("stream1\n\n[cam8]\nurl = rtsp://192.0.2.8/live\n")
    assert camfind.sections(str(ini)) == ["defaults", "yard", "cam7", "cam8"]
    import cams
    _, _, listed = cams.load(str(ini), quiet=True)
    assert [(c.id, c.name, c.url) for c in listed][1:] == [("cam7", "Front door", "rtsp://admin:p%40ss@192.0.2.7/stream1"),
                                                           ("cam8", "cam8", "rtsp://192.0.2.8/live")]
    for bad in ("rtsp://192.0.2.7/x\nopts = -t rtmp://192.0.2.99/x", "rtsp://192.0.2.7/a b", "http://192.0.2.7/x", "rtsp://192.0.2.7/\x1b[2J"):
        with pytest.raises(ValueError):
            camfind.add_camera(str(ini), "bad", "", bad)
    camfind.add_camera(str(ini), "cam9", "a\nopts = -r\x1b[31m", "rtsp://192.0.2.9/live")
    assert ini.read_text().endswith("[cam9]\nname = a opts = -r\nurl = rtsp://192.0.2.9/live\n")


def test_the_first_cameras_ini_is_made_from_the_template(tmp_path):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ini = tmp_path / "cameras.ini"
    assert camfind.add_camera(str(ini), "cam7", "Front door", "rtsp://admin:pw@192.0.2.7/stream1",
                              os.path.join(root, "cameras.example.ini")) is True
    text = ini.read_text()
    assert camfind.sections(str(ini)) == ["defaults", "env", "cam7"]            # the template's cameras are not ours
    assert "NOVA_TIMEOUT = 10000000" in text and "\nopts = " in text and "frontdoor" not in text.split("[env]")[1]
    if os.name != "nt":
        assert os.stat(ini).st_mode & 0o777 == 0o600                             # it holds passwords
    import cams
    defaults, env, listed = cams.load(str(ini), quiet=True)
    assert [c.id for c in listed] == ["cam7"] and "opts" in defaults and env["NOVA_TIMEOUT"] == "10000000"
    other = tmp_path / "other.ini"
    camfind.add_camera(str(other), "a", "", "rtsp://192.0.2.7/x", str(tmp_path / "no-template.ini"))
    assert other.read_text() == "[defaults]\nopts =\n\n[a]\nurl = rtsp://192.0.2.7/x\n"


def test_ids_and_the_cameras_already_listed():
    taken = ["defaults", "env", "Yard", "cam7"]
    assert camfind.id_refusal("porch", taken) is None and camfind.id_refusal("포치", taken) is None
    for bad in ("", "yard", "CAM7", "env", "a b", "a/b", "a\\b", ".hid", "-r", "[x]", "x" * 65):
        assert camfind.id_refusal(bad, taken), bad
    assert camfind.free_id("cam7", taken) == "cam7_2" and camfind.free_id("cam8", taken) == "cam8"

    class Cam:
        def __init__(self, cid, url):
            self.id, self.url = cid, url
    have = camfind.listed([Cam("a", "rtsp://u:p@ss@192.0.2.7:554/x"), Cam("b", "rtsp://192.0.2.7/user=u&password=p&channel=2"),
                           Cam("c", "onvif://u:p@192.0.2.8"), Cam("d", "/dev/video0"), Cam("e", "rtsp://u:p@[2001:db8::1]:554/x"),
                           Cam("f", "rtsp://localhost/x"), Cam("g", "rtsp://no-such-host.invalid/x")])
    assert have["192.0.2.7"] == ["a", "b"] and have["192.0.2.8"] == ["c"] and have["127.0.0.1"] == ["f"]
    assert "2001:db8::1" in have and len(have) == 4


# ---------------------------------------------------------------- the command
class Talk:
    """The terminal of `nova find`: what it printed, and the lines typed at its questions."""

    def __init__(self, *typed):
        self.typed, self.out, self.asked = list(typed), [], []

    def say(self, text=""):
        self.out.append(str(text))

    def ask(self, prompt):
        self.asked.append(prompt)
        self.out.append(prompt)
        answer = self.typed.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    @property
    def text(self):
        return "\n".join(self.out)


def _find(tmp_path, talk, devices, monkeypatch, cameras=(), interactive=True, words=()):
    monkeypatch.setattr(camfind, "search", lambda hosts, own=(): devices)
    monkeypatch.setattr(camfind, "local_networks", lambda: [ipaddress.ip_interface("192.0.2.10/24")])
    monkeypatch.setattr(camfind, "_resolver", lambda *a: None)
    return camfind.main(str(tmp_path / "cameras.ini"), list(words), list(cameras), str(tmp_path / "none.ini"),
                        out=talk.say, ask=talk.ask, ask_hidden=talk.ask, interactive=interactive)


def test_find_lists_asks_and_adds(tmp_path, camera, monkeypatch):
    one, two = camera(paths=("/stream1",)), camera(paths=("/live/main",), user="viewer", pwd="other")
    a, b = _device(one, name="Porch"), _device(two)
    talk = Talk("all", "", "secret",                 # which, user name (admin), the password for both
                "", "",                              # the first: id and name as offered
                "viewer", "other", "gate", "Gate 2")  # the second refused them: its own, then id and name
    assert _find(tmp_path, talk, [a, b], monkeypatch) == 0
    text = (tmp_path / "cameras.ini").read_text()
    assert f"[cam1]\nname = Porch\nurl = rtsp://admin:secret@127.0.0.1:{one.port}/stream1\n" in text
    assert text.endswith(f"[gate]\nname = Gate 2\nurl = rtsp://viewer:other@127.0.0.1:{two.port}/live/main\n")
    assert "secret" not in talk.text and "other@" not in talk.text and "rtsp://admin:***@127.0.0.1" in talk.text
    assert "Looking for cameras on 192.0.2.0/24 (253 addresses)" in talk.text and "2 found" in talk.text
    assert "the camera refused this user name and password" in talk.text and two.refused == 1 and one.refused == 0
    assert "2 added. nova start cam1 opens the first" in talk.text
    assert 'Numbers ("all" for the 2 new; Enter: none)' in talk.asked[0] and "these 2 cameras" in talk.asked[1]


def test_find_marks_the_cameras_it_has_and_adds_nothing_unasked(tmp_path, camera, monkeypatch):
    cam = camera()

    class Listed:
        id, url = "yard", "rtsp://u:p@127.0.0.1/x"
    talk = Talk("")
    assert _find(tmp_path, talk, [_device(cam)], monkeypatch, cameras=[Listed]) == 0
    assert re.search(r"1\s+127\.0\.0\.1\s+RTSP on \d+ \"Fake/1\.0\"\s+yard", talk.text) and "none of them is new" in talk.asked[0]
    assert "Nothing added." in talk.text and not (tmp_path / "cameras.ini").exists()
    # not at a terminal: the list, and no question
    talk = Talk()
    assert _find(tmp_path, talk, [_device(cam)], monkeypatch, interactive=False) == 0
    assert "1 of them is not in cameras.ini. Run nova find in a terminal" in talk.text and not talk.asked
    # nothing found; a word that names no network
    talk = Talk()
    assert _find(tmp_path, talk, [], monkeypatch) == 0 and "None found" in talk.text
    talk = Talk()
    assert _find(tmp_path, talk, [], monkeypatch, words=["8.8.8.0/24"]) == 1 and "only private networks" in talk.text


def test_find_gives_up_on_a_camera_before_it_locks_the_account(tmp_path, camera, monkeypatch):
    cam, good = camera(), camera(paths=("/stream1",))
    talk = Talk("1 2", "admin", "wrong", "admin", "wrong2", "admin", "wrong3",      # three refusals: left out
                "admin", "secret", "", "")                                           # the second camera's own
    assert _find(tmp_path, talk, [_device(cam), _device(good)], monkeypatch) == 0
    assert cam.refused == camfind.TRIES == 3 and f"left out after {camfind.TRIES} tries" in talk.text
    assert camfind.sections(str(tmp_path / "cameras.ini")) == ["defaults", "cam1"]      # the one that took it
    assert "1 added. nova start cam1 opens it" in talk.text
    # Enter at the user name leaves a camera out after the first refusal; Ctrl-C keeps what was added
    cam2 = camera()
    talk = Talk("1 2 3", "admin", "secret", "", "one", "", KeyboardInterrupt())
    assert _find(tmp_path, talk, [_device(good), _device(cam2, name="x"), _device(good)], monkeypatch) == 0
    assert "[cam1_2]\nname = one\n" in (tmp_path / "cameras.ini").read_text() and "1 added." in talk.text


def test_find_asks_again_for_an_id_that_cannot_be(tmp_path, camera, monkeypatch):
    cam = camera(paths=("/stream1",))
    (tmp_path / "cameras.ini").write_text("[defaults]\nopts =\n[Yard]\nurl = rtsp://192.0.2.1/x\n")
    talk = Talk("1", "", "secret", "yard", "a b", "porch", "")
    assert _find(tmp_path, talk, [_device(cam)], monkeypatch) == 0
    assert "[yard] is in cameras.ini already" in talk.text and "an id has no space" in talk.text
    assert camfind.sections(str(tmp_path / "cameras.ini")) == ["defaults", "Yard", "porch"]
    assert camfind._numbers("1 3-5, 7", 7) == [1, 3, 4, 5, 7] and camfind._numbers("", 3) == []
    assert camfind._numbers("0", 3) is None and camfind._numbers("4", 3) is None and camfind._numbers("x", 3) is None


def test_the_launcher_knows_find_without_a_cameras_ini(tmp_path, monkeypatch):
    import cams
    seen = {}
    monkeypatch.setattr(camfind, "main", lambda config, words, cameras, example: seen.update(
        config=config, words=words, cameras=[c.id for c in cameras], example=example) or 0)
    ini = tmp_path / "cameras.ini"
    assert cams.main(["-c", str(ini), "find", "192.168.7.0/24"]) == 0
    assert seen == {"config": str(ini), "words": ["192.168.7.0/24"], "cameras": [], "example": cams.EXAMPLE}
    ini.write_text("[yard]\nurl = rtsp://u:p@192.0.2.1/x\n")
    os.chmod(ini, 0o600)
    assert cams.main(["-c", str(ini), "find"]) == 0 and seen["cameras"] == ["yard"] and seen["words"] == []
    assert "nova find" in cams.USAGE
