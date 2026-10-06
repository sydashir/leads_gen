"""The team sign-in, sessions, CSRF protection and the access decorators."""
from __future__ import annotations

import hmac
import ipaddress
import os
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from functools import wraps
from urllib.parse import urlparse

from flask import abort, g, jsonify, redirect, request, session, url_for
from werkzeug.exceptions import BadRequest
from werkzeug.security import check_password_hash, generate_password_hash

from . import appdb

EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
# Sign-in attempts per 15 minutes: per (network address + email), per email from anywhere, per network address.
# A correct password gives its attempt back. The per-email ceiling is high on purpose: it is a lock anyone could
# trigger by guessing, and ./it-leads reset-password lifts it.
LIMIT_PAIR, LIMIT_ACCOUNT, LIMIT_ADDRESS = 5, 30, 25
WINDOW = 900                                       # seconds: the "15 minutes" of the limits above, and of the sign-in page's message
# PBKDF2 exists in every Python (Apple's has no scrypt, and the data folder can move between interpreters), and
# check_password_hash still reads scrypt hashes where the interpreter supports them.
METHOD = "pbkdf2:sha256:600000"


_slots = threading.BoundedSemaphore(2)             # password hashing is slow on purpose: never more than two at once


class Busy(Exception):
    """Too many password checks at once: the caller gets a polite 'try again' instead of a frozen server."""


class Throttled(Exception):
    """Too many sign-in attempts from this address or for this account."""


BUSY_TITLE = "Sign-in is busy"
BUSY_MESSAGE = "Several people are signing in at once. Try again in a few seconds."
THROTTLED_MESSAGE = (f"Too many sign-in attempts. Wait up to {WINDOW // 60} minutes, then try again. "
                     "If you are still locked out, ask your admin.")
NO_MATCH_MESSAGE = "That email and password do not match. Check both and try again, or ask your admin for the team login."
SESSION_EXPIRED = "Your session expired. Reload the page and try again."


class Refused(BadRequest):
    """A 400 whose error page has a title of its own (the handler in create_app shows `title` and the description)."""

    def __init__(self, title: str, description: str):
        super().__init__(description)
        self.title = title


@contextmanager
def _hashing():
    if not _slots.acquire(timeout=15):
        raise Busy()
    try:
        yield
    finally:
        _slots.release()


def hash_password(pw: str) -> str:
    with _hashing():
        return generate_password_hash(pw, method=METHOD)


# A hash to compare against when the email is unknown, so timing does not reveal who has an account.
_DECOY = hash_password("decoy-" + secrets.token_hex(8))


class AuthError(ValueError):
    pass


def internal_login(cfg: dict) -> dict:
    """The shared sign-in of this internal tool: {"email", "password", "show"}. "show" (write it on the sign-in page) is
    off unless asked for. The settings may be overridden from the environment (ITLEADS_LOGIN_EMAIL,
    ITLEADS_LOGIN_PASSWORD, ITLEADS_SHOW_LOGIN=1 or 0)."""
    c = dict((cfg.get("app") or {}).get("login") or {})
    show = bool(c.get("show", False))
    asked = os.environ.get("ITLEADS_SHOW_LOGIN", "").strip().lower()
    if asked in ("0", "false", "no", "off"):
        show = False
    elif asked in ("1", "true", "yes", "on"):
        show = True
    return {"email": norm_email(os.environ.get("ITLEADS_LOGIN_EMAIL") or c.get("email") or ""),
            "password": os.environ.get("ITLEADS_LOGIN_PASSWORD") or c.get("password") or "",
            "show": show}


def ensure_internal_login(cfg: dict) -> None:
    """Make sure the shared account exists, is an admin and has exactly this password (at every start)."""
    lg = internal_login(cfg)
    if not lg["email"] or not lg["password"]:
        return
    previous = appdb.meta_get("team_login_email")
    if previous and previous != lg["email"]:                     # the login was changed: the old one must stop working
        old = appdb.user_by_email(previous)
        if old:
            appdb.delete_user(old["id"])
    appdb.meta_set("team_login_email", lg["email"])
    user = appdb.user_by_email(lg["email"])
    if user is None:
        appdb.create_user(lg["email"], "Hybrid team", hash_password(lg["password"]), admin=True)
        return
    if not user["is_admin"]:
        appdb.make_admin(user["id"])
    if not _verify(user["pw_hash"], lg["password"]):
        appdb.set_password(user["id"], hash_password(lg["password"]))         # the written password is the real one


