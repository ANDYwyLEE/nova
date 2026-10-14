import os
import re
import sys
import time
import uuid
import html
import json
import errno
import base64
import socket
import hashlib
import logging
import warnings
import selectors
import threading
import ipaddress
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse, urlsplit, urlunsplit, quote, unquote
from xml.sax.saxutils import escape as _xml_escape

try: import requests
except ImportError:
    print("Error: requests is required. Run 'pip install requests'")
    sys.exit(1)

try: import xmltodict
except ImportError:
    print("Error: xmltodict is required. Run 'pip install xmltodict'")
    sys.exit(1)

import logsync  # installs logging.verbose / logging.trace
from util import mask_url

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(CURRENT_DIR, ".uri_cache.json")
CACHE_EXPIRE_DAYS = 1

# Shortcut schemes -> default port of what Nova talks to first: the device's management interface
# (ONVIF: probed; XMeye: DVRIP; Hikvision, Dahua, Axis: HTTP; Reolink: HTTPS then HTTP), or the RTSP
# port itself for the schemes that only build a URL (Tapo, Uniview).  Resolvers: _RESOLVERS below.
SHORTCUT_DEFAULT_PORTS = {"onvif": None, "xmeye": 34567, "hik": 80, "dahua": 80, "reolink": None,
                          "axis": 80, "tapo": 554, "unv": 554}
# OEM lines that run their maker's firmware and HTTP API
SCHEME_ALIASES = {"amcrest": "dahua", "annke": "hik", "hilook": "hik"}
# the STREAM part of a shortcut URI: 0 main, 1 sub, 2 third
STREAM_NAMES = {"": 0, "0": 0, "m": 0, "main": 0, "1": 1, "s": 1, "sub": 1, "2": 2, "t": 2, "third": 2, "ext": 2}


def shortcut_scheme(url):
    """'hik' for 'hik://...' (and for its alias 'annke://...'), None for anything that is no shortcut."""
    s = str(url or "")
    scheme = s.split("://", 1)[0].lower() if "://" in s else ""
    scheme = SCHEME_ALIASES.get(scheme, scheme)
    return scheme if scheme in SHORTCUT_DEFAULT_PORTS else None


# --- Stop / quiet support ----------------------------------------------------------------
class ResolveStopped(BaseException):
    """should_stop() said yes.  A BaseException, so the resolvers' `except Exception` handlers for
    network errors let it through to resolve_input_uri()."""

_tls = threading.local()

def _fail(msg):
    """A resolution failure: ERROR, or DEBUG when the caller already reported the same failure."""
    (logging.debug if getattr(_tls, "quiet", False) else logging.error)(msg)

def _call(should_stop, fn, *args, **kwargs):
    """Run a blocking network call.  With should_stop it runs on a helper thread that is abandoned as
    soon as should_stop() is true (the call then ends on its own timeout), so stop() is never held up
    by a camera that does not answer."""
    if should_stop is None:
        return fn(*args, **kwargs)
    if should_stop():
        raise ResolveStopped()
    box = {}
    done = threading.Event()
    def run():
        try: box["r"] = fn(*args, **kwargs)
        except BaseException as e: box["e"] = e
        finally: done.set()
    threading.Thread(target=run, name="nova-resolve", daemon=True).start()
    while not done.wait(0.2):
        if should_stop():
            raise ResolveStopped()
    if "e" in box:
        raise box["e"]
    return box["r"]

def _sleep(seconds, should_stop):
    end = time.monotonic() + seconds
    while True:
        if should_stop is not None and should_stop():
            raise ResolveStopped()
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(0.1, left))


# --- Cache (passwords are never written to disk) ---------------------------------------
def _cache_key(uri):
    return hashlib.sha256(uri.encode("utf-8")).hexdigest()[:32]

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                cache = json.load(f)
            now = datetime.now()
            valid_cache = {}
            changed = False
            for k, v in cache.items():
                try:
                    fresh = now - datetime.fromisoformat(v['timestamp']) < timedelta(days=CACHE_EXPIRE_DAYS)
                except Exception:
                    fresh = False
                if fresh and isinstance(v.get('url'), str):
                    valid_cache[k] = v
                else:
                    changed = True
            if changed:
                save_all_cache(valid_cache)
            return valid_cache
        except Exception:
            return {}
    return {}

def save_all_cache(cache_data):
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache_data, f, indent=4)
    try: os.chmod(CACHE_FILE, 0o600)
    except OSError: pass

def _masked_for_cache(url):
    """mask_url(url) when it really hides the password (in the user info and in a password= parameter),
    else None."""
    m = mask_url(url)
    try:
        if urlsplit(m).password not in (None, "***"):
            return None
    except ValueError:
        return None
    if re.search(r'(?i)\b(password|passwd|pwd|pass)=(?!\*\*\*)', m):
        return None
    return m

def save_cache(uri, resolved_url):
    masked = _masked_for_cache(resolved_url)
    if masked is None:
        logging.debug("[Resolve] resolved URI not cached: its password cannot be masked")
        return
    cache = load_cache()
    cache[_cache_key(uri)] = {
        "url": masked,                      # password replaced by ***, restored from the input URI on load
        "timestamp": datetime.now().isoformat()
    }
    save_all_cache(cache)

def _restore_password(masked_url, pwd):
    if pwd is None: return masked_url
    s = masked_url.replace(":***@", f":{quote(pwd, safe='')}@", 1)
    s = re.sub(r'(?i)\b(password|passwd|pwd|pass)=\*\*\*', lambda m: f"{m.group(1)}={_qpath(pwd)}", s)
    return s


# --- Helpers ---------------------------------------------------------------------------
def _local(k: str) -> str:
    """'tds:GetCapabilitiesResponse' -> 'GetCapabilitiesResponse' (namespace prefix independent)."""
    return k.split(':', 1)[-1] if ':' in k else k

