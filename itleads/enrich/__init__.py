"""Turn a registry record into a lead: website, email, phone, contact person, IT signal."""
from __future__ import annotations

import copy
import re
from datetime import date
from urllib.parse import urlparse

from .. import util
from ..sources.base import email_kind
from . import contacts, itclass, website

ESTABLISHED_YEARS = 3
# the mailbox to write to first, best first: a company's front door, then the desks that answer customers
GENERIC_LOCAL = ("hello", "info", "contact", "sales", "hi", "team", "office", "inquiries", "mail", "admin", "support")
# mailboxes for something else (law, press, hiring, billing, a robot, a scam-report desk, a registered-agent notice address):
# never the address of the company
NOT_A_CONTACT = re.compile(r"^(privacy|legal|dmca|copyright|abuse|press|media|pr|careers?|jobs?|hr|recruit\w*|billing|accounts?|"
                           r"invoices?|payables?|noreply|no-reply|donotreply|webmaster|postmaster|security|compliance|contracts?|"
                           r"procurement|unsubscribe|bounce|mailer-daemon|investors?|ir|gdpr|ccpa|"
                           r"concerns?|complaints?|fraud|scam|phishing|spam|reports?|whistleblower|ethics|dpo|data[._-]?protection|"
                           r"(?:corporate|legal|service)[._-]?notices?|notices?|registered[._-]?agents?|service[._-]?of[._-]?process|"
                           r"remove[._-]?me|remove|opt[._-]?out|delete[._-]?me|takedown)$", re.I)
# one word of a mailbox name is enough: pam.hr, remove.me, legal.team
NOT_A_CONTACT_WORD = {"hr", "careers", "recruiting", "recruitment", "billing", "invoices", "payables", "unsubscribe", "abuse",
                      "privacy", "legal", "dmca", "gdpr", "dpo", "noreply", "remove", "optout", "relations", "press", "media",
                      "jobs"}


def not_a_contact(local: str) -> bool:
    """True for the first half of an address that belongs to a function (hiring, law, billing, fraud reports, removal
    requests ...) and so never to the company's front door."""
    local = (local or "").strip().lower()
    return bool(NOT_A_CONTACT.match(local)) or any(w in NOT_A_CONTACT_WORD for w in re.split(r"[._+\-]", local))


LEGAL_ONLY = "company website (privacy or terms page only)"      # where a value was found when no other page shows it


def _pick_email(site_emails: list, registry_emails: list, domain: str, city: str = "") -> tuple:
    """The company's own address wins. A third-party address on the page is never the company's.
    A free-mail address counts only if the website itself shows it; a registry free-mail address is a person's
    private mailbox and is ignored unless the site shows the same one. An address the state filing gives is used only when
    its domain is the website's own (same_site), and then it says so ("state registry").
    Among equally good mailboxes of the company the one named after the filing's city goes first (dallas@ for a Dallas filing)."""
    where = util.flat(city)

    def rank(local: str) -> int:
        if local in GENERIC_LOCAL:
            return GENERIC_LOCAL.index(local)
        return len(GENERIC_LOCAL) if where and local == where else len(GENERIC_LOCAL) + 1

    scored = []
    for i, e in enumerate(site_emails):                # earlier in the page (mailto links come first) wins a tie
        dom, local = e.split("@")[1], e.split("@")[0]
        if not_a_contact(local):
            continue
        if contacts.same_site(dom, domain):
            scored.append((0, rank(local), i, e, "company website"))
        elif email_kind(e) == "free":
            scored.append((2, 1, i, e, "company website"))
    for i, e in enumerate(registry_emails):
        kind = email_kind(e)
        if kind == "own" and contacts.same_site(e.split("@")[1], domain) and not not_a_contact(e.split("@")[0]):
            scored.append((1, 1, i, e, "state registry"))
    scored.sort()
    return (scored[0][3], scored[0][4]) if scored else ("", "")


