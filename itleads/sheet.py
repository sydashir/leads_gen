"""Talks to the Apps Script web app that owns the Google Sheet, and turns leads into sheet rows."""
from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from . import util

EXPECTED_VERSION = 6        # the script this tool ships
MIN_VERSION = 5             # the oldest script it still works with (version 6 repairs the Dashboard tab)
SCRIPT_TEMPLATE = Path(__file__).resolve().parent / "appscript" / "Code.gs.tpl"

SOURCE_LABEL = {"tx": "Texas", "ct": "Connecticut", "seattle": "Seattle", "sf": "San Francisco",
                "la": "Los Angeles"}
FIT_LABEL = {"strong": "Confirmed", "weak": "Likely", "unknown": "Unverified"}


class BridgeError(RuntimeError):
    pass


def remember_version(bridge) -> None:
    """Keep the version of the Google script that answered, so Settings can say when a newer one is available."""
    from . import config
    v = int(getattr(bridge, "version", 0) or 0)
    if v and config.load().get("script_version") != v:
        config.update(lambda c: c.__setitem__("script_version", v))


def new_token() -> str:
    return secrets.token_urlsafe(24)


def render_script(token: str) -> str:
    return SCRIPT_TEMPLATE.read_text().replace("__TOKEN__", token)


class Bridge:
    def __init__(self, url: str, token: str, timeout: int = 300, retries: int = 3):
        self.url, self.token, self.timeout, self.retries = url.strip(), token, timeout, max(1, retries)
        self.version = 0                                   # the script version seen in the last answer

    def call(self, action: str, *, expect: tuple = (), **payload) -> dict:
        """expect: keys the answer must carry. Google sometimes answers 200 with something else (seen once, on the very
        first send of 80 rows); a request that adds rows is safe to repeat (rows are matched by id), so ask again."""
        body = json.dumps({"token": self.token, "action": action, **payload})
        last: Exception = BridgeError("no answer from the Google script")
        for i in range(self.retries):
            try:
                r = requests.post(self.url, data=body.encode("utf-8"),
                                  headers={"Content-Type": "text/plain;charset=utf-8", "User-Agent": util.UA},
                                  timeout=self.timeout, allow_redirects=True)
            except requests.RequestException:
                last = BridgeError("Could not reach Google. Check the internet connection and try again.")
                time.sleep(2 * (i + 1))
                continue
            text = r.text.strip()
            if not text.startswith("{"):
                last = BridgeError(
                    f"Google answered with a web page instead of data (HTTP {r.status_code}). In Apps Script open "
                    "Deploy > Manage deployments and make sure 'Who has access' is Anyone (a company Google account "
                    "can block that setting; use a personal one then). It can also be a temporary Google error.")
                time.sleep(3 * (i + 1))
                continue
            try:
                data = r.json()
            except ValueError:                                 # a cut-off or garbled answer from Google
                last = BridgeError("Google sent back an answer that could not be read. It is usually temporary: "
                                   "try again in a minute.")
                time.sleep(3 * (i + 1))
                continue
            if not data.get("ok"):
                err = data.get("error") or "the Google script reported an error"
                if "Wrong token" in err:
                    raise BridgeError("The Google script has a different secret than Hybrid Leads. Open Settings, "
                                      "copy the script again, paste the new script and deploy a new version "
                                      "(Deploy > Manage deployments > pencil > New version). The URL stays the same.")
                raise BridgeError(err)
            if "version" in data:
                self.version = data["version"]
            if "version" in data and not (MIN_VERSION <= data["version"] <= EXPECTED_VERSION):
                raise BridgeError(f"The pasted script is version {data['version']}; Hybrid Leads needs {EXPECTED_VERSION}. "
                                  "Open Settings, copy the script again, paste it into Apps Script, then Deploy > Manage "
                                  "deployments > pencil > Version: New version (not New deployment: that changes the URL).")
            if any(k not in data for k in expect):
                last = BridgeError("Google answered, but not with the result of the request. It is usually temporary: "
                                   "try again in a minute.")
                time.sleep(3 * (i + 1))
                continue
            return data
        raise last

    def ping(self) -> dict:
        return self.call("ping")

    def dashboard(self, sources_active: int = 0, next_run: str = "") -> dict:
        """Rebuild the sheet's Dashboard tab (needs script version 6)."""
        return self.call("dashboard", sources_active=sources_active, next_run=next_run)

    def init(self, tz: str, share: list, refresh: bool = False, link=None) -> dict:
        return self.call("init", tz=tz, share=share, refresh=refresh, link=link)

    def share(self, link: bool) -> dict:
        return self.call("share", link=link)

    def upsert(self, rows: list, today: str) -> dict:
        return self.call("upsert", expect=("inserted", "updated"), rows=rows, today=today)

    def log(self, run: dict, held: int, sources_active: int, next_run: str) -> dict:
        return self.call("log", run=run, held=held, sources_active=sources_active, next_run=next_run)


