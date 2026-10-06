"""Shared helpers: HTTP with retries and hard limits, DNS fallback, name / phone / address handling."""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import threading
import time
import warnings

import requests
import urllib3
import urllib3.util.connection as _u3conn
from urllib.parse import urljoin, urlparse

urllib3.disable_warnings()
try:    # Apple's system Python links LibreSSL; the warning is noise for the user
    urllib3.disable_warnings(urllib3.exceptions.NotOpenSSLWarning)
except AttributeError:
    pass
warnings.filterwarnings("ignore", message=".*OpenSSL.*")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class Transient(RuntimeError):
    """The network misbehaved; a result would say nothing about the company. Retry later, use no attempt."""


# ---------------------------------------------------------------- DNS fallback
# Some ISP resolvers fail on a handful of hosts (data.texas.gov on one network we tested).
# For those hosts only, ask a public DNS-over-HTTPS resolver and pin the answer.
_orig_getaddrinfo = socket.getaddrinfo
_dns_pin: dict = {}                 # host -> address from the DoH fallback
_dns_pin_until: dict = {}           # host -> when to ask the normal resolver again (a pin without an entry never expires)
_dns_lock = threading.Lock()
PIN_SECONDS = 15 * 60
PIN_MAX = 500


def _pinned(host: str):
    """The fallback address for a host, if there is one that has not expired."""
    ip = _dns_pin.get(host)
    if ip and _dns_pin_until.get(host, float("inf")) < time.time():
        with _dns_lock:
            _dns_pin.pop(host, None)
            _dns_pin_until.pop(host, None)
        return None
    return ip


def _getaddrinfo(host, *args, **kwargs):
    ip = _pinned(host)
    return _orig_getaddrinfo(ip or host, *args, **kwargs)


socket.getaddrinfo = _getaddrinfo


