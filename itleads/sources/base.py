"""Source plumbing. Every source returns the same normalized record shape."""
from __future__ import annotations

import re
import time
from datetime import date, timedelta

from .. import util

# 2017 and 2022 NAICS prefixes for software, data processing / hosting, web portals, streaming and
# social networks, computer services and telecom resellers.
IT_PREFIXES = ("5112", "5132", "5162", "5182", "5191", "5192", "5415", "517")

# Registered-agent and incorporation-service domains: their addresses reach a filing service, not the company.
AGENT_DOMAINS = {
    "registeredagentsinc.com", "northwestregisteredagent.com", "incauthority.com", "zenbusiness.com",
    "legalzoom.com", "harborcompliance.com", "bizfilings.com", "corpnet.com", "cscglobal.com", "cscinfo.com",
    "vcorpservices.com", "ctcorporation.com", "swyftfilings.com", "tailorbrands.com", "mycorporation.com",
    "bizee.com", "incfile.com", "legalinc.com", "cogencyglobal.com", "paracorp.com", "wolterskluwer.com",
    "rocketlawyer.com", "registeredagent.com", "nationalregisteredagents.com", "incorporate.com", "doola.com",
    "corporationservicecompany.com", "northwestregisteredagent.net", "registered-agent-solutions.com",
}
FREE_MAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "aol.com", "proton.me",
             "protonmail.com", "pm.me", "live.com", "msn.com", "me.com", "comcast.net", "sbcglobal.net",
             "att.net", "mail.com", "gmx.com", "yandex.com", "qq.com", "163.com", "126.com", "ymail.com",
             "googlegroups.com"}


def email_kind(email: str) -> str:
    """own | free | agent | none"""
    e = (email or "").strip().lower()
    if "@" not in e:
        return "none"
    dom = e.split("@")[1]
    if dom in FREE_MAIL:
        return "free"
    if "registeredagent" in dom or "registered-agent" in dom or any(
            dom == d or dom.endswith("." + d) for d in AGENT_DOMAINS):
        return "agent"
    return "own"


def blank_record(source: str, rid: str) -> dict:
    return {"id": f"{source}:{rid}", "source": source, "name": "", "trade_name": "", "entity_type": "",
            "individual": False,           # a person trading under a name: no street address, no personal contacts
            "state": "", "city": "", "address": "", "zip": "", "registered": "", "naics": "", "industry": "",
            "phone": "", "emails": [], "people": [], "coded": True, "extra": {}}


class Source:
    key = ""
    label = ""
    hosts: tuple = ()          # for `doctor`
    lag = 7                    # days a filing can appear in the feed after its own date (batch publishing)

    def fetch(self, since: date, until: date) -> list:
        raise NotImplementedError

    def check(self) -> str:
        """Run the real fetch over the last week; proves reach, parsing and filters in one go."""
        n = len(self.fetch(date.today() - timedelta(days=7), date.today()))
        return f"{n} new in the last 7 days"


class Socrata:
    """Minimal SODA client: stable paging, retries, gentle pacing."""

    def __init__(self, domain: str, dataset: str):
        self.domain, self.dataset = domain, dataset

    @property
    def url(self) -> str:
        return f"https://{self.domain}/resource/{self.dataset}.json"

    def rows(self, where: str, order: str, select: str = "", page: int = 1000, cap: int = 60000) -> list:
        """`order` must end in a unique column, or paging with $offset repeats and skips rows."""
        out: list = []
        offset = 0
        while offset < cap:
            params = {"$where": where, "$limit": page, "$offset": offset, "$order": order}
            if select:
                params["$select"] = select
            r = util.get(self.url, params=params, timeout=90)
            if r.status_code != 200:
                raise RuntimeError(f"{self.domain} {self.dataset}: HTTP {r.status_code} {r.text[:160]}")
            chunk = r.json()
            out.extend(chunk)
            if len(chunk) < page:
                break
            offset += page
            time.sleep(0.25)
        return out

    def count(self, where: str) -> int:
        r = util.get(self.url, params={"$select": "count(*)", "$where": where}, timeout=60)
        r.raise_for_status()
        return int(r.json()[0]["count"])


def iso(d) -> str:
    """First ten characters when they form a date, otherwise empty (never ship garbage to the sheet)."""
    s = (d or "")[:10]
    return s if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else ""


def clean_zip(z: str) -> str:
    m = re.match(r"\s*(\d{5})", z or "")
    return m.group(1) if m else ""


COMPANY_SUFFIX = re.compile(r"\b(llc|l\.l\.c|inc|incorporated|corp|corporation|co|company|ltd|limited|llp|lp|pllc|pc|"
                            r"plc|holdings|group|partners|associates|labs|studio|studios|technologies|systems)\b\.?", re.I)


def looks_like_company(name: str) -> bool:
    return bool(COMPANY_SUFFIX.search(name or ""))


def unique(records: list) -> list:
    seen, out = set(), []
    for r in records:
        if r["id"] not in seen:
            seen.add(r["id"])
            out.append(r)
    return out
