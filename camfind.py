"""nova find: the cameras on this machine's networks, and a cameras.ini section for the ones chosen.

The search asks nothing that needs a password: an ONVIF discovery probe, a TCP connection to the usual
RTSP ports, RTSP's OPTIONS.  The probe goes to every address by itself as well as to the multicast
group, because a firewall lets in the answer to a packet it saw leave and drops the answers to a
multicast one (firewalld's default zone: five cameras answered on the wire, none reached the socket).
The user name and password are asked for the cameras chosen and go to those addresses only; nothing
is tried that the user did not type, and a refusal ends the questions to that camera, since cameras
count refusals and lock the account."""
import os
import re
import sys
import time
import uuid
import html
import errno
import base64
import socket
import hashlib
import getpass
import ipaddress
import selectors
import threading
from urllib.parse import urlsplit, unquote

RTSP_PORTS = (554, 8554, 10554, 5554, 7447)      # the first is asked of every address, the others of those that answer
DVRIP_PORT = 34567                               # XMeye / XiongMai devices (cameras and recorders) listen here
WSD_GROUP = ("239.255.255.250", 3702)            # WS-Discovery, which ONVIF devices answer
MAX_HOSTS = 1022                                 # a network larger than a /22 is searched only when it is named
CONNECT_S = 0.7                                  # a camera on the LAN answers a connection in milliseconds
# The second try for an address that did not answer the first.  Longer on Windows, which tells of a
# refused connection only after it has tried again by itself: on GitHub's Windows runner a closed port
# of the loopback address was not refused within 0.7 s, where Linux and macOS say so at once.
RETRY_S = 2.5 if os.name == "nt" else 1.0
SIDE_BY_SIDE = os.name == "nt"                   # select() takes 512 sockets there: larger lots in threads
# How many addresses a second get their first packet.  For an address this machine has not talked to,
# the system asks the whole network who has it (ARP, a broadcast), three times, a second apart: a /22
# asked at once is a thousand broadcasts within 7 ms, and again a second and two seconds later, and
# an access point hands only so many on to its Wi-Fi clients.  Measured with two cameras on Wi-Fi among
# seven devices: asked alone they answered in 20 ms; among 200 to 500 questions a second as fast, in
# 13 rounds of 14; among 1000 a second half of the time; in a burst for all 1021 addresses neither
# answered in four rounds of six, the system's two repeats included.  A search from a machine new to
# the network (Linux, Windows) listed five or six of the seven more often than all, and every later
# one seven: by then the machine knew the cameras.  170 addresses a second are, with the system's
# two repeats, 500 broadcasts a second.
PROBE_RATE = 170
ANSWER_S = 4.0                                   # ... and RTSP's OPTIONS as fast, but for one that is busy
ASK_S = 4.0                                      # one RTSP question
TRIES = 3                                        # user name and password are asked this often for one camera

PROBE = ('<?xml version="1.0" encoding="UTF-8"?>'
         '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
         'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
         'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
         'xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><e:Header>'
         '<w:MessageID>uuid:{id}</w:MessageID>'
         '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
         '<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>'
         '</e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>')

# Main-stream paths of the makers Nova has shortcuts for (auth.py), then forms many cameras share.  Asked
# with the user's own name and password, after the camera has accepted them: a wrong path is a 404.
RTSP_PATHS = (
    "/Streaming/Channels/101",                   # Hikvision, Annke, HiLook
    "/cam/realmonitor?channel=1&subtype=0",      # Dahua, Amcrest
    "/h264Preview_01_main",                      # Reolink
    "/h265Preview_01_main",
    "/stream1",                                  # TP-Link Tapo
    "/axis-media/media.amp",                     # Axis
    "/media/video1",                             # Uniview
    "/av_stream/ch0", "/av_stream/main", "/stream_0", "/live/ch00_0", "/live/main", "/ch0_0.h264",
    "/11", "/onvif1", "/live", "/1",
)
REFUSED = "refused"                              # stream_url's reason for a user name or password the camera does not take


class Device:
    """What the search knows of one address."""

    def __init__(self, ip):
        self.ip = ip
        self.rtsp_port = None        # the port RTSP answers on
        self.server = ""             # its Server line ('H264DVR 1.0')
        self.onvif = None            # the port of the ONVIF service it announced
        self.name = ""               # the name, else the hardware, of its ONVIF scopes
        self.dvrip = False           # XMeye's port is open
        self.silent = False          # the RTSP port took the connection and did not answer in time

    def what(self):
        parts = []
        if self.onvif:
            parts.append(f'ONVIF "{self.name}"' if self.name else "ONVIF")
        if self.silent:
            parts.append(f"port {self.rtsp_port} open, no answer to RTSP yet")
        elif self.rtsp_port:
            rtsp = "RTSP" if self.rtsp_port == 554 else f"RTSP on {self.rtsp_port}"
            parts.append(f'{rtsp} "{self.server}"' if self.server else rtsp)
        if self.is_xmeye():
            parts.append("XMeye")
        return ", ".join(parts)

    def is_xmeye(self):
        return self.dvrip or self.server.lower().startswith("h264dvr")