def _find(d, name):
    """Depth-first search for an element by local name in an xmltodict tree."""
    if isinstance(d, dict):
        for k, v in d.items():
            if _local(k) == name:
                return v
        for v in d.values():
            r = _find(v, name)
            if r is not None:
                return r
    elif isinstance(d, list):
        for v in d:
            r = _find(v, name)
            if r is not None:
                return r
    return None

def _text(v):
    if isinstance(v, dict):
        return v.get('#text')
    return v

def _xml_text(s):
    """Text cut out of XML by a regex: CDATA unwrapped, entities (&amp; ...) decoded."""
    s = (s or "").strip()
    m = re.fullmatch(r'<!\[CDATA\[(.*)\]\]>', s, re.S)
    return m.group(1).strip() if m else html.unescape(s)

_SECRET_XML_KEYS = {"Password", "Nonce", "Created"}

def _redact(d):
    """WS-Security digest, nonce and timestamp are enough to attack the password offline: never log them."""
    if isinstance(d, list):
        return [_redact(v) for v in d]
    if isinstance(d, dict):
        return {k: ("***" if _local(k) in _SECRET_XML_KEYS else _redact(v)) for k, v in d.items()}
    return d

def log_xml_pretty(action, http_headers, xml_input):
    if logging.getLogger().getEffectiveLevel() > 5: return

    logging.trace(f"[ONVIF] {action}")
    h_info = {k: v for k, v in http_headers.items() if k.lower() != "authorization"}
    logging.trace(f"[ONVIF] HTTP Headers: {json.dumps(h_info)}")
    try:
        full_dict = xmltodict.parse(xml_input)
        env = next(iter(full_dict.values()))
        h_obj, b_obj = {}, {}
        for k, v in env.items():
            if 'Header' in k: h_obj = v
            if 'Body' in k: b_obj = v
        logging.trace("[ONVIF] XML Header (XML -> JSON):\n" + json.dumps(clean_dict(_redact(h_obj)), indent=4, ensure_ascii=False))
        logging.trace("[ONVIF] XML Body (XML -> JSON):\n" + json.dumps(clean_dict(_redact(b_obj)), indent=1, ensure_ascii=False))
    except Exception as e:
        logging.trace(f"[ONVIF] XML Parse Error: {type(e).__name__}")


def check_port(ip, port, timeout=1):
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout):
            return True
    except Exception:
        return False

_CONNECT_IN_PROGRESS = {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY,
                        getattr(errno, "WSAEWOULDBLOCK", 10035)}

def _open_ports(host, ports, timeout=1.5, should_stop=None):
    """The ports (in the given order) that accept a TCP connection; all are tried at the same time,
    so probing 25 ports of a camera that is off takes `timeout`, not 25 x timeout."""
    infos = _call(should_stop, socket.getaddrinfo, host, None, 0, socket.SOCK_STREAM)
    sel = selectors.DefaultSelector()
    pending = []
    found = set()
    try:
        for port in ports:
            for family, stype, proto, _, sa in infos:
                try:
                    sock = socket.socket(family, stype, proto)
                except OSError:
                    continue
                sock.setblocking(False)
                rc = sock.connect_ex((sa[0], port) + tuple(sa[2:]))
                if rc == 0:
                    found.add(port); sock.close()
                elif rc in _CONNECT_IN_PROGRESS:
                    sel.register(sock, selectors.EVENT_WRITE, port); pending.append(sock)
                else:
                    sock.close()
        deadline = time.monotonic() + timeout
        while pending:
            if should_stop is not None and should_stop():
                raise ResolveStopped()
            left = deadline - time.monotonic()
            if left <= 0:
                break
            for key, _ in sel.select(min(0.2, left)):
                sock = key.fileobj
                if sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0:
                    found.add(key.data)
                sel.unregister(sock); pending.remove(sock); sock.close()
    finally:
        for sock in pending:
            sock.close()
        sel.close()
    return [p for p in ports if p in found]


def clean_dict(d):
    if isinstance(d, list):
        return [clean_dict(v) for v in d]
    if isinstance(d, dict):
        return {k: clean_dict(v) for k, v in d.items()
                if not k.startswith('@') and k not in ['xmlns', 'xsi']}
    return d if d is not None else ''


def _valid_port(v):
    """int 1..65535 from a port string, else None (also for '554@host:80' and the like)."""
    s = str(v).strip() if v is not None else ""
    if not re.fullmatch(r'[0-9]{1,5}', s):
        return None
    n = int(s)
    return n if 1 <= n <= 65535 else None

def _url_host(host):
    """Host as written in a URL: IPv6 literals in brackets."""
    return f"[{host}]" if ":" in host and not host.startswith("[") else host

def _same_host(a, b):
    a, b = (a or "").strip("[]").rstrip(".").lower(), (b or "").strip("[]").rstrip(".").lower()
    if not a or not b:
        return False
    try:
        return ipaddress.ip_address(a.split("%")[0]) == ipaddress.ip_address(b.split("%")[0])
    except ValueError:
        return a == b

def _qpath(s):
    """A user name or password inside a URL path (xmeye's user= and password= parameters). Only what
    would end the parameter is encoded; the rest goes as typed, as it always did, because a device
    that does not decode its path would reject an encoded '@' or space."""
    return s.replace("&", "%26").replace("#", "%23").replace("?", "%3F")