def registry_contact(rec: dict) -> dict:
    """The first person named in the state filing whose name is a person's name (never a company that is an officer, never
    a heading), with the title the filing gives."""
    people = rec.get("people") if isinstance(rec, dict) else None
    for p in people if isinstance(people, list) else []:
        if not isinstance(p, dict):
            continue
        name = contacts.okname(util.clean_text(p.get("name"), 120), registry=True)
        if name:
            return {"name": name, "title": contacts.clean_title(util.clean_text(p.get("title"), 120)), "email": "",
                    "linkedin": "", "from": "state registry"}
    return {}


def _first_leader(people: list, company: str = ""):
    """The first person the website names whose name is a person's and whose title is a plain title (the scrape has
    checked this already; a result handed in from elsewhere is checked again), with both tidied. `company` is the company's
    own name: a heading that is only that name is not a person."""
    for p in people or []:
        if not isinstance(p, dict):
            continue
        name, title = contacts.okname(p.get("name"), company=company), contacts.clean_title(p.get("title"))
        if name and title:
            return dict(p, name=name, title=title)
    return None


def empty_result() -> dict:
    """A lookup that found nothing."""
    return {"checked": date.today().isoformat(), "website": "", "domain": "", "why": [], "created": "",
            "email": "", "email_from": "", "emails": [], "phone": "", "phone_from": "", "phones": [],
            "contact": {}, "linkedin": "", "linkedin_company": "", "it": {"level": "none", "score": 0}}


def enrich_one(rec: dict) -> dict:
    """Raises util.Transient when the network (not the company) is the reason nothing was found."""
    util.reset_net_errors()
    out = empty_result()
    site = website.discover(rec)
    if not site:
        # "no website" is a verdict about the company only if the internet was answering when it was reached: a laptop
        # whose lid closed or whose Wi-Fi dropped mid-run gets "no such name" for every name it tries (measured)
        if not util.reachable(max_age=3):
            raise util.Transient("the internet stopped answering while looking for the website")
        return out
    sc = contacts.scrape(site["url"], site["domain"], rec["name"], home=site.pop("_page", None))
    if not sc["ok"]:
        raise util.Transient("the website could not be read")
    parsed = urlparse(site["url"])
    root = f"{parsed.scheme}://{parsed.netloc}"
    if parsed.scheme not in ("http", "https") or not re.fullmatch(r"https?://[a-z0-9]([a-z0-9.\-]*[a-z0-9])?(:\d{1,5})?", root.lower()):
        return out                                                   # not a plain web address: treat as no website
    out.update(website=root, domain=site["domain"], why=site["why"],
               created=site["created"])
    email, efrom = _pick_email(sc["emails"], rec.get("emails", []), site["domain"], rec.get("city", ""))
    if efrom == "company website" and email in (sc.get("legal_only_emails") or []):
        efrom = LEGAL_ONLY                       # the sheet then says it can only be found in a privacy policy or terms page
    out.update(email=email, email_from=efrom,
               emails=[e for e in sc["emails"] if e != email and contacts.same_site(e.split("@")[1], site["domain"])
                       and not not_a_contact(e.split("@")[0])][:3])
    phones = [p for p in sc["phones"] if util.usable_phone(p)]       # never a 555 stock number or another placeholder
    phones = phones if len(phones) <= 5 else []                       # a long list is a directory, not a contact
    if phones:                                  # only a number the company itself shows: a city licence phone is often the
        legal_only = set(sc.get("legal_only_phones") or [])                 # owner's private line (3 of 3 checked were)
        out.update(phone=phones[0], phone_from=LEGAL_ONLY if phones[0] in legal_only else "company website")
    out["phones"] = phones[:4]
    leader = _first_leader(sc["people"], rec.get("name", ""))
    if leader:
        out["contact"] = {"name": leader["name"], "title": leader["title"], "email": leader.get("email", ""),
                          "linkedin": leader.get("linkedin", ""), "from": "company website"}
    else:
        out["contact"] = registry_contact(rec)
    out["linkedin"] = (out["contact"] or {}).get("linkedin", "")
    out["linkedin_company"] = sc["linkedin_company"]
    out["it"] = itclass.classify(sc["head"], sc["body"])
    return util.scrub(out)                                             # nothing taken from a page is stored as it came