def clean(text, limit=40):
    """A device's own words made fit for a terminal and for a line of cameras.ini: one line, no control
    characters or escape sequences, no more than `limit` characters."""
    text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b.", "", str(text or ""))
    return " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text).split())[:limit].strip()


# ---------------------------------------------------------------- which addresses
def local_networks():
    """[IPv4Interface] of this machine's addresses with their networks, loopback and link-local left out.
    Qt knows them on every system; without it, the address this machine reaches others from, taken as
    one of 254."""
    found = []
    try:
        from PySide6.QtNetwork import QNetworkInterface, QAbstractSocket
        flag = QNetworkInterface.InterfaceFlag
        for nic in QNetworkInterface.allInterfaces():
            flags = nic.flags()
            if not (flags & flag.IsUp and flags & flag.IsRunning) or flags & flag.IsLoopBack:
                continue
            for entry in nic.addressEntries():
                if entry.ip().protocol() == QAbstractSocket.NetworkLayerProtocol.IPv4Protocol \
                        and 0 < entry.prefixLength() <= 32:
                    found.append(ipaddress.ip_interface(f"{entry.ip().toString()}/{entry.prefixLength()}"))
    except Exception:
        found = []
    if not found:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.0.2.1", 9))              # nothing is sent: the system only picks the route
                found = [ipaddress.ip_interface(s.getsockname()[0] + "/24")]
        except OSError:
            found = []
    return [i for i in dict.fromkeys(found) if not i.ip.is_loopback and not i.ip.is_link_local]


def hosts_of(word):
    """The addresses a word on the command line names: a network ('192.168.2.0/24'), an address, or a
    host name.  ValueError with the reason when it names none, or a range Nova does not search."""
    try:
        net = ipaddress.ip_network(word, strict=False)
    except ValueError:
        try:
            return [socket.gethostbyname(word)]
        except (OSError, UnicodeError):
            raise ValueError(f"{word}: neither a network (192.168.2.0/24), an address nor a host name that resolves")
    if net.version != 4:
        raise ValueError(f"{word}: the search is for IPv4 networks")
    if net.num_addresses == 1:
        return [str(net.network_address)]
    if net.is_global:
        raise ValueError(f"{word}: only private networks are searched as a range (name a single address elsewhere)")
    if net.num_addresses - 2 > 65534:
        raise ValueError(f"{word}: too many addresses for one search (a /16 at most)")
    return [str(h) for h in net.hosts()]


# ---------------------------------------------------------------- the search
def _window():
    """How many connections may be open at once: what the system's limit on open files leaves (raised
    to its hard limit where that is allowed), and select()'s 512 on Windows."""
    if os.name == "nt":
        return 400
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = 4096 if hard == resource.RLIM_INFINITY else min(hard, 4096)
        if soft < want:
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
                soft = want
            except (ValueError, OSError):
                pass
        return max(32, min(1024, soft - 96))
    except Exception:
        return 128


_IN_PROGRESS = {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, getattr(errno, "WSAEWOULDBLOCK", 10035)}
_ALIVE = {errno.ECONNREFUSED, getattr(errno, "WSAECONNREFUSED", 10061)}      # nobody listens, somebody is there


