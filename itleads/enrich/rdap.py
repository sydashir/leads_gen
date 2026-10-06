"""Domain creation dates straight from the registries' public RDAP servers (IANA bootstrap)."""
from __future__ import annotations

import json
import threading
import time
from datetime import date

import requests

from .. import config

_lock = threading.Lock()
_boot: dict = {}
_cache: dict = {}
BOOT_FILE = config.DATA / "rdap_bootstrap.json"


def _load_bootstrap() -> dict:
    global _boot
    with _lock:
        if _boot:
            return _boot
        try:
            if BOOT_FILE.exists() and time.time() - BOOT_FILE.stat().st_mtime < 14 * 86400:
                _boot = json.loads(BOOT_FILE.read_text())
                return _boot
        except Exception:
            pass
        try:
            data = requests.get("https://data.iana.org/rdap/dns.json", timeout=20).json()["services"]
            _boot = {tld: urls[0] for tlds, urls in data for tld in tlds}
            config.ensure_dirs()
            BOOT_FILE.write_text(json.dumps(_boot))
        except Exception:
            _boot = {}
        return _boot


def created(domain: str):
    """Registration date of a domain, or None when the registry does not say (some TLDs rate-limit or omit it).
    A real answer (found, or no such record, or no RDAP service for that ending) is remembered; a failure is
    remembered for ten minutes only, so a busy registry does not turn into 'no evidence' for the life of the app."""
    hit = _cache.get(domain)
    if hit is not None and (hit[1] is None or hit[1] > time.time()):
        return hit[0]
    boot = _load_bootstrap()
    if not boot:
        return None                                     # could not learn where to ask: say nothing, remember nothing
    base = boot.get(domain.rsplit(".", 1)[-1])
    result, final = None, True                          # no service for this ending: there will never be an answer
    if base:
        final = False
        for i in range(3):
            try:
                r = requests.get(base.rstrip("/") + "/domain/" + domain, timeout=20,
                                 headers={"Accept": "application/rdap+json", "User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    for e in r.json().get("events", []):
                        if e.get("eventAction") == "registration":
                            result = date.fromisoformat(e["eventDate"][:10])
                    final = True
                    break
                if r.status_code == 429 or r.status_code >= 500:
                    time.sleep(2 * (i + 1))
                    continue
                final = True                            # 404 and the like: the registry has no record
                break
            except Exception:
                time.sleep(1)
    _cache[domain] = (result, None if final else time.time() + 600)
    return result