def parse_uri(uri, default_port, default_stream='0'):
    """scheme://user:password@host[:port][/[channel/]stream].  The user info ends at the *last* '@' (a
    password may contain '@' or '/'), user and password are percent-decoded, IPv6 hosts are written
    [addr].  'ip' is the bare host, 'host' its URL form; 'channel' the recorder channel (1-based,
    default 1, 'channel_given' tells whether it was written); 'stream_idx' 0 main / 1 sub / 2 third
    (STREAM_NAMES) or, for an ONVIF profile, any index; 'stream' the text as written."""
    m = re.match(r'^[A-Za-z][A-Za-z0-9+.\-]*://(.*)$', uri or "", re.S)
    if not m: return None
    userinfo, at, hostpart = m.group(1).rpartition('@')
    if not at: return None
    user, colon, pwd = userinfo.partition(':')
    if not colon: return None
    hostport, _, path = hostpart.partition('/')
    if hostport.startswith('['):
        end = hostport.find(']')
        if end < 0: return None
        ip, after = hostport[1:end], hostport[end + 1:]
        if after and not after.startswith(':'): return None
        port_s = after[1:]
    else:
        ip, _, port_s = hostport.partition(':')
    if not ip: return None
    if port_s:
        port = _valid_port(port_s)
        if port is None: return None
    else:
        port = default_port
    parts = [p for p in path.split('/') if p] if path else []
    if len(parts) > 2: return None
    channel = 1
    if len(parts) == 2:
        if not re.fullmatch(r'[1-9][0-9]{0,3}', parts[0]): return None
        channel = int(parts[0])
    stream_val = parts[-1] if parts else default_stream
    key = stream_val.strip().lower()
    if key in STREAM_NAMES:
        stream_idx = STREAM_NAMES[key]
    elif re.fullmatch(r'[0-9]{1,3}', key):
        stream_idx = int(key)           # an ONVIF profile index; the vendor resolvers refuse > 2
    else:
        return None
    return {
        'user': unquote(user), 'pwd': unquote(pwd),
        'ip': ip, 'host': _url_host(ip), 'port': port,
        'channel': channel, 'channel_given': len(parts) == 2,
        'stream': stream_val, 'stream_idx': stream_idx,
    }


def _device_port(value, tag, ip):
    """(port, trusted) for an RTSP port the device reported.  Not a port number -> (554, False): the
    value is spliced into the URL that carries the password, so '554@other-host:80' must never pass."""
    if value is None or str(value).strip() == "":
        return 554, True
    port = _valid_port(value)
    if port is None:
        logging.warning(f"{tag} ⚠️ {ip} reported an invalid RTSP port {mask_url(str(value))[:40]!r}; using 554")
        return 554, False
    return port, True

def _rtsp_url(user, pwd, host, port, path, scheme="rtsp"):
    auth = f"{quote(user, safe='')}:{quote(pwd, safe='')}@" if (user or pwd) else ""
    hp = host if (scheme == "rtsp" and port in (None, 554)) else f"{host}:{port}"
    return f"{scheme}://{auth}{hp}{path}"

def clean_rtsp_554_url(url):
    """rtsp://host:554/... -> rtsp://host/... (IPv6 hosts keep their brackets)."""
    if not url: return ''
    parsed = urlparse(url)
    if parsed.scheme.lower() == "rtsp" and parsed.port == 554:
        userinfo, at, _ = parsed.netloc.rpartition('@')
        parsed = parsed._replace(netloc=f"{userinfo}{at}{_url_host(parsed.hostname)}")
    return urlunparse(parsed)

def _with_credentials(raw_url, user, pwd):
    scheme, sep, rest = raw_url.partition("://")
    if not sep or "@" in rest.split("/", 1)[0] or not (user or pwd):
        return raw_url
    return f"{scheme}://{quote(user, safe='')}:{quote(pwd, safe='')}@{rest}"

def _check_stream_uri(raw, info, tag="[ONVIF]"):
    """A stream URI from a device, made safe to carry the password: rtsp/rtsps only, no user info of its
    own, valid port, and the camera's host (a URI naming another host is pointed back at the camera:
    NAT / a DNS name the device does not know, or a device steering the password elsewhere).
    Returns the URI without credentials, or None."""
    s = _xml_text(raw)
    why = None
    try:
        p = urlsplit(s)
        scheme = p.scheme.lower()
        if scheme not in ("rtsp", "rtsps"):
            why = "not an rtsp:// URI"
        elif "@" in p.netloc:
            why = "it carries credentials of its own"
        else:
            host, port = p.hostname, p.port
            if port is not None and not 1 <= port <= 65535:
                why = "invalid port"
            elif not host:
                why = "no host"
    except ValueError:
        why = "malformed URI"
    if why:
        logging.warning(f"{tag} ⚠️ ignoring stream URI {mask_url(s)[:120]!r}: {why}")
        return None
    if not _same_host(host, info['ip']):
        logging.info(f"{tag} stream URI names host {host}; connecting to the camera's address {info['ip']} instead")
        host = info['ip']
    hp = _url_host(host) + (f":{port}" if port and not (scheme == "rtsp" and port == 554) else "")
    return urlunsplit((scheme, hp, p.path, p.query, ""))

def _points_to_camera(url, info):
    """Last check before a URL with the password is handed to FFmpeg: rtsp(s), one user info, the
    camera's host, a valid port."""
    try:
        p = urlsplit(url)
        if p.scheme.lower() not in ("rtsp", "rtsps"):
            return False
        if "@" in p.netloc.rpartition("@")[0]:
            return False
        host, port = p.hostname, p.port
    except ValueError:
        return False
    if port is not None and not 1 <= port <= 65535:
        return False
    return _same_host(host, info['ip'])