def connects(targets, timeout=CONNECT_S, window=None, hello=None, answer_s=None):
    """{(host, port): answer} for the targets that answered within `timeout`, the others are missing:
    False when the connection was refused (nobody listens, the host is there), True when it was made.
    With `hello(target)` giving bytes, those are sent on the connection and the answer is what came back
    within `answer_s`, up to the end of its header (b"" for a port that stays silent).  It is asked on
    the connection that tested the port: one connection to a camera, not two (one camera's RTSP
    server, after many questions in a row, answered in 0.02 s, in 3 s, or not within 6 s).  As many
    at once as the system allows."""
    window = window or _window()
    answer_s = ANSWER_S if answer_s is None else answer_s
    answers, todo = {}, list(dict.fromkeys(targets))
    if SIDE_BY_SIDE and len(todo) > window:
        # a lot of `window` to a thread, each with a selector of its own: the addresses that are not
        # there hold their place for the whole timeout, and a /22 in lots one after the other would
        # take three timeouts
        lots = [todo[i:i + window] for i in range(0, len(todo), window)]
        threads = [threading.Thread(target=lambda lot=lot: answers.update(connects(lot, timeout, window, hello, answer_s)),
                                    daemon=True) for lot in lots]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return answers
    sel = selectors.DefaultSelector()
    pending = {}                                         # socket: [target, deadline, None while connecting | bytes read]

    def done(sock, answer=None):
        if answer is not None:
            answers[pending[sock][0]] = answer
        sel.unregister(sock)
        del pending[sock]
        sock.close()

    def connected(sock, target):
        """True when the socket stays open for the answer to hello."""
        data = hello(target) if hello else None
        if not data:
            answers[target] = True
            return False
        try:
            sock.send(data)
        except OSError:
            answers[target] = b""
            return False
        return True

    try:
        while todo or pending:
            while todo and len(pending) < window:
                target = todo.pop()
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                except OSError:                          # out of sockets: wait for some to end
                    todo.append(target)
                    if not pending:
                        return answers
                    break
                sock.setblocking(False)
                rc = sock.connect_ex(target)
                if rc == 0:
                    if connected(sock, target):
                        sel.register(sock, selectors.EVENT_READ)
                        pending[sock] = [target, time.monotonic() + answer_s, b""]
                    else:
                        sock.close()
                elif rc in _IN_PROGRESS:
                    sel.register(sock, selectors.EVENT_WRITE)
                    pending[sock] = [target, time.monotonic() + timeout, None]
                else:
                    if rc in _ALIVE:
                        answers[target] = False
                    sock.close()
            if not pending:
                continue
            for key, _ in sel.select(0.05):
                sock = key.fileobj
                target, _, read = pending[sock]
                if read is None:                         # the connection: made, refused, or neither
                    err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                    if err == 0 and connected(sock, target):
                        sel.modify(sock, selectors.EVENT_READ)
                        pending[sock] = [target, time.monotonic() + answer_s, b""]
                    else:
                        done(sock, False if err in _ALIVE else None)
                    continue
                try:
                    chunk = sock.recv(4096)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    chunk = b""
                read += chunk
                pending[sock][2] = read
                if not chunk or b"\r\n\r\n" in read or len(read) > 8192:
                    done(sock, read)
            now = time.monotonic()
            for sock in [s for s, (_, end, _) in pending.items() if end <= now]:
                done(sock, pending[sock][2])             # None for a connection that never came: no answer
    finally:
        for sock in pending:
            sock.close()
        sel.close()
    return answers


def parse_probe_match(text, sender):
    """(port of the ONVIF service, name) from a device's answer to the discovery probe; None when it is
    no answer of a video device.  Prefixes differ by maker, so tags are matched by their local name."""
    if "ProbeMatch" not in text:
        return None
    types = " ".join(re.findall(r"<(?:\w+:)?Types[^>]*>([^<]*)<", text))
    scopes = html.unescape(" ".join(re.findall(r"<(?:\w+:)?Scopes[^>]*>([^<]*)<", text))).split()
    if "NetworkVideoTransmitter" not in types and not any("onvif.org" in s for s in scopes):
        return None
    port = None
    for addr in html.unescape(" ".join(re.findall(r"<(?:\w+:)?XAddrs[^>]*>([^<]*)<", text))).split():
        try:
            p = urlsplit(addr)
            if p.scheme.lower() not in ("http", "https"):
                continue
            here = p.port or (443 if p.scheme.lower() == "https" else 80)
        except ValueError:
            continue
        if p.hostname == sender:
            port = here
            break
        port = port or here                              # an address of another network of the device: its port counts
    if not port:
        return None
    said = {}
    for scope in scopes:
        m = re.search(r"onvif\.org/(name|hardware)/(.+)$", scope)
        if m:
            said.setdefault(m.group(1), clean(unquote(m.group(2))))
    name = said.get("name") or ""
    if name.lower() in ("", "unknown", "ipc", "ipcam", "camera", "hd", "onvif"):         # what a device is called by default
        name = said.get("hardware") or ""
    return port, name


def _options(target):
    host, port = target
    return f"OPTIONS rtsp://{host}:{port}/ RTSP/1.0\r\nCSeq: 1\r\nUser-Agent: Nova\r\n\r\n".encode()


def server_line(answer):
    """The Server line of an answer to RTSP's OPTIONS ('' when it has none), cut before its details
    ('DSS/6.0.3 (Build/526.3; Platform/Linux; …)'); None when the answer is not RTSP's."""
    if not isinstance(answer, bytes):
        return None
    text = answer.decode("latin-1")
    if not text.startswith("RTSP/1."):
        return None
    m = re.search(r"(?im)^Server:[ \t]*(.*?)\s*$", text)
    return clean((m.group(1) if m else "").split(" (")[0], 28)