def has_usable_email(e: dict) -> bool:
    """A stored email counts only when it is not a function mailbox (fraud reports, removal requests, legal notices ...)."""
    addr = str((e.get("email") if isinstance(e, dict) else "") or "").strip()
    return bool(addr) and not not_a_contact(addr.split("@")[0])


def has_usable_phone(e: dict) -> bool:
    """A stored phone counts only when it could be a company's own line: not a 555 stock number or another placeholder and
    not a number filed with the city."""
    if not isinstance(e, dict):
        return False
    return bool(util.usable_phone(str(e.get("phone") or ""))) and e.get("phone_from") != "city licence"


def clean_stored(e: dict, rec: dict = None) -> dict:
    """A copy of a lookup result stored earlier with what today's checks refuse taken out: a placeholder phone (the next
    stored number takes its place when there is one), a function mailbox as the email, a contact whose name is not a
    person's or whose title is no title (the first person in the state filing, `rec`, takes the place when given).
    The caller compares it with the original to see whether anything changed. A stored result that is not a dictionary,
    or a field of the wrong kind (a number as the email, a list as the contact), is handled, never raised on."""
    if not isinstance(e, dict):
        return {}
    out = copy.deepcopy(e)
    rec = rec if isinstance(rec, dict) else None
    if "phones" in out:
        out["phones"] = [str(p) for p in (out["phones"] if isinstance(out["phones"], list) else []) if util.usable_phone(str(p))]
    phones = out.get("phones") or []
    if out.get("phone") not in (None, ""):
        out["phone"] = str(out["phone"])
    if out.get("phone") and not util.usable_phone(out["phone"]):
        out["phone"] = phones[0] if phones else ""
        if not out["phone"]:
            out["phone_from"] = ""
    if out.get("email") not in (None, ""):
        out["email"] = str(out["email"])
    if out.get("email") and not has_usable_email(out):
        out["email"], out["email_from"] = "", ""
    c = out.get("contact")
    if c and isinstance(c, dict):
        registry = c.get("from") == "state registry"
        name = contacts.okname(c.get("name"), registry=registry, company=(rec or {}).get("name", ""))
        title = contacts.clean_title(c.get("title"))
        if name and (registry or (title and contacts.LEAD.search(title))):
            c = dict(c, name=name, title=title)
        else:
            c = registry_contact(rec) if rec else {}
        out["contact"] = c
        out["linkedin"] = c.get("linkedin", "")
    elif c is not None and not isinstance(c, dict):              # a list or a text where the contact should be: no contact
        out["contact"] = registry_contact(rec) if (c and rec) else {}
        out["linkedin"] = out["contact"].get("linkedin", "")
    return out


def qualify(rec: dict, e: dict, cfg: dict) -> tuple:
    """-> (state, missing). state: ready | held | dropped
    A phone that cannot be a company's line (a 555 stock number ...) and an email that belongs to a function (fraud reports,
    removal requests ...) count as missing, also in a result stored earlier, so the rules apply to the whole list."""
    if not e or not isinstance(e, dict) or not e.get("website"):
        return "held", ["website"]
    if cfg.get("skip_established"):                    # a website years older than the filing: an existing business, not a new one
        years = util.site_age_years(str(e.get("created") or ""), rec.get("registered"))
        if years is not None and years >= ESTABLISHED_YEARS:
            return "dropped", [f"established company (website since {str(e['created'])[:4]})"]
    it = e.get("it")
    if cfg.get("require_it_signal") and (it.get("level") if isinstance(it, dict) else "none") == "none":
        return "dropped", ["no sign of IT work on the website"]
    missing = []
    has_phone = has_usable_phone(e)
    has_email = has_usable_email(e)
    needs = [n for n in cfg.get("require", [])]
    if cfg.get("contact_either") and "email" in needs and "phone" in needs:
        needs = [n for n in needs if n not in ("email", "phone")]
        if not has_email and not has_phone:
            missing.append("email or phone")
    for need in needs:
        if need == "email" and not has_email:
            missing.append("email")
        elif need == "phone" and not has_phone:
            missing.append("phone")
        elif need == "address" and not rec.get("address"):
            missing.append("address")
    return ("held", missing) if missing else ("ready", [])