def norm_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_password(pw: str) -> None:
    if len(pw or "") < 8:
        raise AuthError("Use a password of at least 8 characters.")
    if len(pw) > 128:
        raise AuthError("That password is too long (128 characters at most).")


def client_key() -> str:
    """The visitor's network address; an IPv6 visitor can pick any address in a /64, so the whole /64 is one visitor."""
    raw = request.remote_addr or "?"
    try:
        ip = ipaddress.ip_address(raw.split("%")[0])
        return str(ipaddress.ip_network(f"{ip}/64", strict=False)) if ip.version == 6 else str(ip)
    except ValueError:
        return raw[:64]


def _keys(email: str) -> tuple:
    ip, e = client_key(), appdb.email_key(email)
    return f"u:{ip}|{e}", f"acct:{e}", f"ip:{ip}"


def _verify(pw_hash: str, password: str) -> bool:
    try:
        with _hashing():
            return check_password_hash(pw_hash, password or "")
    except (AttributeError, ValueError):       # a hash this Python cannot read (made where scrypt exists): reset-password fixes it
        return False


def authenticate(email: str, password: str):
    """Returns the user dict or None; raises Throttled when the limits are reached. The attempt is counted BEFORE the
    password is checked, in one step, so parallel guesses cannot all slip under the limit; a correct password gives
    the attempt back. Always spends the same work, whether or not the email exists."""
    email = norm_email(email)
    k_pair, k_acct, k_ip = _keys(email)
    rows = appdb.reserve([(k_pair, LIMIT_PAIR), (k_acct, LIMIT_ACCOUNT), (k_ip, LIMIT_ADDRESS)], window=WINDOW)
    if rows is None:
        raise Throttled()
    user = appdb.user_by_email(email)
    ok = _verify(user["pw_hash"] if user else _DECOY, password)
    if user and ok:
        appdb.release(rows)
        appdb.clear_failures(k_pair)
        if not user["pw_hash"].startswith(METHOD):             # made by an older version: store it the current way
            appdb.rehash_password(user["id"], hash_password(password))
        return user
    return None


def login_user(user: dict) -> None:
    session.clear()                                  # a fresh session on every sign-in (no fixation)
    session["uid"], session["v"] = user["id"], user["session_v"]
    session["csrf"] = secrets.token_urlsafe(24)
    session.permanent = True
    appdb.touch_login(user["id"])


def logout_user() -> None:
    """Signs THIS browser out: its session cookie is cleared and nothing else changes.

    The team shares one account, so the account's session_v is deliberately NOT bumped here: bumping it would end every
    other person's session too (mid-run, mid-download). It also keeps sign-out working where instances share no disk
    (Vercel), because nothing is stored on the server. The trade-off: a copy of this cookie made before signing out
    keeps working until it expires (30 days from sign-in) or until the password changes. Changing the team password
    (set_password), deleting the account or the "sign out everywhere" bump (appdb.bump_session) still end every
    session at once."""
    session.clear()


def current_user():
    if "user" in g:
        return g.user
    user = None
    uid = session.get("uid")
    if uid:
        u = appdb.user_by_id(uid)
        if u and u["session_v"] == session.get("v"):
            user = u
    g.user = user
    return user


# ------------------------------------------------------------------ CSRF
def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def check_csrf() -> None:
    sent = (request.headers.get("X-CSRF-Token") or request.form.get("_csrf") or "").encode("utf-8", "ignore")
    want = (session.get("csrf") or "").encode()
    if not want or not hmac.compare_digest(sent, want):
        raise Refused("Session expired", SESSION_EXPIRED)


def safe_next(target: str) -> str:
    """Only ever redirect to a path on this site."""
    target = target or ""
    if (not target.startswith("/") or target.startswith("//") or "\\" in target
            or any(ord(c) < 32 or ord(c) == 127 for c in target)):
        return url_for("views.dashboard")
    p = urlparse(target)
    if p.scheme or p.netloc:
        return url_for("views.dashboard")
    return target


# ------------------------------------------------------------ decorators
def _wants_json() -> bool:
    return request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json"


def login_required(fn):
    @wraps(fn)
    def inner(*a, **k):
        if not current_user():
            if _wants_json():
                return jsonify(ok=False, error="Sign in first."), 401
            return redirect(url_for("views.login", next=request.full_path.rstrip("?")))
        return fn(*a, **k)
    return inner


def admin_required(fn):
    @wraps(fn)
    @login_required
    def inner(*a, **k):
        if not current_user()["is_admin"]:
            if _wants_json():
                return jsonify(ok=False, error="Only an admin can do that."), 403
            abort(403)
        return fn(*a, **k)
    return inner