def rtsp_server(host, port, answer_s=None):
    """server_line of what listens on one port; None when nothing does, or nothing that speaks RTSP."""
    return server_line(connects([(host, port)], hello=_options, answer_s=answer_s).get((host, port)))


def duration(count, wait=2.5):
    """About how many seconds a search of `count` addresses takes: the first packets, two tries at the
    RTSP port, the other ports of those that are there."""
    return max(wait, count / PROBE_RATE + 2 * CONNECT_S + RETRY_S + 0.5)


def search(hosts, own=(), wait=2.5, ports=RTSP_PORTS, wsd_port=WSD_GROUP[1], dvrip_port=DVRIP_PORT, answer_s=None,
           rate=PROBE_RATE):
    """[Device], by address, of what looks like a camera among `hosts`: it answered the ONVIF probe as a
    video device, or RTSP answers on one of `ports`.  `own`: this machine's addresses, from each of
    which the probe also goes to the multicast group (for a camera the direct one did not reach).
    `rate`: addresses a second for the first packet to each (PROBE_RATE says why; 0: all at once)."""
    hosts = list(dict.fromkeys(hosts))
    wanted = set(hosts)
    found = {}

    def device(ip):
        return found.setdefault(ip, Device(ip))

    socks = []

    def probe(some, groups=(), rate=0):
        """The probe to the multicast group, then to each of these addresses, `rate` of them a second
        and a hundred to a socket: a packet to an address nobody has waits in its socket's send
        buffer for an answer to ARP, and a buffer holds about 160 of them (a /22 asked through one
        socket: 167 sent, 854 refused with EAGAIN, no answer at all).  The answers are read meanwhile."""
        if not some and not groups:
            return
        began = time.monotonic()
        for i in range(0, max(len(some), 1), 100):
            try:
                udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                udp.bind(("", 0))
                udp.setblocking(False)
            except OSError:
                return
            socks.append(udp)
            if i == 0:
                # an id of its own: a device may answer a message it has seen once only, and a
                # firewall drops the answer to this one
                packet = PROBE.format(id=uuid.uuid4()).encode()
                for mine in groups:
                    try:
                        udp.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(str(mine)))
                        udp.sendto(packet, (WSD_GROUP[0], wsd_port))
                    except OSError:
                        pass
                packet = PROBE.format(id=uuid.uuid4()).encode()
            for n, h in enumerate(some[i:i + 100], i):
                if rate and began + n / rate > time.monotonic():
                    listen(began + n / rate)
                try:
                    udp.sendto(packet, (h, wsd_port))
                except OSError:
                    pass

    def listen(until):
        while socks:
            quiet = True
            for udp in socks:
                try:
                    data, (ip, _) = udp.recvfrom(65535)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    continue
                quiet = False
                match = parse_probe_match(data.decode("utf-8", "replace"), ip) if ip in wanted else None
                if match and not device(ip).onvif:
                    device(ip).onvif, device(ip).name = match
            if quiet:
                if time.monotonic() >= until:
                    return
                time.sleep(0.02)

    def hello(target):
        return _options(target) if target[1] in ports else None

    started = time.monotonic()
    probe(hosts, own, rate)                              # after this the system knows who is there
    first = connects([(h, ports[0]) for h in hosts], hello=hello, answer_s=answer_s)
    # Once more for the addresses that said nothing: one whose first question was lost on the way gets
    # the system's next a second later (from a Mac new to the network, before the first packets had
    # their pace, 54 addresses of a /22 answered within 0.7 s, 57 within 1.5 s, 60 within 3 s), and
    # Windows tells of a refusal after two seconds.
    first.update(connects([(h, ports[0]) for h in hosts if (h, ports[0]) not in first], timeout=RETRY_S,
                          hello=hello, answer_s=answer_s))
    listen(time.monotonic())
    there = [h for h in hosts if (h, ports[0]) in first or h in found]
    probe([h for h in there if h not in found])          # once more, now that these are known to be there
    said = dict(first)
    said.update(connects([(h, p) for h in there for p in tuple(ports[1:]) + (dvrip_port,)], hello=hello, answer_s=answer_s))
    listen(max(time.monotonic(), started + wait))
    for udp in socks:
        udp.close()
    for h in there:
        for p in ports:
            server = server_line(said.get((h, p)))
            if server is not None:
                device(h).rtsp_port, device(h).server = p, server
                break
        else:
            # RTSP's own port, open and silent: a camera whose server is busy (one of them answered
            # OPTIONS in 0.02 s, in 3 s, or not within 6 s, after many questions in a row)
            if said.get((h, ports[0])) == b"":
                device(h).rtsp_port, device(h).silent = ports[0], True
    for h, d in found.items():
        d.dvrip = said.get((h, dvrip_port)) is True
    return sorted(found.values(), key=lambda d: tuple(int(x) for x in d.ip.split(".")))


