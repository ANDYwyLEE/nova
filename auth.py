import os
import re
import sys
import time
import uuid
import json
import base64
import socket
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse

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
                cached_time = datetime.fromisoformat(v['timestamp'])
                if now - cached_time < timedelta(days=CACHE_EXPIRE_DAYS):
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

def save_cache(uri, resolved_url):
    cache = load_cache()
    cache[_cache_key(uri)] = {
        "url": mask_url(resolved_url),      # password replaced by ***, restored from the input URI on load
        "timestamp": datetime.now().isoformat()
    }
    save_all_cache(cache)

def _restore_password(masked_url, pwd):
    if pwd is None: return masked_url
    s = masked_url.replace(":***@", f":{pwd}@")
    s = re.sub(r'(?i)\b(password|passwd|pwd|pass)=\*\*\*', lambda m: f"{m.group(1)}={pwd}", s)
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

def log_xml_pretty(action, http_headers, xml_input):
    if logging.getLogger().getEffectiveLevel() > 5: return

    logging.trace(f"[ONVIF] {action}")
    h_info = {k: v for k, v in http_headers.items()}
    logging.trace(f"[ONVIF] HTTP Headers: {json.dumps(h_info)}")
    try:
        full_dict = xmltodict.parse(xml_input)
        env = next(iter(full_dict.values()))
        h_obj, b_obj = {}, {}
        for k, v in env.items():
            if 'Header' in k: h_obj = v
            if 'Body' in k: b_obj = v
        logging.trace("[ONVIF] XML Header (XML -> JSON):\n" + json.dumps(clean_dict(h_obj), indent=4, ensure_ascii=False))
        logging.trace("[ONVIF] XML Body (XML -> JSON):\n" + json.dumps(clean_dict(b_obj), indent=1, ensure_ascii=False))
    except Exception as e:
        logging.trace(f"[ONVIF] XML Parse Error: {e}")


def check_port(ip, port, timeout=1):
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def clean_dict(d):
    if isinstance(d, list):
        return [clean_dict(v) for v in d]
    if isinstance(d, dict):
        return {k: clean_dict(v) for k, v in d.items()
                if not k.startswith('@') and k not in ['xmlns', 'xsi']}
    return d if d is not None else ''


def parse_uri(uri, default_port, default_stream='0'):
    pattern = r"\w+://(?P<user>[^:@/]*):(?P<pwd>[^@]*)@(?P<ip>[^:/]+)(?::(?P<port>\d+))?(?:/(?P<stream>.*))?"
    match = re.match(pattern, uri)
    if not match: return None
    d = match.groupdict()
    d['port'] = int(d['port']) if d['port'] else default_port
    stream_val = d['stream'] if d['stream'] else default_stream
    d['stream_idx'] = 0 if stream_val in ['0', 'm', 'main', ''] else 1
    d['stream'] = stream_val
    return d


def clean_rtsp_554_url(url):
    if not url: return ''
    parsed = urlparse(url)
    if parsed.port == 554:
        if parsed.password is not None:
            new_netloc = f"{parsed.username}:{parsed.password}@{parsed.hostname}"
        elif parsed.username is not None:
            new_netloc = f"{parsed.username}@{parsed.hostname}"
        else:
            new_netloc = parsed.hostname
        parsed = parsed._replace(netloc=new_netloc)
    return urlunparse(parsed)

def _with_credentials(raw_url, user, pwd):
    if "@" in raw_url.split("://", 1)[-1].split("/", 1)[0]:
        return raw_url
    return raw_url.replace("://", f"://{user}:{pwd}@", 1)

#========================================
def resolve_input_uri(uri):
    if not any(uri.startswith(p) for p in ["onvif://", "xmeye://", "hik://", "dahua://"]):
        return uri
    info = parse_uri(uri, None)
    cache = load_cache()
    key = _cache_key(uri)
    if cache and key in cache:
        cached_url = _restore_password(cache[key]["url"], info['pwd'] if info else None)
        logging.info(f"[ONVIF] ✅ Found cached URI: {mask_url(cached_url)}")
        return cached_url
    resolved = None
    if uri.startswith("onvif://"):
        resolved = resolve_onvif(uri)
    elif uri.startswith("xmeye://"):
        resolved = resolve_xmeye(uri)
    elif uri.startswith("hik://"):
        resolved = resolve_hikvision(uri)
    elif uri.startswith("dahua://"):
        resolved = resolve_dahua(uri)
    if resolved:
        save_cache(uri, resolved)
    return resolved