#========================================
def resolve_input_uri(uri, use_cache=True, should_stop=None, quiet=False):
    """A shortcut URI (onvif:// xmeye:// hik:// dahua:// reolink:// axis:// tapo:// unv:// and the
    aliases in SCHEME_ALIASES) -> rtsp:// URL with credentials (None on failure); any other URL is
    returned as it is.  use_cache=False asks the device again (the cache is refreshed); should_stop()
    is polled between and during network calls; quiet=True logs failures at DEBUG (the caller reports
    repeats)."""
    scheme = shortcut_scheme(uri)
    if scheme is None:
        return uri
    written = uri.split("://", 1)[0].lower()
    info = parse_uri(uri, SHORTCUT_DEFAULT_PORTS[scheme])
    if not info:
        (logging.debug if quiet else logging.error)(
            f"[Resolve] ❌ cannot parse the {written}:// URI; expected {written}://user:password@host[:port]"
            f"[/CHANNEL][/STREAM] with STREAM 0|main, 1|sub or 2|third (a ':' in the user name must be written %3A; "
            f"the password shows as *** here)")
        return None
    key = _cache_key(uri)
    if use_cache:
        entry = load_cache().get(key)
        if entry:
            cached_url = _restore_password(entry["url"], info['pwd'])
            if _points_to_camera(cached_url, info):
                logging.info(f"[Resolve] ✅ Found cached URI: {mask_url(cached_url)}")
                return cached_url
            logging.warning(f"[Resolve] ⚠️ ignoring a cached URI that does not point to {info['ip']}")
    _tls.quiet = quiet
    try:
        resolved, from_device = _RESOLVERS[scheme](info, should_stop)
    except ResolveStopped:
        logging.debug(f"[Resolve] {written}:// resolution stopped")
        return None
    finally:
        _tls.quiet = False
    if resolved and not _points_to_camera(resolved, info):
        logging.error(f"[Resolve] ❌ refusing a resolved URI that does not point to {info['ip']}: {mask_url(resolved)}")
        return None
    if resolved:
        if from_device:
            save_cache(uri, resolved)
        else:
            logging.debug("[Resolve] the URI is not the device's own answer; not cached")
    return resolved

class ONVIFClient:
    def __init__(self, ip, port, user, pwd, is_probing=False, should_stop=None):
        self.ip, self.port, self.user, self.pwd = ip, port, user, pwd
        self.is_probing = is_probing
        self.should_stop = should_stop
        self.base_url = f"http://{_url_host(ip)}:{port}/onvif"
        self.device_url = f"{self.base_url}/device_service"
        self.media_url = f"{self.base_url}" # Not fixed yet
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/soap+xml; charset=utf-8",
            "Connection": "close",
        })

    def set_media_url(self, url):
        self.media_url = url

    def get_auth_xml(self):
        created = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        nonce = uuid.uuid4().bytes
        digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + self.pwd.encode()).digest()).decode()
        return f'''<Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"><UsernameToken><Username>{_xml_escape(self.user)}</Username><Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password><Nonce>{base64.b64encode(nonce).decode()}</Nonce><Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created></UsernameToken></Security>'''

    def request(self, body_xml, action_name, service_type="media"):
        auth_header = self.get_auth_xml()
        xml_envelope = f'''<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Header>{auth_header}</s:Header><s:Body>{body_xml}</s:Body></s:Envelope>'''
        ns_path = "device" if service_type == "device" else "media"
        self.session.headers.update({
            "SOAPAction": f'"http://www.onvif.org/ver10/{ns_path}/wsdl/{action_name}"',
            "Content-Type": f'application/soap+xml; charset=utf-8; action="http://www.onvif.org/ver10/{ns_path}/wsdl/{action_name}"'
        })

        url = self.media_url if service_type == "media" else self.device_url
        log_xml_pretty(f"{action_name} REQUEST", self.session.headers, xml_envelope)
        try:
            resp = _call(self.should_stop, self.session.post, url, data=xml_envelope, timeout=8)
            log_xml_pretty(f"{action_name} RESPONSE", resp.headers, resp.text)
            if resp.status_code != 200: return None
            return resp.text
        except Exception as e:
            err_str = str(e).lower()
            if "refused" in err_str: msg = "Connection Refused"
            elif "timeout" in err_str: msg = "Connection Timeout (8s)"
            elif "exceeded" in err_str: msg = "Max retries exceeded"
            else: msg = "Network Error"
            lvl = logging.debug if self.is_probing else _fail
            lvl(f"[ONVIF] ❌ {action_name} Failed on {url.split('//')[-1].split('/')[0]} : {msg}")
            return None

    def close(self):
        self.session.close()



ONVIF_PROBE_PORTS = [
    # Top 10 (High Probability)
    8080, 8899, 80, 1000, 8000, 8091, 5000, 8888, 8043, 8081,
    # 11-25 (Vendor Specific)
    10000, 10080, 9000, 9090, 9091, 8100, 8200, 8300, 7001, 7070, 37215, 1101, 1102, 1103, 7654,
]

_RE_PROFILE_TOKEN = re.compile(r'<(?:\w+:)?Profiles\b[^>]*\btoken="(.*?)"')
_RE_URI = re.compile(r'<(?:\w+:)?Uri>\s*(.*?)\s*</(?:\w+:)?Uri>', re.S)

# --- Main Resolvers ---
# Each _resolve_*(info, should_stop) returns (url or None, from_device): only URLs built from what the
# device reported are cached; a fallback (xmeye's default port after a failed query) is not.