# ---------------------------------------------------------------- one RTSP question
def _answer(sock):
    """(status, {header: [values]}, body) of one RTSP response."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk or len(data) > 65536:
            raise OSError("no RTSP answer")
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    m = re.match(r"RTSP/1\.\d\s+(\d{3})", lines[0])
    if not m:
        raise OSError("no RTSP answer")
    headers = {}
    for line in lines[1:]:
        k, sep, v = line.partition(":")
        if sep:
            headers.setdefault(k.strip().lower(), []).append(v.strip())
    try:
        length = min(int(headers.get("content-length", ["0"])[0]), 65536)
    except ValueError:
        length = 0
    while len(body) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk
    return int(m.group(1)), headers, body.decode("utf-8", "replace")


def _authorization(method, url, user, pwd, challenges):
    """The Authorization line for the server's challenge, Digest before Basic; None for one Nova has
    no answer to."""
    digest = next((c for c in challenges if c.lower().startswith("digest")), None)
    if digest:
        f = {k.lower(): v1 or v2 for k, v1, v2 in re.findall(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]*))', digest)}
        algo = f.get("algorithm", "MD5").upper()
        h = {"MD5": hashlib.md5, "SHA-256": hashlib.sha256}.get(algo)
        if h is None or "realm" not in f or "nonce" not in f:
            return None

        def hx(text):
            return h(text.encode("utf-8")).hexdigest()

        ha1, ha2 = hx(f"{user}:{f['realm']}:{pwd}"), hx(f"{method}:{url}")
        line = f'Digest username="{user}", realm="{f["realm"]}", nonce="{f["nonce"]}", uri="{url}", '
        if "auth" in [q.strip() for q in f.get("qop", "").split(",")]:
            cnonce = uuid.uuid4().hex[:16]
            response = hx(f"{ha1}:{f['nonce']}:00000001:{cnonce}:auth:{ha2}")
            line += f'qop=auth, nc=00000001, cnonce="{cnonce}", '
        else:
            response = hx(f"{ha1}:{f['nonce']}:{ha2}")
        line += f'response="{response}"'
        if "algorithm" in f:
            line += f", algorithm={f['algorithm']}"
        if "opaque" in f:
            line += f', opaque="{f["opaque"]}"'
        return line
    if any(c.lower().startswith("basic") for c in challenges):
        return "Basic " + base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("ascii")
    return None


def describe(url, user="", pwd="", timeout=ASK_S):
    """(status, SDP, answered): what the RTSP server says to DESCRIBE of `url` (rtsp://host[:port]/path,
    without credentials).  200 with the stream's description; 401 with answered=True when it refused
    this user name and password, with False when none were given or it asks in a way Nova cannot
    answer; 404 and the like for a path it does not have; 0 when it does not answer at all."""
    try:
        p = urlsplit(url)
        target = (p.hostname, p.port or 554)
    except ValueError:
        return 0, "", False

    def ask(sock, cseq, authorization=None):
        lines = [f"DESCRIBE {url} RTSP/1.0", f"CSeq: {cseq}", "Accept: application/sdp", "User-Agent: Nova"]
        if authorization:
            lines.append("Authorization: " + authorization)
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
        return _answer(sock)

    try:
        sock = socket.create_connection(target, timeout)
    except OSError:
        return 0, "", False
    try:
        sock.settimeout(timeout)
        status, headers, body = ask(sock, 1)
        if status != 401 or not (user or pwd):
            return status, body, False
        authorization = _authorization("DESCRIBE", url, user, pwd, headers.get("www-authenticate", []))
        if not authorization:
            return status, body, False
        try:
            status, headers, body = ask(sock, 2, authorization)
        except OSError:                                  # a server that closes after its 401
            sock.close()
            sock = socket.create_connection(target, timeout)
            sock.settimeout(timeout)
            status, headers, body = ask(sock, 2, authorization)
        return status, body, True
    except OSError:
        return 0, "", False
    finally:
        sock.close()


_CODECS = {"H264": "H.264", "H265": "H.265", "HEVC": "H.265", "JPEG": "MJPEG", "MP4V-ES": "MPEG-4",
           "MPEG4-GENERIC": "AAC", "MP4A-LATM": "AAC", "PCMA": "G.711 A-law", "PCMU": "G.711 µ-law", "OPUS": "Opus"}


def media(sdp):
    """'H.265, sound G.711 A-law' from a stream's description; '' when it names no video."""
    said = {}
    kind = None
    for line in sdp.splitlines():
        m = re.match(r"m=(video|audio)\s+\d+\s+\S+\s+(\d+)", line)
        if m:
            kind = m.group(1)
            static = {"0": "PCMU", "8": "PCMA", "26": "JPEG"}.get(m.group(2))
            if static:
                said.setdefault(kind, static)
            continue
        m = re.match(r"a=rtpmap:\d+\s+([^/\s]+)", line)
        if m and kind:
            said.setdefault(kind, m.group(1))
    if "video" not in said:
        return ""

    def named(codec):
        return _CODECS.get(codec.upper(), clean(codec, 16))

    return named(said["video"]) + (f", sound {named(said['audio'])}" if "audio" in said else "")


