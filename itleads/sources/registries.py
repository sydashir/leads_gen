"""The five tested feeds that carry an industry code: Texas, Connecticut, Seattle, San Francisco, Los Angeles."""
from __future__ import annotations

import re
from datetime import date

from .. import util
from .base import (IT_PREFIXES, Socrata, Source, blank_record, clean_zip, email_kind, iso, looks_like_company,
                   unique)


def _naics_starts(col: str) -> str:
    return " OR ".join(f"starts_with({col},'{p}')" for p in IT_PREFIXES)


def _distinct(a: str, b: str) -> bool:
    """True when b is a genuinely different name from a (not the same words, not contained in it)."""
    fa, fb = util.flat(a), util.flat(b)
    return bool(fb) and fb != fa and fb not in fa


# ----------------------------------------------------------------------- Texas
class Texas(Source):
    """Texas Comptroller: new sales-tax permits. Texas taxes software and data-processing services,
    so IT firms register here, with their NAICS code and outlet address. Loaded weekly (Saturdays)."""
    key = "tx"
    label = "Texas"
    hosts = ("data.texas.gov",)
    lag = 21
    api = Socrata("data.texas.gov", "jrea-zgmq")
    # outlet_naics_code is a number column, so IT is expressed as numeric ranges
    RANGES = [(511200, 511299), (513200, 513299), (516200, 516299), (518200, 518299), (519100, 519299),
              (541500, 541599), (517000, 517999)]
    ORG = {"CL": "LLC / Corp", "CF": "Foreign corp", "CT": "Trust", "CI": "Partnership", "IS": "Sole proprietor",
           "PL": "Partnership"}

    def fetch(self, since: date, until: date) -> list:
        it = " OR ".join(f"outlet_naics_code between {a} and {z}" for a, z in self.RANGES)
        where = (f"outlet_permit_issue_date >= '{since.isoformat()}' AND outlet_permit_issue_date <= "
                 f"'{until.isoformat()}' AND ({it})")
        out = []
        for x in self.api.rows(where, order="outlet_permit_issue_date DESC, taxpayer_number, outlet_number"):
            legal = (x.get("taxpayer_name") or "").strip()
            outlet = (x.get("outlet_name") or "").strip()
            org = x.get("taxpayer_organization_type") or ""
            if org == "IS" and not looks_like_company(outlet):
                continue        # an individual without a business name
            r = blank_record(self.key, f"{x.get('taxpayer_number', '')}-{x.get('outlet_number', '')}")
            r["name"] = util.tidy_name(legal or outlet)
            if outlet and _distinct(legal, outlet):
                r["trade_name"] = util.tidy_name(outlet)
            r["entity_type"] = self.ORG.get(org, org)
            r["individual"] = org == "IS"
            r["state"] = (x.get("outlet_state") or "TX").upper()
            r["city"] = (x.get("outlet_city") or "").title()
            r["address"] = util.tidy_address(x.get("outlet_address") or "")
            r["zip"] = clean_zip(x.get("outlet_zip_code") or "")
            r["registered"] = iso(x.get("outlet_permit_issue_date"))
            r["naics"] = str(x.get("outlet_naics_code") or "")
            r["industry"] = "Sales-tax permit, NAICS " + r["naics"]
            out.append(r)
        return unique(out)


# ----------------------------------------------------------------- Connecticut
class Connecticut(Source):
    key = "ct"
    label = "Connecticut"
    hosts = ("data.ct.gov",)
    lag = 7
    master = Socrata("data.ct.gov", "n7gp-d28j")
    principals = Socrata("data.ct.gov", "ka36-64k6")

    def fetch(self, since: date, until: date) -> list:
        it = " OR ".join(f"naics_code like '%({p}%'" for p in IT_PREFIXES)
        where = (f"date_registration >= '{since.isoformat()}' AND date_registration <= '{until.isoformat()}T23:59:59' "
                 f"AND ({it})")
        rows = self.master.rows(where, order="date_registration DESC, id")
        people = self._people([x["id"] for x in rows if x.get("id")])
        out = []
        for x in rows:
            m = re.search(r"^(.*?)\s*\((\d{6})\)\s*$", x.get("naics_code") or "")
            r = blank_record(self.key, x.get("accountnumber") or x["id"])
            r["name"] = util.tidy_name(x.get("name") or "")
            r["entity_type"] = x.get("business_type") or ""
            r["address"] = util.tidy_address(x.get("billingstreet") or "")
            r["city"] = (x.get("billingcity") or "").title()
            r["state"] = (x.get("billingstate") or "CT").upper()
            r["zip"] = clean_zip(x.get("billingpostalcode") or "")
            r["registered"] = iso(x.get("date_registration"))
            r["naics"] = m.group(2) if m else ""
            r["industry"] = (m.group(1) if m else x.get("naics_code") or "").strip()
            em = (x.get("business_email_address") or "").strip().lower()
            if email_kind(em) in ("own", "free"):
                r["emails"] = [em]
            r["people"] = people.get(x["id"], [])
            out.append(r)
        return unique(out)

    def _people(self, ids: list) -> dict:
        got: dict = {}
        for i in range(0, len(ids), 60):
            chunk = ids[i:i + 60]
            where = "business_id in (" + ",".join(f"'{c}'" for c in chunk) + ")"
            for p in self.principals.rows(where, order="business_id, create_dt, name__c", page=5000):
                nm = " ".join(x for x in ((p.get("firstname") or "").strip(), (p.get("lastname") or "").strip()) if x)
                if not nm:
                    nm = (p.get("name__c") or "").strip()
                if nm:
                    got.setdefault(p["business_id"], []).append(
                        {"name": util.tidy_name(nm), "title": (p.get("designation") or "").strip().title()})
        return got