class ONVIFClient:
    def __init__(self, ip, port, user, pwd, is_probing=False):
        self.ip, self.port, self.user, self.pwd = ip, port, user, pwd
        self.is_probing = is_probing
        self.base_url = f"http://{ip}:{port}/onvif"
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
        return f'''<Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"><UsernameToken><Username>{self.user}</Username><Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password><Nonce>{base64.b64encode(nonce).decode()}</Nonce><Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created></UsernameToken></Security>'''

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
            resp = self.session.post(url, data=xml_envelope, timeout=8)
            log_xml_pretty(f"{action_name} RESPONSE", resp.headers, resp.text)
            if resp.status_code != 200: return None
            return resp.text
        except Exception as e:
            err_str = str(e).lower()
            if "refused" in err_str: msg = "Connection Refused"
            elif "timeout" in err_str: msg = "Connection Timeout (8s)"
            elif "exceeded" in err_str: msg = "Max retries exceeded"
            else: msg = "Network Error"
            lvl = logging.debug if self.is_probing else logging.error
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
def resolve_onvif(uri):
    info = parse_uri(uri, None)
    if not info: return None

    if info['port']:
        ports = [info['port']]
    else:
        ports = ONVIF_PROBE_PORTS
        logging.debug(f"[ONVIF] ONVIF_PROBE_PORTS: {ONVIF_PROBE_PORTS}")

    is_probing = len(ports) > 1

    for i, p in enumerate(ports):
        logging.verbose(f"[ONVIF] checking port... {p}")
        if is_probing and not check_port(info['ip'], p, timeout=1.5):
            continue

        client = ONVIFClient(info['ip'], p, info['user'], info['pwd'], is_probing)
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
            logging.verbose(f"[ONVIF] 🎯 Found ONVIF Media Path: {full_xaddr}")

            # keep the camera's host:port unless it advertises an unreachable address
            xp = urlparse(full_xaddr)
            media_url = full_xaddr if xp.hostname == info['ip'] else f"{client.base_url}/{full_xaddr.rstrip('/').split('/')[-1]}"
            client.set_media_url(media_url)

            logging.trace("[ONVIF] time.sleep(1)...")
            time.sleep(1)

            # Get Profiles
            profile_request = '<GetProfiles xmlns="http://www.onvif.org/ver10/media/wsdl"/>'
            resp = client.request(profile_request, "GetProfiles", service_type="media")
            if not resp: continue
            tokens = list(dict.fromkeys(_RE_PROFILE_TOKEN.findall(resp)))
            if not tokens: continue
            logging.verbose(f"[ONVIF] ✅ Tokens found: {tokens}")
            direct = [u for u in _RE_URI.findall(resp) if u.startswith("rtsp://")]
            if direct:
                raw_url = clean_rtsp_554_url(direct[min(info['stream_idx'], len(direct) - 1)])
                final_url = _with_credentials(raw_url, info['user'], info['pwd'])
                logging.info(f"[ONVIF] 🚀 Generated URI: {mask_url(final_url)}")
                return final_url

            # Get Stream URI
            token = tokens[min(info['stream_idx'], len(tokens)-1)]
            body = f'<GetStreamUri xmlns="http://www.onvif.org/ver10/media/wsdl"><StreamSetup><Stream xmlns="http://www.onvif.org/ver10/schema">RTP-Unicast</Stream><Transport xmlns="http://www.onvif.org/ver10/schema"><Protocol>RTSP</Protocol></Transport></StreamSetup><ProfileToken>{token}</ProfileToken></GetStreamUri>'

            logging.trace("[ONVIF] time.sleep(1)...")
            time.sleep(1)

            uri_resp = client.request(body, "GetStreamUri", service_type="media")
            if not uri_resp: continue

            m = _RE_URI.search(uri_resp)
            if m:
                raw_url = clean_rtsp_554_url(m.group(1))
                final_url = _with_credentials(raw_url, info['user'], info['pwd'])
                logging.info(f"[ONVIF] 🚀 Generated URI: {mask_url(final_url)}")
                return final_url
        finally:
            client.close()

    logging.error(f"[ONVIF] ❌ All attempts failed for {info['ip']}")
    return None



def resolve_xmeye(uri):

    try: from dvrip import DVRIPCam
    except ImportError:
        logging.error("python-dvr is required for xmeye:// URIs. Run 'pip install python-dvr'")
        return None

    info = parse_uri(uri, 34567)
    if not info:
        logging.error(f"[XMeye] 💥 Critical Error in xmeye uri: {mask_url(uri)}")
        return None

    ip, user, pwd = info['ip'], info['user'], info['pwd']
    mgmt_port = info['port']
    stream_idx = info['stream_idx']

    logging.verbose(f"[XMeye] 🔍 Connecting to {ip}:{mgmt_port} to fetch RTSP port...")

    rtsp_port = 554
    cam = None
    try:
        cam = DVRIPCam(ip, user=user, password=pwd, port=mgmt_port)
        if not cam.login():
            logging.error(f"[XMeye] ❌ Login failed at {ip}:{mgmt_port}. Check credentials.")
            return None

        logging.info(f"[XMeye] ✅ Login successful. Searching configs...")
        try:
            configs = ["NetWork.NetCommon", "NetWork.NetOrder"]
            for cfg_name in configs:
                cfg_data = cam.get_info(cfg_name)
                if cfg_data and cfg_data.get("RtspPort"):
                    rtsp_port = cfg_data.get("RtspPort")
                    break
        except Exception as e:
            logging.debug(f"[XMeye] Optional config fetch failed: {e}")

        logging.info(f"[XMeye] 🎯 Final RTSP Port determined: {rtsp_port}")

    except Exception as e:
        logging.error(f"[XMeye] ❌ Device communication error: {e}")
        logging.info(f"[XMeye] ⚠️  Falling back to default port 554.")
        rtsp_port = 554
    finally:
        if cam:
            try: cam.close()
            except Exception: pass
    rtsp_format = "" if str(rtsp_port) == "554" else f":{rtsp_port}"
    final_url = f"rtsp://{user}:{pwd}@{ip}{rtsp_format}/user={user}&password={pwd}&channel=1&stream={stream_idx}.sdp"
    logging.info(f"[XMeye] 🚀 Generated URI: {mask_url(final_url)}")
    return final_url


