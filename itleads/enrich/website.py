"""Find the company's website and prove it belongs to them.

We never trust a search engine. Candidate domains come from the company's own email domain (when the
registry has one) and from its name. A page is accepted only when it carries evidence tied to the filing:
the same phone or street address, the full legal name, the email domain filed with the registry, a domain
registered within three weeks of the filing, or a domain registered within a year of it together with a mention
of the filing's own city (a state name alone proves nothing).
"""
from __future__ import annotations

import concurrent.futures as cf
import re
import socket
from datetime import date
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .. import util
from ..sources.base import email_kind
from . import contacts, rdap

TLDS = ["com", "io", "ai", "co", "app", "dev", "net", "org", "us", "tech", "cloud", "llc", "agency"]
EXTRA_PAGES = ("/terms", "/privacy", "/privacy-policy", "/terms-of-service", "/legal", "/contact", "/contact-us", "/about", "/about-us")
PARK = re.compile(r"domain (is )?for sale|buy this domain|this domain (is|may be) for sale|parked (free|domain)|"
                  r"hugedomains|dan\.com|afternic|sedo\.com|domain has expired|account suspended|"
                  r"default web page|under construction|future home of|coming soon", re.I)
DIRS = {"n", "s", "e", "w", "ne", "nw", "se", "sw", "north", "south", "east", "west", "northeast", "northwest",
        "southeast", "southwest"}
_dns_pool = cf.ThreadPoolExecutor(16)
_NXDOMAIN = {socket.EAI_NONAME, getattr(socket, "EAI_NODATA", -999)}


def dns_state(domain: str) -> str:
    """ok | nx (the name does not exist) | temp (the lookup failed; says nothing about the company)"""
    try:
        socket.getaddrinfo(domain, 443, proto=socket.IPPROTO_TCP)
        return "ok"
    except socket.gaierror as e:
        if e.errno in _NXDOMAIN:
            return "nx"
        util.note_net_error()
        return "temp"
    except UnicodeError:                                  # an empty or over-long label: no such name can exist
        return "nx"
    except OSError:
        util.note_net_error()
        return "temp"


def addr_pattern(addr: str):
    t = re.sub(r"[^a-z0-9 ]", " ", (addr or "").lower()).split()
    if not t or not t[0].isdigit():
        return None
    w = [x for x in t[1:] if x not in DIRS and not x.isdigit()][:1]
    if not w:
        return None
    return re.compile(r"\b" + t[0] + r"\b[\s,\.#\-a-z]{0,18}?\b" + re.escape(w[0]))


def candidates(rec: dict) -> list:
    """(domain, hinted) pairs, most likely first."""
    out, seen = [], set()

    def add(d, hinted=False):
        if d and d not in seen:
            seen.add(d)
            out.append((d, hinted))

    for e in rec.get("emails", []):
        if email_kind(e) == "own":
            add(e.split("@")[1], True)
    names = []
    for n in [rec["name"]] + ([rec["trade_name"]] if rec.get("trade_name") else []):
        names += [x.strip() for x in n.split("/") if x.strip()]        # "Jon K Nelman Co/Nelman Tyler": each part is a name
    for n in names:
        for sl in util.slugs(n):
            for t in TLDS:
                add(f"{sl}.{t}")
            if sl.endswith("ai") and len(sl) >= 6 and "-" not in sl:     # Glintly AI: glintly.ai
                add(sl[:-2] + ".ai")
    return out