def _resolve_onvif(info, should_stop=None):
    if info['channel_given']:
        _fail("[ONVIF] ❌ ONVIF profiles are one flat list, there is no channel: pick a profile with /N "
              "(0 = the first profile, usually the main stream, 1 = the second, ...)")
        return None, False
    if info['port']:
        ports = [info['port']]
    else:
        logging.debug(f"[ONVIF] ONVIF_PROBE_PORTS: {ONVIF_PROBE_PORTS}")
        try:
            ports = _open_ports(info['ip'], ONVIF_PROBE_PORTS, timeout=1.5, should_stop=should_stop)
        except OSError as e:
            _fail(f"[ONVIF] ❌ {info['ip']}: {e.strerror or e}")
            return None, False
        if not ports:
            _fail(f"[ONVIF] ❌ no ONVIF port answers on {info['ip']} (give it: onvif://user:pw@ip:PORT)")
            return None, False
        logging.verbose(f"[ONVIF] open ports: {ports}")

    is_probing = not info['port']

    for p in ports:
        logging.verbose(f"[ONVIF] checking port... {p}")
        client = ONVIFClient(info['ip'], p, info['user'], info['pwd'], is_probing, should_stop)
        try:
            # GetCapabilities -> media service address (namespace prefixes differ between vendors)
            cap_body = '<GetCapabilities xmlns="http://www.onvif.org/ver10/device/wsdl"><Category>Media</Category></GetCapabilities>'
            resp = client.request(cap_body, "GetCapabilities", service_type="device")
            if not resp: continue
            try:
                parsed = xmltodict.parse(resp)
            except Exception as e:
                logging.debug(f"[ONVIF] GetCapabilities XML parse error: {e}")
                continue
            caps = _find(parsed, "GetCapabilitiesResponse")
            media = _find(caps, "Media") if caps is not None else None
            full_xaddr = _text(_find(media, "XAddr")) if media is not None else None
            if not full_xaddr:
                logging.debug("[ONVIF] Media XAddr not found in GetCapabilities")
                continue
            full_xaddr = full_xaddr.strip()
            logging.verbose(f"[ONVIF] 🎯 Found ONVIF Media Path: {full_xaddr}")

            # keep the camera's host:port unless it advertises another (or unreachable) address
            try:
                xp = urlparse(full_xaddr)
                same = xp.scheme.lower() in ("http", "https") and _same_host(xp.hostname, info['ip'])
            except ValueError:
                same = False
            media_url = full_xaddr if same else f"{client.base_url}/{full_xaddr.rstrip('/').split('/')[-1]}"
            client.set_media_url(media_url)

            logging.trace("[ONVIF] time.sleep(1)...")
            _sleep(1, should_stop)

            # Get Profiles
            profile_request = '<GetProfiles xmlns="http://www.onvif.org/ver10/media/wsdl"/>'
            resp = client.request(profile_request, "GetProfiles", service_type="media")
            if not resp: continue
            tokens = list(dict.fromkeys(html.unescape(t) for t in _RE_PROFILE_TOKEN.findall(resp)))
            if not tokens: continue
            logging.verbose(f"[ONVIF] ✅ Tokens found: {tokens}")
            direct = [u for u in (_check_stream_uri(x, info) for x in _RE_URI.findall(resp)
                                  if _xml_text(x).lower().startswith(("rtsp://", "rtsps://"))) if u]
            if direct:
                final_url = _with_credentials(direct[min(info['stream_idx'], len(direct) - 1)], info['user'], info['pwd'])
                logging.info(f"[ONVIF] 🚀 Generated URI: {mask_url(final_url)}")
                return final_url, True

            # Get Stream URI
            token = tokens[min(info['stream_idx'], len(tokens)-1)]
            body = f'<GetStreamUri xmlns="http://www.onvif.org/ver10/media/wsdl"><StreamSetup><Stream xmlns="http://www.onvif.org/ver10/schema">RTP-Unicast</Stream><Transport xmlns="http://www.onvif.org/ver10/schema"><Protocol>RTSP</Protocol></Transport></StreamSetup><ProfileToken>{_xml_escape(token)}</ProfileToken></GetStreamUri>'

            logging.trace("[ONVIF] time.sleep(1)...")
            _sleep(1, should_stop)

            uri_resp = client.request(body, "GetStreamUri", service_type="media")
            if not uri_resp: continue

            m = _RE_URI.search(uri_resp)
            raw_url = _check_stream_uri(m.group(1), info) if m else None
            if raw_url:
                final_url = _with_credentials(raw_url, info['user'], info['pwd'])
                logging.info(f"[ONVIF] 🚀 Generated URI: {mask_url(final_url)}")
                return final_url, True
        finally:
            client.close()

    _fail(f"[ONVIF] ❌ All attempts failed for {info['ip']}")
    return None, False


# --- HTTP helpers shared by the vendor resolvers ---
def _net_error(e, timeout):
    err = str(e).lower()
    if "refused" in err: return "Connection Refused"
    if "timeout" in err or "timed out" in err: return f"Connection Timeout ({timeout}s)"
    if "exceeded" in err: return "Max retries exceeded"
    return "Network Error"

def _digest_get(url, user, pwd, tag, should_stop, timeout=5):
    """GET with HTTP Digest only (Basic would send the password in clear over plain http).  The
    response, or None after logging the network failure."""
    try:
        return _call(should_stop, requests.get, url, auth=requests.auth.HTTPDigestAuth(user, pwd), timeout=timeout)
    except Exception as e:
        _fail(f"{tag} ❌ {_net_error(e, timeout)}: {url.split('/')[2]}")
        logging.debug(f"{tag} 🔍 Full Query: {mask_url(url)}")
        logging.trace(f"{tag} 🛠️ Raw Detail: {mask_url(str(e))}")
        return None

def _denied(resp, tag):
    """True, and the failure logged, when the device did not answer 200."""
    if resp.status_code == 200:
        return False
    what = "authentication failed (HTTP Digest)" if resp.status_code == 401 else "Access denied or failed"
    _fail(f"{tag} ❌ {what}. Status: {resp.status_code}")
    return True

def _no_such_stream(tag, info, have):
    _fail(f"{tag} ❌ stream {info['stream']!r} does not exist: this device has {have}")
    return None, False


def _quiet_dvrip():
    """python-dvr logs every DVRIP packet at DEBUG (login hash and config dumps included; the masking
    filter hides the secrets).  Like the ONVIF SOAP bodies, show them at TRACE only."""
    lg = logging.getLogger("dvrip")
    if not getattr(lg, "_nova_filtered", False):
        lg.addFilter(lambda r: r.levelno > logging.DEBUG or logging.getLogger().getEffectiveLevel() <= 5)
        lg._nova_filtered = True