def ensure_resolvable(host: str) -> bool:
    """True if the host resolves (directly or through the DoH fallback)."""
    if _pinned(host):
        return True
    try:
        _orig_getaddrinfo(host, 443)
        return True
    except OSError:
        pass
    for url in ("https://dns.google/resolve", "https://cloudflare-dns.com/dns-query"):
        try:
            r = requests.get(url, params={"name": host, "type": "A"},
                             headers={"accept": "application/dns-json"}, timeout=10)
            for a in r.json().get("Answer", []):
                if a.get("type") == 1:
                    with _dns_lock:
                        if len(_dns_pin) >= PIN_MAX:                     # company sites that failed once must not pile up
                            for old in list(_dns_pin)[: PIN_MAX // 2]:
                                _dns_pin.pop(old, None)
                                _dns_pin_until.pop(old, None)
                        _dns_pin[host] = a["data"]
                        _dns_pin_until[host] = time.time() + PIN_SECONDS
                    return True
        except Exception:
            continue
    return False


_reach: dict = {"at": 0.0, "ok": False}
_reach_lock = threading.Lock()


def reachable(fresh: bool = False, max_age: float = 30.0) -> bool:
    """Does the internet answer at all? Asks two well-known hosts; one answer is shared by every thread for max_age
    seconds (the lock makes the others wait for it instead of all asking at once)."""
    if not fresh and time.monotonic() - _reach["at"] < max_age:
        return _reach["ok"]
    with _reach_lock:
        if not fresh and time.monotonic() - _reach["at"] < max_age:       # another thread has just asked
            return _reach["ok"]
        ok = False
        for url in ("https://www.gstatic.com/generate_204", "https://data.ct.gov"):
            try:
                get(url, timeout=8, retries=1)
                ok = True
                break
            except Exception:
                continue
        _reach.update(at=time.monotonic(), ok=ok)
        return ok


# --------------------------------------------------------- network-error counter
# Failures that say "the network is broken", not "this company has no website".
_tls = threading.local()


def reset_net_errors() -> None:
    _tls.errors = 0


def note_net_error() -> None:
    _tls.errors = getattr(_tls, "errors", 0) + 1


def net_errors() -> int:
    return getattr(_tls, "errors", 0)


# ------------------------------------------------------------------------ HTTP
def _session() -> requests.Session:
    s = getattr(_tls, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        _tls.s = s
    return s


def _host(url: str) -> str:
    return re.sub(r"^https?://", "", url).split("/")[0].split(":")[0]


def get(url: str, *, params=None, timeout: int = 30, retries: int = 3, verify: bool = True,
        headers=None, allow_redirects: bool = True) -> requests.Response:
    host = _host(url)
    if not _pinned(host):
        ensure_resolvable(host)
    last: Exception = RuntimeError("request failed")
    for i in range(retries):
        try:
            r = _session().get(url, params=params, timeout=timeout, verify=verify,
                               headers=headers, allow_redirects=allow_redirects)
            if r.status_code in (429, 500, 502, 503, 504):
                last = RuntimeError(f"HTTP {r.status_code} from {host}")
                time.sleep(1.5 * (i + 1))
                continue
            return r
        except requests.exceptions.SSLError:
            raise
        except requests.RequestException as e:
            last = e
            time.sleep(1.0 * (i + 1))
    note_net_error()
    raise last


class BlockedAddress(requests.RequestException):
    """The address points inside a network (loopback, private, link-local): never fetched."""


def public_addresses(host: str) -> set:
    """Every address the name points to, if all of them are normal public internet addresses; otherwise an empty set."""
    try:
        ip = _pinned(host)
        found = {ip} if ip else {i[4][0].split("%")[0] for i in _orig_getaddrinfo(host, None)}
    except OSError:
        return set()
    try:
        ok = bool(found) and all(ipaddress.ip_address(a).is_global for a in found)
    except ValueError:
        ok = False
    return found if ok else set()


def is_public_host(host: str) -> bool:
    return bool(public_addresses(host))


_orig_create_connection = _u3conn.create_connection


def _is_global(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr.split("%")[0]).is_global
    except ValueError:
        return False


def _guarded_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, socket_options=None):
    """urllib3's connect step, replaced. While a company page is being fetched (this thread carries a guard) it connects
    only to a public internet address, picked here in the same step that checks it. Whatever the URL looks like to
    any parser (backslashes, unicode names, redirects, rebinding), the machine reached is the one checked."""
    guard = getattr(_tls, "guard", None)
    if guard is None:
        return _orig_create_connection(address, timeout, source_address, socket_options)
    host, port = address
    if host.startswith("["):
        host = host.strip("[]")
    try:
        infos = socket.getaddrinfo(host, port, _u3conn.allowed_gai_family(), socket.SOCK_STREAM)
    except UnicodeError:
        raise socket.gaierror(socket.EAI_NONAME, "not a valid host name")
    err = None
    for family, socktype, proto, _canon, sa in infos:
        if not _is_global(sa[0]):
            guard["blocked"] = True
            err = OSError("refused: that name points inside a network")
            continue
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            for opt in socket_options or ():
                sock.setsockopt(*opt)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            guard["sock"] = sock                         # the watchdog cuts this one at the deadline
            sock.connect(sa)
            guard["blocked"] = False
            return sock
        except OSError as e:
            err = e
            if sock is not None:
                sock.close()
    raise err or OSError("no address to connect to")


_u3conn.create_connection = _guarded_create_connection


def _cut(guard: dict) -> None:
    """The deadline passed: close the connection under whoever is still reading from it."""
    sock = guard.get("sock")
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _guard_session() -> requests.Session:
    """A fresh session per request: no pooled connection can outlive its check, and no proxy setting from the
    environment can send the request somewhere the check cannot see."""
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9", "Connection": "close"})
    return s


def get_bounded(url: str, *, timeout: int = 8, deadline: float = 20.0, max_bytes: int = 2_000_000,
                verify: bool = True, max_hops: int = 5):
    """Fetch a company's web page safely: a wall-clock deadline that really holds (a watchdog cuts the connection),
    a size cap, and no way to be sent to an address inside a network (a company site must not be able to point this
    program at localhost or a router: see _guarded_create_connection). Redirects are followed by hand.
    Returns (response, body_bytes)."""
    end = time.monotonic() + deadline
    cur = url
    for _ in range(max_hops + 1):
        p = urlparse(cur)
        if (p.scheme not in ("http", "https") or not p.hostname or "\\" in cur or p.username or p.password
                or any(ord(ch) <= 32 or ord(ch) == 127 for ch in cur)):
            raise BlockedAddress("unsupported address")
        if not _pinned(p.hostname):
            ensure_resolvable(p.hostname)
        if not public_addresses(p.hostname):               # an early, friendly refusal; the connect step enforces it
            raise BlockedAddress("not a public address")
        guard = {"sock": None, "blocked": False}
        timer = threading.Timer(max(end - time.monotonic(), 0.05), _cut, (guard,))
        timer.daemon = True
        timer.start()
        _tls.guard = guard
        try:
            with _guard_session() as sess:
                try:
                    r = sess.get(cur, timeout=timeout, verify=verify, allow_redirects=False, stream=True)
                except requests.exceptions.SSLError:
                    raise
                except requests.RequestException:
                    if guard["blocked"]:
                        raise BlockedAddress("not a public address")
                    note_net_error()
                    raise
                finally:
                    _tls.guard = None
                if r.is_redirect or r.is_permanent_redirect:
                    nxt = r.headers.get("location")
                    r.close()
                    if not nxt:
                        raise requests.TooManyRedirects("redirect without a target")
                    cur = urljoin(cur, nxt)
                    continue
                body = bytearray()
                try:
                    for chunk in r.iter_content(chunk_size=16384):
                        body.extend(chunk)
                        if len(body) > max_bytes or time.monotonic() > end:
                            break
                except requests.RequestException:
                    note_net_error()
                finally:
                    r.close()
                r.url = cur                                 # the page we really ended on
                return r, bytes(body)
        finally:
            timer.cancel()
            _tls.guard = None
    raise requests.TooManyRedirects("too many redirects")


_BAD_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff\ufffe\uffff]")
EMAIL_STRICT = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63})*\.[A-Za-z]{2,24}$")


