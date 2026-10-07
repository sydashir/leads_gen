"""Settings live in config.json next to the code; data and logs sit beside it."""
from __future__ import annotations

import copy
import fcntl
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(os.environ.get("ITLEADS_HOME") or Path(__file__).resolve().parent.parent)
DATA = ROOT / "data"
LOGS = ROOT / "logs"
CONFIG_PATH = ROOT / "config.json"

DEFAULTS = {
    # Google side (filled in on the Settings page)
    "apps_script_url": "",
    "token": "",
    "script_version": 0,           # the version of the Google script last seen (so Settings can say when a newer one exists)
    "sheet_id": "",
    "sheet_url": "",
    "share_link": True,            # "anyone with the link can view": needed for the embedded preview
    "share_with": [],
    # when the daily job runs (local time)
    "schedule": {"hour": 7, "minute": 0},
    # the first run looks back this far; each source then remembers its own last good fetch
    "backfill_days": 45,
    # a company only reaches the sheet when all of these are known
    "require": ["website", "email", "phone"],
    # drop companies whose website shows no sign of IT work at all (the registry code is self-reported)
    "require_it_signal": True,
    # skip companies whose website is 3+ years older than the filing (usually an existing firm with a new permit)
    "skip_established": False,
    # a company with an email OR a phone is enough (most small sites publish an email and a form, few a phone)
    "contact_either": False,
    "sources": {"tx": True, "ct": True, "seattle": True, "sf": True, "la": True},
    "workers": 12,
    # a command to run after every run that refreshed the list (read from this file only), e.g. the Vercel publisher
    "after_run": "",
    # the web app
    "app": {"host": "127.0.0.1", "port": 8765, "secret_key": "", "cooldown_minutes": 10,
            # an internal tool for one team: no sign-up, one shared sign-in. The password is NOT in the code (the code is
            # kept in git): set it with  ./it-leads set-login  (it is stored in config.json, which is never committed) or
            # with ITLEADS_LOGIN_PASSWORD. Without one, nobody can sign in. "show": true, or ITLEADS_SHOW_LOGIN=1, would
            # write the login on the sign-in page. Changing the email retires the old login at the next start.
            "login": {"email": "team@hybrid.agency", "password": "", "show": False},
            "trusted_proxy": "",           # address of a reverse proxy (Caddy, nginx) whose X-Forwarded-For can be believed
            "secure_cookies": False,       # true when the app is served over https (also: ITLEADS_SECURE_COOKIES=1)
            "allowed_hosts": []},          # extra names the app answers to when reached through a proxy or a domain
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class ConfigError(RuntimeError):
    """config.json exists but cannot be read."""


def load() -> dict:
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ConfigError(f"config.json is not valid JSON: {CONFIG_PATH}. Fix it, or delete it to start over "
                              "(then connect Google again in Settings).")
        if not isinstance(data, dict):
            raise ConfigError(f"config.json must hold a JSON object: {CONFIG_PATH}")
        return _merge(DEFAULTS, data)
    return copy.deepcopy(DEFAULTS)


_lock = threading.RLock()


@contextmanager
def locked():
    """One writer at a time, across threads and across processes (the web app and the command line)."""
    ensure_dirs()
    with _lock:
        f = open(DATA / "config.lock", "w")
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
            f.close()


def save(cfg: dict) -> None:
    """Write atomically to a private file; it holds the session secret and the sheet token."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(CONFIG_PATH.parent), prefix=".config-", suffix=".tmp")   # created owner-only
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(cfg, indent=2) + "\n")
        os.replace(tmp, CONFIG_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update(mutate) -> dict:
    """Read, change and write the settings as one step, so two saves at once never lose each other's change."""
    with locked():
        cfg = load()
        mutate(cfg)
        save(cfg)
        return cfg


def tighten() -> None:
    """config.json holds the session secret and the sheet secret: owner only, even if an older version wrote it loosely."""
    try:
        if CONFIG_PATH.exists() and CONFIG_PATH.stat().st_mode & 0o077:
            os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def read_only() -> bool:
    """A published copy that only shows a snapshot of the list: no runs, no settings (ITLEADS_READ_ONLY=1)."""
    return os.environ.get("ITLEADS_READ_ONLY") == "1"


def snapshot_date():
    """The day a read-only copy was taken (ITLEADS_SNAPSHOT_DATE, YYYY-MM-DD), or None. Its dashboard counts days from
    then, not from today, so a frozen list does not drain to zero as the weeks pass."""
    from datetime import date
    try:
        return date.fromisoformat(os.environ.get("ITLEADS_SNAPSHOT_DATE", ""))
    except ValueError:
        return None


def ensure_dirs() -> None:
    for d in (DATA, LOGS):
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)                   # companies, accounts and the session secret live here: owner only
        except OSError:
            pass