def _resolve_xmeye(info, should_stop=None):
    """XMeye / XiongMai (DVRIP on 34567): the RTSP port and on/off switch from the NetWork.RTSP config,
    the stream /user=***&password=***&channel=N&stream=S.sdp?real_stream as the device shows it."""
    try: from dvrip import DVRIPCam
    except ImportError:
        logging.error("python-dvr is required for xmeye:// URIs. Run 'pip install python-dvr'")
        return None, False
    _quiet_dvrip()
    if info['stream_idx'] > 1:
        return _no_such_stream("[XMeye]", info, "streams 0 (main) and 1 (sub)")
    ip, user, pwd, mgmt_port = info['ip'], info['user'], info['pwd'], info['port']
    logging.verbose(f"[XMeye] 🔍 Connecting to {ip}:{mgmt_port} to fetch the RTSP port...")
    rtsp_port, from_device = 554, True
    cam = None
    try:
        cam = DVRIPCam(ip, user=user, password=pwd, port=mgmt_port)
        if not _call(should_stop, cam.login):
            _fail(f"[XMeye] ❌ Login failed at {ip}:{mgmt_port}. Check credentials.")
            return None, False
        logging.info("[XMeye] ✅ Login successful. Reading NetWork.RTSP...")
        # SystemInfo: analog + digital (IP) channels; a recorder reports VideoInChannel 0 and DigChannel N
        try:
            si = _call(should_stop, cam.get_system_info)
        except Exception as e:
            logging.debug(f"[XMeye] SystemInfo not readable: {type(e).__name__}")
            si = None
        if isinstance(si, dict) and "Ret" not in si:
            try:
                total = int(si.get("VideoInChannel") or 0) + int(si.get("DigChannel") or 0)
            except (TypeError, ValueError):
                total = 0
            if total and info['channel'] > total:
                _fail(f"[XMeye] ❌ {ip} has {total} channel(s); channel {info['channel']} does not exist")
                return None, False
        try:
            cfg = _call(should_stop, cam.get_info, "NetWork.RTSP")
        except Exception as e:
            logging.debug(f"[XMeye] NetWork.RTSP not readable: {mask_url(str(e))}")
            cfg = None
        server = cfg.get("Server") if isinstance(cfg, dict) else None
        if isinstance(server, dict):
            if cfg.get("IsServer") is False:
                logging.warning(f"[XMeye] ⚠️ the RTSP server is switched off on {ip} (NetService > RTSP in the device's settings)")
            if server.get("Port") not in (None, ""):
                rtsp_port, from_device = _device_port(server.get("Port"), "[XMeye]", ip)
        else:
            logging.debug("[XMeye] no NetWork.RTSP config (older firmware); assuming port 554")
        logging.info(f"[XMeye] 🎯 RTSP port: {rtsp_port}")
    except Exception as e:
        _fail(f"[XMeye] ❌ Device communication error: {mask_url(str(e))}")
        logging.info("[XMeye] ⚠️  Falling back to default port 554 (not cached).")
        rtsp_port, from_device = 554, False
    finally:
        if cam:
            try: cam.close()
            except Exception: pass
    login = "&".join(f"{k}={_qpath(v)}" for k, v in (("user", user), ("password", pwd)))
    path = f"/{login}&channel={info['channel']}&stream={info['stream_idx']}.sdp?real_stream"
    final_url = _rtsp_url(user, pwd, info['host'], rtsp_port, path)
    logging.info(f"[XMeye] 🚀 Generated URI: {mask_url(final_url)}")
    return final_url, from_device


def _resolve_hikvision(info, should_stop=None):
    """Hikvision ISAPI (HTTP Digest): the stream id is channel x 100 + 1 main / 2 sub / 3 third, checked
    against /ISAPI/Streaming/channels; the RTSP port is the RTSP entry of /ISAPI/Security/adminAccesses
    (the channel list does not carry it)."""
    ip, user, pwd, port, ch = info['ip'], info['user'], info['pwd'], info['port'], info['channel']
    if info['stream_idx'] > 2:
        return _no_such_stream("[Hikvision]", info, "streams 0 (main), 1 (sub) and 2 (third)")
    ch_id = str(ch * 100 + info['stream_idx'] + 1)
    base = f"http://{info['host']}:{port}/ISAPI"
    resp = _digest_get(f"{base}/Streaming/channels", user, pwd, "[Hikvision]", should_stop)
    if resp is None or _denied(resp, "[Hikvision]"):
        return None, False
    try:
        channels = _find(xmltodict.parse(resp.content), 'StreamingChannel') or []
    except Exception as e:
        _fail(f"[Hikvision] ❌ unreadable channel list from {ip}: {type(e).__name__}")
        return None, False
    if not isinstance(channels, list): channels = [channels]
    ids = [str(_text(_find(c, 'id')) or "").strip() for c in channels]
    if ids and ch_id not in ids:
        shown = ", ".join(ids[:16]) + (" ..." if len(ids) > 16 else "")
        _fail(f"[Hikvision] ❌ {ip} has no stream {ch_id} (channel {ch}, stream {info['stream_idx']}); it lists {shown}")
        return None, False
    rtsp_port, from_device = 554, True
    resp = _digest_get(f"{base}/Security/adminAccesses", user, pwd, "[Hikvision]", should_stop)
    protocols = None
    if resp is not None and resp.status_code == 200:
        try:
            protocols = _find(xmltodict.parse(resp.content), 'AdminAccessProtocol')
        except Exception as e:
            logging.debug(f"[Hikvision] unreadable adminAccesses: {type(e).__name__}")
    if protocols is None:
        logging.debug(f"[Hikvision] adminAccesses not readable (status {getattr(resp, 'status_code', 'n/a')}); assuming RTSP port 554")
    else:
        if not isinstance(protocols, list): protocols = [protocols]
        for p in protocols:
            if str(_text(_find(p, 'protocol')) or "").strip().upper() == "RTSP":
                if str(_text(_find(p, 'enabled')) or "true").strip().lower() == "false":
                    logging.warning(f"[Hikvision] ⚠️ RTSP is switched off on {ip} (Network > Advanced Settings > Integration Protocol / Port)")
                rtsp_port, from_device = _device_port(_text(_find(p, 'portNo')), "[Hikvision]", ip)
                break
    resolved_url = _rtsp_url(user, pwd, info['host'], rtsp_port, f"/Streaming/Channels/{ch_id}")
    logging.info(f"[Hikvision] 🎯 Resolved RTSP (channel {ch}, stream {info['stream_idx']}): {mask_url(resolved_url)}")
    return resolved_url, from_device