def clean_text(s, limit: int = 600) -> str:
    """Text taken from a web page, made safe to store and to put in a spreadsheet: lone surrogates, control characters
    and the two code points XML cannot hold are gone, and it is never longer than `limit`."""
    s = str(s if s is not None else "")
    s = s.encode("utf-8", "ignore").decode("utf-8", "ignore")              # lone surrogates are dropped
    return _BAD_CHARS.sub("", s).strip()[:limit]


def site_age_years(created: str, filed: str):
    """How many years older the website's domain is than the registry filing; None when either date is unknown."""
    from datetime import date
    try:
        return (date.fromisoformat((filed or "")[:10]) - date.fromisoformat((created or "")[:10])).days / 365.25
    except ValueError:
        return None


def valid_email(e: str) -> bool:
    return len(e or "") <= 254 and bool(EMAIL_STRICT.fullmatch(e or ""))


def scrub(obj, limit: int = 600):
    """clean_text over every string inside a result built from page content."""
    if isinstance(obj, str):
        return clean_text(obj, limit)
    if isinstance(obj, list):
        return [scrub(x, limit) for x in obj]
    if isinstance(obj, dict):
        return {clean_text(k, 80): scrub(v, limit) for k, v in obj.items()}
    return obj


# ------------------------------------------------------------------ name utils
LEGAL_SUFFIX = {"llc", "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited",
                "llp", "lp", "pllc", "pc", "plc", "the", "dba", "lc", "pa", "liability"}
# endings of foreign company forms (Veltra Oy, Acme GmbH): dropped from the END of a name only
FOREIGN_SUFFIX = {"oy", "oyj", "ab", "as", "asa", "gmbh", "ag", "bv", "nv", "sa", "sas", "sarl", "srl", "spa", "pty", "pvt",
                  "kk", "ltda", "aps", "kft", "sro"}
GENERIC = {"tech", "technologies", "technology", "software", "systems", "system", "solutions", "solution",
           "consulting", "services", "service", "group", "labs", "lab", "studio", "studios", "digital",
           "global", "international", "holdings", "holding", "enterprises", "partners", "ventures", "it",
           "data", "cloud", "ai", "media", "networks", "network"}
UPPER_TOKENS = {"LLC", "INC", "LLP", "LP", "AI", "IT", "USA", "US", "PLLC", "LC", "II", "III", "IV", "MSP",
                "HVAC", "CPA", "DBA", "PC", "PA", "NW", "NE", "SW", "SE", "N", "S", "E", "W"}