# ---------------------------------------------------------------- the stream of a camera
def _resolver(scheme, user, pwd, host, port):
    """auth.py's answer for a shortcut scheme: the rtsp:// URL with the credentials, or None.  The path
    is the device's own: one with a space, a line end or a control character in it is no URL (and
    would be a line of its own in cameras.ini)."""
    import auth
    url = auth.find_stream(scheme, user, pwd, host, port)
    return url if url and not re.search(r"[\x00-\x20\x7f]", url) else None


def _without_credentials(url):
    p = urlsplit(url)
    host = p.hostname or ""
    host = f"[{host}]" if ":" in host else host
    return p._replace(netloc=host + (f":{p.port}" if p.port else "")).geturl()


def _first_path(base, paths, user, pwd):
    """(path, SDP) of the first of `paths` that the server gives to this user; (None, "") when it has
    none of them, (REFUSED, "") at the first refusal.  One question at a time: a camera's RTSP server
    answers several at once more slowly than one after the other (17 paths on one of them: 7 s in
    turn, 22 s two at a time), and a refusal must be the last question."""
    for path in paths:
        status, sdp, answered = describe(base + path, user, pwd)
        if status == 200 and "m=" in sdp:
            return path, sdp
        if status == 401 and answered:
            return REFUSED, ""
    return None, ""


def stream_url(dev, user, pwd, say=lambda text: None, paths=RTSP_PATHS):
    """(url, what it sends) for the main stream of a device with this user name and password, the
    credentials in the URL; (None, reason) when none is found.  REFUSED as the reason ends it at the
    first refusal: every further question would be one more failed login on the camera's count."""
    import auth
    port = dev.rtsp_port or 554
    base = f"rtsp://{dev.ip}" + ("" if port == 554 else f":{port}")

    def accepted(url, bare=None):
        """(True, SDP) when the server gives this stream to the user; (None, …) when it refuses them."""
        status, sdp, answered = describe(bare or _without_credentials(url), user, pwd)
        if status == 200 and "m=" in sdp:
            return True, sdp
        return (None if status == 401 and answered else False), sdp

    status, root_sdp, answered = describe(base + "/", user, pwd)
    if status == 401 and answered:
        return None, REFUSED
    if status == 0 and not dev.onvif:
        return None, f"RTSP does not answer on port {port}"
    root = status == 200 and "m=" in root_sdp
    whatever = root and describe(f"{base}/nova-find-{uuid.uuid4().hex[:8]}", user, pwd)[0] == 200

    tried = []
    if dev.onvif:
        say("asking its ONVIF service")
        tried.append("ONVIF")
        url = _resolver("onvif", user, pwd, dev.ip, dev.onvif)
        if url:
            ok, sdp = accepted(url)
            if ok:
                return url, media(sdp)
            if ok is None:
                return None, REFUSED
    if dev.is_xmeye():
        tried.append("XMeye's form")
        login = "&".join(f"{k}={auth._qpath(v)}" for k, v in (("user", user), ("password", pwd)))
        path = f"/{login}&channel=1&stream=0.sdp?real_stream"       # the credentials are in the path
        ok, sdp = accepted(None, base + path)
        if ok:
            return auth._rtsp_url(user, pwd, dev.ip, port, path), media(sdp)
        if ok is None:
            return None, REFUSED
    if not whatever:
        say("asking for the usual paths")
        tried.append(f"{len(paths)} usual paths")
        path, sdp = _first_path(base, paths, user, pwd)
        if path is REFUSED:
            return None, REFUSED
        if path:
            return auth._rtsp_url(user, pwd, dev.ip, port, path), media(sdp)
    if not dev.onvif:
        say("looking for an ONVIF service")
        tried.append("ONVIF on the usual ports")
        url = _resolver("onvif", user, pwd, dev.ip, None)
        if url:
            ok, sdp = accepted(url)
            if ok:
                return url, media(sdp)
            if ok is None:
                return None, REFUSED
    if root:
        return auth._rtsp_url(user, pwd, dev.ip, port, "/"), media(root_sdp)
    return None, "no stream found (" + ", ".join(tried) + "): its RTSP URL can be written into cameras.ini by hand"