def _resolve_dahua(info, should_stop=None):
    """Dahua CGI (HTTP Digest; Amcrest and other OEM lines are the same): the RTSP port from
    configManager.cgi name=RTSP, the stream /cam/realmonitor?channel=N&subtype=S (0 main, 1 sub, 2 third)."""
    ip, user, pwd, port, ch = info['ip'], info['user'], info['pwd'], info['port'], info['channel']
    if info['stream_idx'] > 2:
        return _no_such_stream("[Dahua]", info, "subtypes 0 (main), 1 (sub) and 2 (third)")
    api_url = f"http://{info['host']}:{port}/cgi-bin/configManager.cgi?action=getConfig&name=RTSP"
    resp = _digest_get(api_url, user, pwd, "[Dahua]", should_stop)
    if resp is None or _denied(resp, "[Dahua]"):
        return None, False
    rtsp_port, from_device = 554, True
    m = re.search(r'(?im)^\s*table\.RTSP\.Port\s*=\s*(.*?)\s*$', resp.text)
    if m:
        rtsp_port, from_device = _device_port(m.group(1), "[Dahua]", ip)
    else:
        logging.debug(f"[Dahua] no table.RTSP.Port in the answer ({resp.text[:60]!r}); assuming 554")
    resolved_url = _rtsp_url(user, pwd, info['host'], rtsp_port, f"/cam/realmonitor?channel={ch}&subtype={info['stream_idx']}")
    logging.info(f"[Dahua] 🎯 Resolved RTSP (channel {ch}, subtype {info['stream_idx']}): {mask_url(resolved_url)}")
    return resolved_url, from_device


def _resolve_reolink(info, should_stop=None):
    """Reolink HTTP API: Login -> GetNetPort (RTSP on/off and port) -> GetRtspUrl (the device's own URL
    for the channel; older firmware: <codec>Preview_<channel>_<main|sub> from GetEnc) -> Logout.  HTTPS
    first (the certificate is self-signed and not verified, but the password is not readable on the
    wire); plain HTTP only when HTTPS is off, with a warning."""
    ip, user, pwd, ch, s = info['ip'], info['user'], info['pwd'], info['channel'], info['stream_idx']
    if s > 1:
        return _no_such_stream("[Reolink]", info, "streams 0 (main) and 1 (sub)")
    stream = ("main", "sub")[s]
    sess = requests.Session()
    token = base = None

    def post(url, cmd, param=None, tok=None):
        q = {"cmd": cmd, **({"token": tok} if tok else {})}
        body = [{"cmd": cmd, "action": 0, "param": param or {}}]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")          # InsecureRequestWarning for the self-signed certificate
            r = _call(should_stop, sess.post, url, params=q, json=body, timeout=8, verify=False)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        data = r.json()
        if not (isinstance(data, list) and data and isinstance(data[0], dict)):
            raise RuntimeError("unexpected answer")
        return data[0]

    def get(cmd, param=None):
        try:
            ans = post(base, cmd, param, token)
        except Exception as e:
            logging.debug(f"[Reolink] {cmd} failed: {_net_error(e, 8)} ({type(e).__name__})")
            return None
        if ans.get("code") != 0:
            logging.debug(f"[Reolink] {cmd}: {ans.get('error')}")
            return None
        return ans.get("value") or {}

    try:
        for scheme, port in (("https", info['port'] or 443), ("http", info['port'] or 80)):
            url = f"{scheme}://{info['host']}:{port}/cgi-bin/api.cgi"
            try:
                ans = post(url, "Login", {"User": {"userName": user, "password": pwd}})
            except Exception as e:
                logging.debug(f"[Reolink] {scheme}://{ip}:{port}: {_net_error(e, 8)} ({type(e).__name__})")
                continue
            if ans.get("code") != 0:
                err = ans.get("error") or {}
                _fail(f"[Reolink] ❌ login refused by {ip}:{port}: {err.get('detail', '?')} (rspCode {err.get('rspCode', '?')})")
                return None, False
            try:
                token = str(ans["value"]["Token"]["name"])
            except (KeyError, TypeError):
                _fail(f"[Reolink] ❌ unexpected login answer from {ip}:{port}")
                return None, False
            base = url
            if scheme == "http":
                logging.warning(f"[Reolink] ⚠️ HTTPS is switched off on {ip}: the password went over plain HTTP")
            logging.info(f"[Reolink] ✅ logged in at {scheme}://{ip}:{port}")
            break
        if token is None:
            _fail(f"[Reolink] ❌ no HTTP API answers on {ip} (port {info['port'] or '443, then 80'}); "
                  f"battery cameras have none, add-on cameras are reached through their NVR")
            return None, False
        rtsp_port, from_device = 554, True
        net = (get("GetNetPort") or {}).get("NetPort")
        if isinstance(net, dict):
            if net.get("rtspEnable", 1) != 1:
                logging.warning(f"[Reolink] ⚠️ RTSP is switched off on {ip}: enable it in the Reolink app "
                                "(Settings > Network > Advanced > Port Settings)")
            rtsp_port, from_device = _device_port(net.get("rtspPort"), "[Reolink]", ip)
        dev = (get("GetDevInfo") or {}).get("DevInfo")
        if isinstance(dev, dict) and isinstance(dev.get("channelNum"), int) and ch > dev["channelNum"]:
            _fail(f"[Reolink] ❌ {dev.get('model') or ip} has {dev['channelNum']} channel(s); channel {ch} does not exist")
            return None, False
        urls = (get("GetRtspUrl", {"channel": ch - 1}) or {}).get("rtspUrl")
        raw = urls.get(f"{stream}Stream") if isinstance(urls, dict) else None
        checked = _check_stream_uri(str(raw), info, "[Reolink]") if raw else None
        if checked:
            final = _with_credentials(checked, user, pwd)
            logging.info(f"[Reolink] 🎯 Resolved RTSP (channel {ch}, {stream}): {mask_url(final)}")
            return final, from_device
        enc = (get("GetEnc", {"channel": ch - 1}) or {}).get("Enc")
        vtype = ((enc.get(f"{stream}Stream") or {}).get("vType") if isinstance(enc, dict) else None) or ""
        codec = "h265" if str(vtype).lower().startswith("h265") else "h264"
        final = _rtsp_url(user, pwd, info['host'], rtsp_port, f"/{codec}Preview_{ch:02d}_{stream}")
        logging.info(f"[Reolink] 🎯 Resolved RTSP (channel {ch}, {stream}, no GetRtspUrl): {mask_url(final)}")
        return final, from_device
    finally:
        if token and base:
            try: post(base, "Logout", None, token)
            except Exception: pass
        sess.close()