def _iso(d: str) -> str:
    return d if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d or "") else ""


def _fit(rec: dict, e: dict) -> str:
    """How sure we are it is IT work, and a note when the website is much older than the filing: the filing may be a new
    location or permit of an established company, not a newly formed one."""
    label = FIT_LABEL.get((e.get("it") or {}).get("level", ""), "")
    years = util.site_age_years(e.get("created"), rec.get("registered"))
    if years is None or years < 3:
        return label
    note = f"site since {e['created'][:4]}"
    return f"{label} · {note}" if label else note


def _email_source(e: dict) -> str:
    """Where the email came from. An address from the state filing is used only when its domain is the website's own, and the
    sheet says so; one that is not (it cannot happen through the lookup) says that too, never a bare 'state registry'."""
    src = e.get("email_from", "")
    if src != "state registry":
        return src
    site = (e.get("domain") or urlparse(e.get("website") or "").hostname or "").lower()
    same = util.same_site(e["email"].rpartition("@")[2], site)
    return src + (" (same domain as the website)" if same else " (domain differs from the website)")


def sheet_row(item: dict) -> dict:
    """One qualified lead -> the dict the Apps Script writes into a row."""
    rec, e = item["raw"], item["enrich"]
    name = rec["name"] + (f" ({rec['trade_name']})" if rec.get("trade_name") else "")
    contact = e.get("contact") or {}
    contact_txt = contact.get("name", "")
    if contact_txt and contact.get("title"):
        contact_txt += f", {contact['title']}"
    li = e.get("linkedin") or ""
    li_label = "Profile"
    if not li and e.get("linkedin_company"):
        li, li_label = e["linkedin_company"], "Company"
    # one plain sentence for the hidden "Verified by" column (cell notes would put a black mark on every cell)
    proof = []
    if e.get("why"):
        proof.append("Website: " + "; ".join(e["why"]) + (f" (domain registered {e['created']})" if e.get("created") else ""))
    if e.get("email"):
        proof.append("Email: " + _email_source(e))
    if e.get("phone"):
        proof.append("Phone: " + e.get("phone_from", ""))
    # a person trading under a business name may work from home: city and state only
    street = "" if rec.get("individual") else rec.get("address", "")
    return {
        "id": rec["id"],
        "company": name,
        "website": e.get("website", ""),
        "email": e.get("email", ""),
        "phone": util.fmt_phone(e.get("phone", "")),
        "address": util.compose_address(street, rec.get("city", ""), rec.get("state", ""),
                                        "" if rec.get("individual") else rec.get("zip", "")),
        "registered": _iso(rec.get("registered", "")),
        "contact": contact_txt,
        "contact_email": contact.get("email", ""),
        "linkedin": li,
        "linkedin_label": li_label,
        "industry": rec.get("industry", ""),
        "state": rec.get("state", ""),
        "source": SOURCE_LABEL.get(rec["source"], rec["source"]),
        "fit": _fit(rec, e),
        "proof": ". ".join(proof),
    }