# ---------------------------------------------------------------- cameras.ini
RESERVED = ("defaults", "env")


def url_host(url):
    """The host a camera URL names (the user info ends at the last '@' before the path), lower case; ''
    when it names none (a file, a device)."""
    m = re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://([^/?#]*)", str(url or ""))
    if not m:
        return ""
    host = m.group(1).rpartition("@")[2]
    if host.startswith("["):
        return host[1:host.find("]")].lower() if "]" in host else ""
    return host.split(":")[0].lower()


def listed(cameras):
    """{address: [camera ids]} of the cameras a configuration has, a host name looked up."""
    by = {}
    for cam in cameras:
        host = url_host(cam.url)
        if not host:
            continue
        try:
            ipaddress.ip_address(host)
        except ValueError:
            try:
                host = socket.gethostbyname(host)
            except (OSError, UnicodeError):
                continue
        by.setdefault(host, []).append(cam.id)
    return by


def id_refusal(cid, taken):
    """Why this cannot be a new camera's id, None when it can: it names a log file and is typed on the
    command line, and two ids that differ in case would share a log on Windows and macOS."""
    if not cid:
        return "empty"
    if re.search(r"[/\\\[\]\s\x00-\x1f\x7f]", cid) or cid[0] in ".-" or len(cid.encode("utf-8")) > 64:
        return "an id has no space, '/', '\\' or brackets, does not start with '.' or '-' and is 64 bytes at most"
    if cid.lower() in RESERVED:
        return f"[{cid}] is a section of cameras.ini itself"
    if cid.lower() in {t.lower() for t in taken}:
        return f"[{cid}] is in cameras.ini already"
    return None


def free_id(wish, taken):
    cid, n = wish, 1
    while id_refusal(cid, taken):
        n += 1
        cid = f"{wish}_{n}"
    return cid


def sections(path):
    """The section names a cameras.ini has, as written; [] for a file that is not there."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            return re.findall(r"(?m)^\[([^\]\r\n]+)\]", f.read())
    except OSError:
        return []


def starting_text(example):
    """What a new cameras.ini begins with: the template up to its first camera, so that [defaults] with
    its presets and [env] are there."""
    try:
        with open(example, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError:
        return "[defaults]\nopts =\n"
    for m in re.finditer(r"(?m)^\[([^\]\r\n]+)\][^\n]*\n", text):
        if m.group(1).lower() not in RESERVED:
            text = text[:m.start()]
            break
    return text.rstrip() + "\n"


def add_camera(path, cid, name, url, example=None):
    """Append one camera to cameras.ini, which is made from the template when it is not there (readable
    by its owner only: it holds passwords).  The id is checked by the caller; the URL and the name
    come in part from a device, and one that breaks out of its line is refused: an `opts =` line of a
    device's making would be options for that camera."""
    if re.search(r"[\x00-\x20\x7f]", url) or not re.match(r"^rtsps?://", url):
        raise ValueError("not an RTSP URL in one piece")
    name = clean(name, 60)
    entry = f"[{cid}]\n" + (f"name = {name}\n" if name and name != cid else "") + f"url = {url}\n"
    if os.path.exists(path):
        with open(path, "rb") as f:
            old = f.read()
        gap = b"" if not old else b"\n" if old.endswith(b"\n") else b"\n\n"
        with open(path, "ab") as f:
            f.write(gap + entry.encode("utf-8"))
        return False
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(starting_text(example) + "\n" + entry)
    return True


# ---------------------------------------------------------------- the command
def _numbers(answer, count):
    """The 1-based numbers a line names ('1 3-5, 7'), in order; None for a line that names something else."""
    picked = []
    for word in answer.replace(",", " ").split():
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", word)
        if not m:
            return None
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if not 1 <= a <= b <= count:
            return None
        picked.extend(range(a, b + 1))
    return list(dict.fromkeys(picked))