def _resolve_axis(info, should_stop=None):
    """Axis VAPIX (HTTP Digest): the RTSP port from param.cgi, the stream /axis-media/media.amp with
    ?camera=N when a channel is written (multi-sensor devices).  Axis has no numbered sub stream:
    stream profiles are chosen in an rtsp:// URL written by hand (...media.amp?streamprofile=NAME)."""
    ip, user, pwd, port, ch = info['ip'], info['user'], info['pwd'], info['port'], info['channel']
    if info['stream_idx'] != 0:
        _fail("[Axis] ❌ Axis cameras have no numbered sub stream; use an rtsp:// URL with a stream profile: "
              "rtsp://user:pw@ip/axis-media/media.amp?streamprofile=NAME")
        return None, False
    api_url = f"http://{info['host']}:{port}/axis-cgi/param.cgi?action=list&group=Network.RTSP.Port"
    resp = _digest_get(api_url, user, pwd, "[Axis]", should_stop)
    if resp is None or _denied(resp, "[Axis]"):
        return None, False
    rtsp_port, from_device = 554, True
    m = re.search(r'(?m)^\s*root\.Network\.RTSP\.Port\s*=\s*(\S+)', resp.text)
    if m:
        rtsp_port, from_device = _device_port(m.group(1), "[Axis]", ip)
    else:
        logging.debug(f"[Axis] no Network.RTSP.Port in the answer ({resp.text[:60]!r}); assuming 554")
    path = "/axis-media/media.amp" + (f"?camera={ch}" if info['channel_given'] else "")
    resolved_url = _rtsp_url(user, pwd, info['host'], rtsp_port, path)
    logging.info(f"[Axis] 🎯 Resolved RTSP (camera {ch}): {mask_url(resolved_url)}")
    return resolved_url, from_device


def _resolve_tapo(info, should_stop=None):
    """TP-Link Tapo: /stream1 (high quality) and /stream2 (standard) on port 554, with the camera
    account created in the Tapo app (not the TP-Link cloud login).  Nothing is asked of the camera."""
    if info['stream_idx'] > 1:
        return _no_such_stream("[Tapo]", info, "/stream1 (0) and /stream2 (1)")
    if info['channel'] != 1:
        _fail("[Tapo] ❌ a Tapo camera has one channel")
        return None, False
    url = _rtsp_url(info['user'], info['pwd'], info['host'], info['port'], f"/stream{info['stream_idx'] + 1}")
    logging.info(f"[Tapo] 🎯 {mask_url(url)}")
    return url, False


def _resolve_unv(info, should_stop=None):
    """Uniview: cameras serve /media/video1|2|3 (main, sub, third), recorders /unicast/cN/sS/live; a
    channel written in the URI (unv://user:pw@nvr/CH/STREAM) selects the recorder form.  Nothing is
    asked of the device."""
    if info['stream_idx'] > 2:
        return _no_such_stream("[Uniview]", info, "streams 0 (main), 1 (sub) and 2 (third)")
    ch, s = info['channel'], info['stream_idx']
    path = f"/unicast/c{ch}/s{s}/live" if info['channel_given'] else f"/media/video{s + 1}"
    url = _rtsp_url(info['user'], info['pwd'], info['host'], info['port'], path)
    logging.info(f"[Uniview] 🎯 {mask_url(url)}")
    return url, False


_RESOLVERS = {"onvif": _resolve_onvif, "xmeye": _resolve_xmeye, "hik": _resolve_hikvision, "dahua": _resolve_dahua,
              "reolink": _resolve_reolink, "axis": _resolve_axis, "tapo": _resolve_tapo, "unv": _resolve_unv}
assert set(_RESOLVERS) == set(SHORTCUT_DEFAULT_PORTS) and set(SCHEME_ALIASES.values()) <= set(_RESOLVERS)


def _public(resolver, default_port):
    def resolve(uri, should_stop=None):
        info = parse_uri(uri, default_port)
        if not info:
            return None
        try:
            return resolver(info, should_stop)[0]
        except ResolveStopped:
            return None
    resolve.__doc__ = f"{resolver.__name__} for a URI string; the resolved URL or None (no caching)."
    return resolve

resolve_onvif = _public(_resolve_onvif, None)
resolve_xmeye = _public(_resolve_xmeye, 34567)
resolve_hikvision = _public(_resolve_hikvision, 80)
resolve_dahua = _public(_resolve_dahua, 80)
resolve_reolink = _public(_resolve_reolink, None)
resolve_axis = _public(_resolve_axis, 80)