# --------------------------------------------------------------------- Seattle
class Seattle(Source):
    key = "seattle"
    label = "Seattle"
    hosts = ("data.seattle.gov",)
    lag = 7
    api = Socrata("data.seattle.gov", "wnbq-64tb")

    def fetch(self, since: date, until: date) -> list:
        where = (f"license_start_date between '{since.strftime('%Y%m%d')}' and '{until.strftime('%Y%m%d')}' "
                 f"AND ({_naics_starts('naics_code')})")
        out = []
        for x in self.api.rows(where, order="license_start_date DESC, city_account_number"):
            legal = (x.get("business_legal_name") or "").strip()
            trade = (x.get("trade_name") or "").strip()
            own = (x.get("ownership_type") or "")
            name = legal
            sole = own.lower().startswith("sole")
            if sole:
                if not trade or util.flat(trade) == util.flat(legal):
                    continue
                name = trade
            r = blank_record(self.key, x.get("city_account_number") or x.get("ubi") or util.flat(name))
            r["name"] = util.tidy_name(name)
            if _distinct(name, trade):
                r["trade_name"] = util.tidy_name(trade)
            r["entity_type"] = own
            r["individual"] = sole
            r["address"] = util.tidy_address(x.get("street_address") or "")
            r["city"] = (x.get("city") or "").title()
            r["state"] = (x.get("state") or "WA").upper()
            r["zip"] = clean_zip(x.get("zip") or "")
            s = x.get("license_start_date") or ""
            r["registered"] = iso(f"{s[:4]}-{s[4:6]}-{s[6:8]}") if len(s) >= 8 else ""
            r["naics"] = x.get("naics_code") or ""
            r["industry"] = x.get("naics_description") or ""
            r["phone"] = util.norm_phone(x.get("business_phone") or "")
            r["extra"] = {"ubi": x.get("ubi") or ""}
            out.append(r)
        return unique(out)


# --------------------------------------------------------------- San Francisco
class SanFrancisco(Source):
    key = "sf"
    label = "San Francisco"
    hosts = ("data.sf.gov",)
    lag = 7
    api = Socrata("data.sf.gov", "g8m3-pdis")

    def fetch(self, since: date, until: date) -> list:
        where = (f"location_start_date between '{since.isoformat()}T00:00:00' and '{until.isoformat()}T23:59:59' "
                 f"AND ({_naics_starts('self_reported_naics_code')})")
        out = []
        for x in self.api.rows(where, order="location_start_date DESC, uniqueid"):
            owner = (x.get("ownership_name") or "").strip()
            dba = (x.get("dba_name") or "").strip()
            company = looks_like_company(owner)
            if company:
                name, trade = owner, dba
            elif dba:
                name, trade = dba, ""          # an individual trading under a business name
            else:
                continue
            r = blank_record(self.key, x.get("uniqueid") or x.get("ttxid"))
            r["name"] = util.tidy_name(name)
            if trade and _distinct(name, trade):
                r["trade_name"] = util.tidy_name(trade)
            r["entity_type"] = "Company" if company else "Individual"
            r["individual"] = not company
            r["address"] = util.tidy_address(x.get("full_business_address") or "")
            r["city"] = (x.get("city") or "San Francisco").title()
            r["state"] = (x.get("state") or "CA").upper()
            r["zip"] = clean_zip(x.get("business_zip") or "")
            r["registered"] = iso(x.get("location_start_date"))
            r["naics"] = x.get("self_reported_naics_code") or ""
            r["industry"] = "NAICS " + r["naics"]
            out.append(r)
        return unique(out)


# ----------------------------------------------------------------- Los Angeles
class LosAngeles(Source):
    """The LA list is refreshed monthly (the 15th), so its window must reach back more than a month."""
    key = "la"
    label = "Los Angeles"
    hosts = ("data.lacity.org",)
    lag = 45
    api = Socrata("data.lacity.org", "6rrh-rzua")

    def fetch(self, since: date, until: date) -> list:
        where = (f"location_start_date between '{since.isoformat()}T00:00:00' and '{until.isoformat()}T23:59:59' "
                 f"AND ({_naics_starts('naics')})")
        out = []
        for x in self.api.rows(where, order="location_start_date DESC, location_account"):
            legal = (x.get("business_name") or "").strip()
            dba = (x.get("dba_name") or "").strip()
            company = looks_like_company(legal)
            if not company and not dba:
                continue        # an individual
            r = blank_record(self.key, x.get("location_account") or util.flat(legal))
            r["name"] = util.tidy_name(legal)
            if _distinct(legal, dba):
                r["trade_name"] = util.tidy_name(dba)
            r["entity_type"] = "Company" if company else "Individual"
            r["individual"] = not company
            r["address"] = util.tidy_address(x.get("street_address") or "")
            r["city"] = (x.get("city") or "Los Angeles").title()
            r["zip"] = clean_zip(x.get("zip_code") or "")
            r["state"] = util.state_from_zip(r["zip"]) or "CA"       # the dataset has no state column
            r["registered"] = iso(x.get("location_start_date"))
            r["naics"] = x.get("naics") or ""
            r["industry"] = x.get("primary_naics_description") or ""
            out.append(r)
        return unique(out)


ALL = {c.key: c for c in (Texas, Connecticut, Seattle, SanFrancisco, LosAngeles)}