def flat(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def core_tokens(name: str) -> list:
    toks = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower().replace("&", " and ")).split()
    toks = [t for t in toks if t not in LEGAL_SUFFIX]
    if len(toks) > 1 and toks[-1] in FOREIGN_SUFFIX:
        toks = toks[:-1]
    return toks


def slugs(name: str) -> list:
    """Likely domain names for a company, most likely first."""
    t = core_tokens(name)
    out: list = []
    if not t:
        return out

    def add(s: str, minimum: int = 3) -> None:
        if len(s) >= minimum and s not in out:
            out.append(s)

    add("".join(t))
    add("-".join(t))
    nt = [x for x in t if x not in GENERIC]
    if nt and nt != t:
        add("".join(nt), 4)
    joined = "".join(t)
    short = joined.replace("technologies", "tech").replace("technology", "tech")      # Bx Global Technologies: bxglobaltech
    if short != joined:
        add(short, 4)
    if len(t) >= 3:                                                                    # Reef & Tide Money: reefandtide
        add("".join(t[:-1]), 5)
    if len(t) >= 2 and len(t[0]) >= 5 and t[0] not in GENERIC:                         # Wordlark Language Labs: wordlark
        add(t[0], 5)
    return out


def tidy_name(n: str) -> str:
    """ALL-CAPS registry names to readable case; leave mixed-case names alone."""
    n = re.sub(r"\s+", " ", (n or "").strip())
    if not n or not n.isupper():
        return n
    out = []
    for w in n.split(" "):
        bare = re.sub(r"[^A-Za-z]", "", w)
        if bare in UPPER_TOKENS:
            out.append(w)
        elif "&" in w or "." in w and len(bare) <= 2:
            out.append(w)
        else:
            out.append("-".join(p.capitalize() for p in w.split("-")))
    return " ".join(out)


def tidy_address(addr: str) -> str:
    a = re.sub(r"\s+", " ", (addr or "").strip())
    if not a:
        return a
    if a.isupper() or a.islower():
        a = " ".join(w if w.upper() in UPPER_TOKENS else w.capitalize() for w in a.split(" "))
        a = re.sub(r"\b(\d+)(St|Nd|Rd|Th)\b", lambda m: m.group(1) + m.group(2).lower(), a)
    return a


def digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def norm_phone(s: str) -> str:
    d = digits(s)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) == 10 else ""


# Area codes that belong to no place: personal-number services (500, 52x, 533, 544, 566, 577, 588), 600, carrier access (700),
# government (710) and premium-rate lines (900). A company's contact line is never one of them.
NON_GEOGRAPHIC_AREAS = {"500", "521", "522", "523", "524", "525", "526", "527", "528", "529", "533", "544", "566", "577",
                        "588", "600", "622", "700", "710", "900"}


def placeholder_phone(s) -> bool:
    """True when a number cannot be a company's own line: not a 10-digit North American number at all, an area code or
    exchange that cannot start with 0 or 1, a service code (N11) as area code, an area code that belongs to no place, one
    digit repeated, or any 555 exchange. 555-0100 to 0199 are reserved for films and demos, 555-1212 is directory enquiries
    and every other 555 number is the stock number of a mock-up (555-1234, 555-0341, 555-5555 were all found on real
    company pages)."""
    d = norm_phone(s)
    if not d:
        return True
    area, exchange = d[:3], d[3:6]
    if area[0] in "01" or exchange[0] in "01":
        return True
    if exchange == "555" or area[1:] == "11":                 # (an area code N11 is a service code; 211 to 911 reach no company)
        return True
    return len(set(d)) == 1 or area in NON_GEOGRAPHIC_AREAS


def usable_phone(s) -> str:
    """The number as ten digits when it could be a company's own line, else an empty string."""
    return "" if placeholder_phone(s) else norm_phone(s)


def fmt_phone(d: str) -> str:
    d = norm_phone(d)
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if d else ""