def main(config, words, cameras, example, out=print, ask=input, ask_hidden=getpass.getpass, interactive=None):
    """`nova find [NETWORK | ADDRESS ...]`.  Returns the exit code."""
    from util import mask_url
    name_of = os.path.basename(config)
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    try:
        if words:
            hosts, own = [h for w in words for h in hosts_of(w)], ()
            where = ", ".join(words)
        else:
            nets = local_networks()
            if not nets:
                out("nova find: this machine has no network to search (name one: nova find 192.168.1.0/24)")
                return 1
            small = [n for n in nets if n.network.num_addresses - 2 <= MAX_HOSTS]
            for n in nets:
                if n not in small:
                    out(f"note: {n.network} has more than {MAX_HOSTS} addresses and is not searched "
                        f"(name a part of it: nova find {n.ip}/24)")
            if not small:
                return 1
            own = [n.ip for n in small]
            mine = {str(n.ip) for n in nets}
            hosts = [str(h) for n in dict.fromkeys(i.network for i in small) for h in n.hosts() if str(h) not in mine]
            where = ", ".join(str(n) for n in dict.fromkeys(i.network for i in small))
    except ValueError as e:
        out(f"nova find: {e}")
        return 1
    hosts = list(dict.fromkeys(hosts))
    out(f"Looking for cameras on {where} ({len(hosts)} address{'es' if len(hosts) != 1 else ''}, "
        f"about {duration(len(hosts)):.0f} s) ...")
    t0 = time.monotonic()
    found = search(hosts, own)
    if not found:
        out(f"None found in {time.monotonic() - t0:.0f} s: nothing answered an ONVIF probe or RTSP on port "
            f"{', '.join(map(str, RTSP_PORTS))}. A camera on another network can be named: nova find ADDRESS")
        return 0
    have = listed(cameras)
    out(f"{len(found)} found in {time.monotonic() - t0:.0f} s:\n")
    wide = max(len(d.what()) for d in found)
    out(f"  {'#':>2}  {'address':<15}  {'what answers':<{wide}}  {name_of}")
    for i, d in enumerate(found, 1):
        out(f"  {i:>2}  {d.ip:<15}  {d.what():<{wide}}  {', '.join(have.get(d.ip, [])) or 'new'}")
    new = [i for i, d in enumerate(found, 1) if d.ip not in have]
    out("")
    if not interactive:
        out(f"{len(new)} of them {'is' if len(new) == 1 else 'are'} not in {name_of}. "
            f"Run nova find in a terminal to add cameras: it asks for their user name and password.")
        return 0
    added = []
    try:
        hint = f'"all" for the {len(new)} new' if new else "none of them is new"
        while True:
            answer = ask(f"Add which to {name_of}? Numbers ({hint}; Enter: none): ").strip().lower()
            picked = new if answer == "all" else _numbers(answer, len(found))
            if picked is not None:
                break
            out(f'  numbers from 1 to {len(found)}, as in "1 3-5", or "all"')
        if not picked:
            out("Nothing added.")
            return 0
        taken = sections(config)
        these = "this camera" if len(picked) == 1 else f"these {len(picked)} cameras"
        user = ask(f"User name for {these} [admin]: ").strip() or "admin"
        pwd = ask_hidden("Password (not shown" + ("" if len(picked) == 1 else "; a camera that refuses it is asked for its own") + "): ")
        for n in picked:
            d = found[n - 1]
            out(f"\n{d.ip}  {d.what()}")
            if d.ip in have:
                out(f"  in {name_of} already as {', '.join(have[d.ip])}: this adds one more camera for it")
            if not d.rtsp_port:
                out("  it answered ONVIF and has no RTSP port open among " + ", ".join(map(str, RTSP_PORTS)))
            u, p, url, said = user, pwd, None, ""
            for attempt in range(TRIES):
                url, said = stream_url(d, u, p, say=lambda text: out(f"  {text} ..."))
                if url or said != REFUSED:
                    break
                if attempt + 1 == TRIES:
                    out(f"  refused again: left out after {TRIES} tries (cameras lock an account that is refused too often)")
                    break
                out("  the camera refused this user name and password")
                typed = ask("  its own user name (Enter: leave this camera out): ").strip()
                if not typed:
                    break
                u, p = typed, ask_hidden("  its password: ")
            if not url:
                out(f"  {said}" if said != REFUSED else "  left out")
                continue
            out(f"  found: {mask_url(url)}" + (f"  ({said})" if said else ""))
            wish = free_id("cam" + d.ip.split(".")[-1], taken)
            while True:
                cid = ask(f"  id, as typed after nova start [{wish}]: ").strip() or wish
                why = id_refusal(cid, taken)
                if not why:
                    break
                out(f"  {why}")
            name = clean(ask(f"  name of the window [{d.name or cid}]: "), 60) or d.name or cid
            try:
                made = add_camera(config, cid, name, url, example)
            except (OSError, ValueError) as e:
                out(f"  not written: {e}")
                continue
            taken.append(cid)
            added.append(cid)
            out(f"  added [{cid}] to {name_of}"
                + (f" (made from {os.path.basename(example)}: look at the presets under [defaults])" if made else ""))
    except (KeyboardInterrupt, EOFError):
        out("")
    if added:
        out(f"\n{len(added)} added. nova start {added[0]} opens {'it' if len(added) == 1 else 'the first'}; "
            f"nova list has them all.")
    else:
        out("\nNothing added.")
    return 0