def check_domain(rec: dict, domain: str, hinted: bool):
    """Fetch the home page and score it against the filing. Returns an evidence dict or None."""
    page = None
    for scheme in ("https://", "http://"):
        page = contacts.load(scheme + domain, timeout=8)
        if page is not None:
            break
    if page is None:
        return None
    soup = BeautifulSoup(page.content, "lxml")
    head = contacts._head_text(soup)
    text = contacts._visible(soup)
    phones = set(contacts.phones_in(soup, text))
    low = text.lower()
    if PARK.search(head + " " + text[:700]) and len(text) < 1500:
        return None
    cores = [c for c in {util.flat("".join(util.core_tokens(rec["name"]))),
                         util.flat("".join(util.core_tokens(rec.get("trade_name", ""))))} if len(c) >= 3]
    fh, fb = util.flat(head), util.flat(text)
    host = urlparse(page.url).netloc.lower().removeprefix("www.")
    label = util.flat(host.split(".")[0])
    in_head = any(c in fh for c in cores)
    in_body = any(c in fb for c in cores)
    in_domain = any(c in label or (len(label) >= 4 and label in c) for c in cores)
    if not (in_head or in_body or in_domain):
        return None

    city = (rec.get("city") or "").lower()
    city_hit = bool(city and re.search(r"\b" + re.escape(city) + r"\b", low))     # the city, never just the state
    specific = []                                   # evidence that ties this page to this filing
    phone = util.norm_phone(rec.get("phone", ""))
    if phone and phone in phones:
        specific.append("phone on the site matches the filing")
    ap = addr_pattern(rec.get("address", ""))
    if ap and ap.search(low) and not rec.get("individual"):
        specific.append("street address on the site matches the filing")
    legal = util.flat(rec["name"])
    # a one-word name ("Strata Inc.") is on many companies' sites: it needs the filing's city beside it
    if len(legal) >= 6 and legal in fb and (len(util.core_tokens(rec["name"])) >= 2 or city_hit):
        specific.append("full legal name on the site")
    if hinted:
        specific.append("domain of the email filed with the registry")

    created = rdap.created(host)
    age_note = ""
    if created and rec.get("registered"):
        try:
            delta = (created - date.fromisoformat(rec["registered"])).days   # > 0: domain came after the filing
        except ValueError:
            delta = None
        if delta is not None and -300 <= delta <= 60:
            age_note = f"domain registered {abs(delta)} days {'before' if delta <= 0 else 'after'} the filing"
            exact = label in cores
            if abs(delta) <= 21 or (exact and -90 <= delta <= 30):   # name-matching domain made around the filing; the
                #                                                      full exact name gets a wider window
                specific.append(age_note)
                age_note = ""

    if not specific:                                 # the home page proves nothing yet: read the pages where a company puts its
        specific += _fine_print(rec, page.url, label in cores)        # legal name and address (terms, privacy, contact)
    why = list(specific)
    if age_note and (specific or city_hit):
        why.append(age_note)
    if not specific:
        if age_note and city_hit:
            why.append("site mentions the same city")
        elif in_head and in_domain and city_hit and label in cores:
            why.append("exact name match and the site mentions the same city")
        else:
            return None
    return {"domain": host, "url": page.url, "why": why, "created": created.isoformat() if created else "",
            "head": head, "city_hit": city_hit, "_page": page}


def _fine_print(rec: dict, home_url: str, exact_domain: bool) -> list:
    """Evidence from the terms, privacy, contact and about pages (the home page of a small site often has none):
    the street address, or the full legal name together with the city or ZIP of the filing."""
    root = f"{urlparse(home_url).scheme}://{urlparse(home_url).netloc}"
    with cf.ThreadPoolExecutor(4) as pool:
        pages = [p for p in pool.map(lambda path: contacts.load(root + path, timeout=6), EXTRA_PAGES[:6]) if p is not None]
    if not pages:
        return []
    low = " ".join(contacts._visible(BeautifulSoup(p.content, "lxml")) for p in pages).lower()
    flat_low = util.flat(low)
    out = []
    ap = addr_pattern(rec.get("address", ""))
    if ap and ap.search(low) and not rec.get("individual"):
        out.append("street address on the site's terms or contact page matches the filing")
    legal = util.flat(rec["name"])
    city = (rec.get("city") or "").lower()
    zip5 = (rec.get("zip") or "")[:5]
    where = bool(city and re.search(r"\b" + re.escape(city) + r"\b", low)) or bool(zip5.isdigit() and zip5 in low)
    if len(legal) >= 6 and legal in flat_low and where and not out:
        out.append("full legal name with the city or ZIP on the site's terms or contact page")
    return out


def discover(rec: dict, max_checks: int = 8):
    cands = candidates(rec)
    states = list(_dns_pool.map(lambda c: dns_state(c[0]), cands))      # resolve all names at once, keep order
    if cands and sum(1 for s in states if s == "temp") * 2 > len(cands):
        raise util.Transient("DNS lookups are failing")
    live = [c for c, s in zip(cands, states) if s == "ok"]
    for d, h in live[:max_checks]:
        ev = check_domain(rec, d, h)
        if ev:
            return ev
    return None