# the second-level names under which a whole country's companies register (acme.co.uk): the site's own name is one label deeper
PUBLIC_SUFFIX_2 = {"co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk", "com.au", "net.au", "org.au", "co.nz",
                   "org.nz", "co.in", "net.in", "org.in", "co.za", "org.za", "com.br", "com.mx", "com.ar", "com.co", "co.jp",
                   "or.jp", "ne.jp", "com.sg", "com.hk", "com.cn", "com.tw", "co.kr", "co.il", "com.tr", "com.pk", "com.ph",
                   "com.my", "com.ng", "co.ke", "com.eg", "com.sa", "com.ua", "co.id", "com.vn", "com.bd"}


def site_root(domain: str) -> str:
    """The registered name of a site: 'mail.acme.io' -> 'acme.io', 'www.acme.co.uk' -> 'acme.co.uk'."""
    labels = (domain or "").lower().strip(".").split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in PUBLIC_SUFFIX_2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def same_site(email_domain: str, site_domain: str) -> bool:
    """True when the email's domain is the site's domain or a subdomain of it (never a look-alike)."""
    root = site_root(site_domain)
    ed = (email_domain or "").lower().strip(".")
    return bool(root) and (ed == root or ed.endswith("." + root))


def compose_address(street: str, city: str, state: str, zip_: str) -> str:
    street = tidy_address(street)
    city = (city or "").strip()
    city = city.title() if city.isupper() else city
    tail = " ".join(x for x in ((state or "").strip().upper(), (zip_ or "").strip()[:5]) if x)
    return ", ".join(x for x in (street, city, tail) if x)


# 5-digit ZIP ranges (USPS) used only when a dataset gives no state of its own.
_ZIP_RANGES = [
    ("AL", 35000, 36999), ("AK", 99500, 99999), ("AZ", 85000, 86999), ("AR", 71600, 72999), ("CA", 90000, 96199),
    ("CO", 80000, 81999), ("CT", 6000, 6999), ("DE", 19700, 19999), ("DC", 20000, 20599), ("FL", 32000, 34999),
    ("GA", 30000, 31999), ("GA", 39800, 39999), ("HI", 96700, 96899), ("ID", 83200, 83999), ("IL", 60000, 62999),
    ("IN", 46000, 47999), ("IA", 50000, 52999), ("KS", 66000, 67999), ("KY", 40000, 42799), ("LA", 70000, 71599),
    ("ME", 3900, 4999), ("MD", 20600, 21999), ("MA", 1000, 2799), ("MA", 5500, 5599), ("MI", 48000, 49999),
    ("MN", 55000, 56799), ("MS", 38600, 39799), ("MO", 63000, 65899), ("MT", 59000, 59999), ("NE", 68000, 69399),
    ("NV", 88900, 89999), ("NH", 3000, 3899), ("NJ", 7000, 8999), ("NM", 87000, 88499), ("NY", 10000, 14999),
    ("NC", 27000, 28999), ("ND", 58000, 58899), ("OH", 43000, 45999), ("OK", 73000, 74999), ("OR", 97000, 97999),
    ("PA", 15000, 19699), ("RI", 2800, 2999), ("SC", 29000, 29999), ("SD", 57000, 57799), ("TN", 37000, 38599),
    ("TX", 75000, 79999), ("TX", 88500, 88599), ("UT", 84000, 84999), ("VT", 5000, 5499), ("VT", 5600, 5999),
    ("VA", 20100, 20199), ("VA", 22000, 24699), ("WA", 98000, 99499), ("WV", 24700, 26999), ("WI", 53000, 54999),
    ("WY", 82000, 83199),
]


def state_from_zip(z: str) -> str:
    m = re.match(r"\s*(\d{5})", z or "")
    if not m:
        return ""
    n = int(m.group(1))
    for st, lo, hi in _ZIP_RANGES:
        if lo <= n <= hi:
            return st
    return ""


def tz_name() -> str:
    """The machine's time zone as an IANA name (for the Google sheet): the TZ setting if there is one (servers, containers),
    else the Mac's own, UTC if it cannot be read."""
    tz = os.environ.get("TZ", "").lstrip(":")
    if tz == "UTC" or re.fullmatch(r"[A-Za-z_]+/[A-Za-z_+\-0-9/]+", tz):
        return tz
    try:
        target = os.readlink("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return "UTC"
