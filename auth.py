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

SHORTCUT_DEFAULT_PORTS = {"onvif": None, "xmeye": 34567, "hik": 80, "dahua": 80}


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
        logging.debug("[ONVIF] resolved URI not cached: its password cannot be masked")
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
    """scheme://user:password@host[:port][/stream].  The user info ends at the *last* '@' (a password may
    contain '@' or '/'), user and password are percent-decoded, IPv6 hosts are written [addr].
    'ip' is the bare host, 'host' its URL form."""
    m = re.match(r'^[A-Za-z][A-Za-z0-9+.\-]*://(.*)$', uri or "", re.S)
    if not m: return None
    userinfo, at, hostpart = m.group(1).rpartition('@')
    if not at: return None
    user, colon, pwd = userinfo.partition(':')
    if not colon: return None
    hostport, _, stream = hostpart.partition('/')
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
    stream_val = stream if stream else default_stream
    return {
        'user': unquote(user), 'pwd': unquote(pwd),
        'ip': ip, 'host': _url_host(ip), 'port': port,
        'stream': stream_val, 'stream_idx': 0 if stream_val in ['0', 'm', 'main', ''] else 1,
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
    """onvif:// xmeye:// hik:// dahua:// -> rtsp:// URL with credentials (None on failure).
    use_cache=False asks the device again (the cache is refreshed); should_stop() is polled between
    and during network calls; quiet=True logs failures at DEBUG (the caller reports repeats)."""
    scheme = uri.split("://", 1)[0].lower() if "://" in uri else ""
    if scheme not in SHORTCUT_DEFAULT_PORTS:
        return uri
    info = parse_uri(uri, SHORTCUT_DEFAULT_PORTS[scheme])
    if not info:
        (logging.debug if quiet else logging.error)(
            f"[ONVIF] ❌ cannot parse the {scheme}:// URI; expected {scheme}://user:password@host[:port][/0|1] "
            f"(a ':' in the user name must be written %3A)")
        return None
    key = _cache_key(uri)
    if use_cache:
        entry = load_cache().get(key)
        if entry:
            cached_url = _restore_password(entry["url"], info['pwd'])
            if _points_to_camera(cached_url, info):
                logging.info(f"[ONVIF] ✅ Found cached URI: {mask_url(cached_url)}")
                return cached_url
            logging.warning(f"[ONVIF] ⚠️ ignoring a cached URI that does not point to {info['ip']}")
    resolver = {"onvif": _resolve_onvif, "xmeye": _resolve_xmeye,
                "hik": _resolve_hikvision, "dahua": _resolve_dahua}[scheme]
    _tls.quiet = quiet
    try:
        resolved, from_device = resolver(info, should_stop)
    except ResolveStopped:
        logging.debug(f"[ONVIF] {scheme}:// resolution stopped")
        return None
    finally:
        _tls.quiet = False
    if resolved and not _points_to_camera(resolved, info):
        logging.error(f"[ONVIF] ❌ refusing a resolved URI that does not point to {info['ip']}: {mask_url(resolved)}")
        return None
    if resolved:
        if from_device:
            save_cache(uri, resolved)
        else:
            logging.debug("[ONVIF] resolved URI is a fallback; not cached")
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


def _resolve_xmeye(info, should_stop=None):

    try: from dvrip import DVRIPCam
    except ImportError:
        logging.error("python-dvr is required for xmeye:// URIs. Run 'pip install python-dvr'")
        return None, False

    ip, user, pwd = info['ip'], info['user'], info['pwd']
    mgmt_port = info['port']
    stream_idx = info['stream_idx']

    logging.verbose(f"[XMeye] 🔍 Connecting to {ip}:{mgmt_port} to fetch RTSP port...")

    rtsp_port = 554
    from_device = True
    cam = None
    try:
        cam = DVRIPCam(ip, user=user, password=pwd, port=mgmt_port)
        if not _call(should_stop, cam.login):
            _fail(f"[XMeye] ❌ Login failed at {ip}:{mgmt_port}. Check credentials.")
            return None, False

        logging.info(f"[XMeye] ✅ Login successful. Searching configs...")
        try:
            configs = ["NetWork.NetCommon", "NetWork.NetOrder"]
            for cfg_name in configs:
                cfg_data = _call(should_stop, cam.get_info, cfg_name)
                if isinstance(cfg_data, dict) and cfg_data.get("RtspPort"):
                    rtsp_port, from_device = _device_port(cfg_data.get("RtspPort"), "[XMeye]", ip)
                    break
        except Exception as e:
            logging.debug(f"[XMeye] Optional config fetch failed: {e}")

        logging.info(f"[XMeye] 🎯 Final RTSP Port determined: {rtsp_port}")

    except Exception as e:
        _fail(f"[XMeye] ❌ Device communication error: {e}")
        logging.info(f"[XMeye] ⚠️  Falling back to default port 554 (not cached).")
        rtsp_port, from_device = 554, False
    finally:
        if cam:
            try: cam.close()
            except Exception: pass
    login = "&".join(f"{k}={_qpath(v)}" for k, v in (("user", user), ("password", pwd)))
    final_url = _rtsp_url(user, pwd, info['host'], rtsp_port, f"/{login}&channel=1&stream={stream_idx}.sdp")
    logging.info(f"[XMeye] 🚀 Generated URI: {mask_url(final_url)}")
    return final_url, from_device


def _resolve_hikvision(info, should_stop=None):
    ip, user, pwd, port = info['ip'], info['user'], info['pwd'], info['port']
    stream_idx = info['stream_idx'] # 0: main, 1: sub
    api_url = f"http://{info['host']}:{port}/ISAPI/Streaming/channels"
    try:
        # Digest only: HTTP Basic would send the password in clear text over plain http
        auth = requests.auth.HTTPDigestAuth(user, pwd)
        resp = _call(should_stop, requests.get, api_url, auth=auth, timeout=5)
        if resp.status_code != 200:
            what = "authentication failed (HTTP Digest)" if resp.status_code == 401 else "Access denied or failed"
            _fail(f"[Hikvision] ❌ {what}. Status: {resp.status_code}")
            return None, False
        doc = xmltodict.parse(resp.content)
        root = _find(doc, 'StreamingChannelList') or {}
        channels = _find(root, 'StreamingChannel') or []
        if not isinstance(channels, list): channels = [channels]
        target_ch_id = "101" if stream_idx == 0 else "102"
        rtsp_port, from_device = 554, True
        for ch in channels:
            ch_id = _text(_find(ch, 'id'))
            if ch_id == target_ch_id:
                prot_list = _find(ch, 'ControlProtocol') or []
                if not isinstance(prot_list, list): prot_list = [prot_list]
                for prot in prot_list:
                    if _text(_find(prot, 'streamingTransport')) == 'RTSP':
                        rtsp_port, from_device = _device_port(_text(_find(prot, 'portNo')), "[Hikvision]", ip)
                break
        resolved_url = _rtsp_url(user, pwd, info['host'], rtsp_port, f"/Streaming/Channels/{target_ch_id}")
        logging.info(f"[Hikvision] 🎯 Resolved RTSP (stream:{stream_idx}): {mask_url(resolved_url)}")
        return resolved_url, from_device
    except Exception as e:
        err_str = str(e).lower()
        if "refused" in err_str: msg = "Connection Refused"
        elif "timeout" in err_str: msg = "Connection Timeout (5s)"
        elif "exceeded" in err_str: msg = "Max retries exceeded"
        else: msg = "Network Error"
        host_port = api_url.split('/')[2]
        _fail(f"[Hikvision] ❌ {msg}: {host_port}")
        logging.debug(f"[Hikvision] 🔍 Full Query: {api_url}")
        logging.trace(f"[Hikvision] 🛠️ Raw Detail: {e}")
    return None, False


def _resolve_dahua(info, should_stop=None):
    ip, user, pwd, port = info['ip'], info['user'], info['pwd'], info['port']
    stream_idx = info['stream_idx'] # 0: main, 1: sub
    api_url = f"http://{info['host']}:{port}/cgi-bin/configManager.cgi?action=getConfig&name=Rtsp"
    try:
        auth = requests.auth.HTTPDigestAuth(user, pwd)
        resp = _call(should_stop, requests.get, api_url, auth=auth, timeout=5)
        if resp.status_code != 200:
            _fail(f"[Dahua] ❌ Access failed. Status: {resp.status_code}")
            return None, False
        content = resp.text
        rtsp_port, from_device = 554, True
        for line in content.splitlines():
            if "table.Rtsp.Port=" in line:
                rtsp_port, from_device = _device_port(line.split("=", 1)[-1].strip(), "[Dahua]", ip)
                break
        resolved_url = _rtsp_url(user, pwd, info['host'], rtsp_port, f"/cam/realmonitor?channel=1&subtype={stream_idx}")
        logging.info(f"[Dahua] 🎯 Resolved RTSP (stream:{stream_idx}): {mask_url(resolved_url)}")
        return resolved_url, from_device
    except Exception as e:
        err_str = str(e).lower()
        if "refused" in err_str: msg = "Connection Refused"
        elif "timeout" in err_str: msg = "Connection Timeout (5s)"
        elif "exceeded" in err_str: msg = "Max retries exceeded"
        else: msg = "Network Error"
        host_port = api_url.split('/')[2]
        _fail(f"[Dahua] ❌ {msg}: {host_port}")
        logging.debug(f"[Dahua] 🔍 Full Query: {api_url}")
        logging.trace(f"[Dahua] 🛠️ Raw Detail: {e}")
    return None, False


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