def resolve_hikvision(uri):
    info = parse_uri(uri, 80)
    if not info: return None
    ip, user, pwd, port = info['ip'], info['user'], info['pwd'], info['port']
    stream_idx = info['stream_idx'] # 0: main, 1: sub
    api_url = f"http://{ip}:{port}/ISAPI/Streaming/channels"
    try:
        auth = requests.auth.HTTPDigestAuth(user, pwd)
        resp = requests.get(api_url, auth=auth, timeout=5)
        if resp.status_code == 401:
            resp = requests.get(api_url, auth=(user, pwd), timeout=5)
        if resp.status_code != 200:
            logging.error(f"[Hikvision] ❌ Access denied or failed. Status: {resp.status_code}")
            return None
        doc = xmltodict.parse(resp.content)
        root = _find(doc, 'StreamingChannelList') or {}
        channels = _find(root, 'StreamingChannel') or []
        if not isinstance(channels, list): channels = [channels]
        target_ch_id = "101" if stream_idx == 0 else "102"
        rtsp_port = 554
        found_ch = None
        for ch in channels:
            ch_id = _text(_find(ch, 'id'))
            if ch_id == target_ch_id:
                found_ch = ch
                prot_list = _find(ch, 'ControlProtocol') or []
                if not isinstance(prot_list, list): prot_list = [prot_list]
                for prot in prot_list:
                    if _text(_find(prot, 'streamingTransport')) == 'RTSP':
                        rtsp_port = _text(_find(prot, 'portNo')) or 554
                break
        final_ch_id = target_ch_id
        rtsp_format = "" if str(rtsp_port) == "554" else f":{rtsp_port}"
        resolved_url = f"rtsp://{user}:{pwd}@{ip}{rtsp_format}/Streaming/Channels/{final_ch_id}"
        logging.info(f"[Hikvision] 🎯 Resolved RTSP (stream:{stream_idx}): {mask_url(resolved_url)}")
        return resolved_url
    except Exception as e:
        err_str = str(e).lower()
        if "refused" in err_str: msg = "Connection Refused"
        elif "timeout" in err_str: msg = "Connection Timeout (5s)"
        elif "exceeded" in err_str: msg = "Max retries exceeded"
        else: msg = "Network Error"
        host_port = api_url.split('/')[2]
        logging.error(f"[Hikvision] ❌ {msg}: {host_port}")
        logging.debug(f"[Hikvision] 🔍 Full Query: {api_url}")
        logging.trace(f"[Hikvision] 🛠️ Raw Detail: {e}")
    return None


def resolve_dahua(uri):
    info = parse_uri(uri, 80)
    if not info: return None
    ip, user, pwd, port = info['ip'], info['user'], info['pwd'], info['port']
    stream_idx = info['stream_idx'] # 0: main, 1: sub
    api_url = f"http://{ip}:{port}/cgi-bin/configManager.cgi?action=getConfig&name=Rtsp"
    try:
        auth = requests.auth.HTTPDigestAuth(user, pwd)
        resp = requests.get(api_url, auth=auth, timeout=5)
        if resp.status_code != 200:
            logging.error(f"[Dahua] ❌ Access failed. Status: {resp.status_code}")
            return None
        content = resp.text
        rtsp_port = 554
        for line in content.splitlines():
            if "table.Rtsp.Port=" in line:
                rtsp_port = line.split("=")[-1].strip()
                break
        rtsp_format = "" if str(rtsp_port) == "554" else f":{rtsp_port}"
        resolved_url = f"rtsp://{user}:{pwd}@{ip}{rtsp_format}/cam/realmonitor?channel=1&subtype={stream_idx}"
        logging.info(f"[Dahua] 🎯 Resolved RTSP (stream:{stream_idx}): {mask_url(resolved_url)}")
        return resolved_url
    except Exception as e:
        err_str = str(e).lower()
        if "refused" in err_str: msg = "Connection Refused"
        elif "timeout" in err_str: msg = "Connection Timeout (5s)"
        elif "exceeded" in err_str: msg = "Max retries exceeded"
        else: msg = "Network Error"
        host_port = api_url.split('/')[2]
        logging.error(f"[Dahua] ❌ {msg}: {host_port}")
        logging.debug(f"[Dahua] 🔍 Full Query: {api_url}")
        logging.trace(f"[Dahua] 🛠️ Raw Detail: {e}")
    return None
