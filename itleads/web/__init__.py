"""The web app: sign in, run, preview the Google Sheet, download, daily schedule."""
from __future__ import annotations

import os
import secrets
import zlib
from datetime import timedelta
from pathlib import Path

from urllib.parse import urlsplit

from flask import Flask, g, jsonify, render_template, request

from .. import config
from . import auth
from .jobs import RunManager, Scheduler


def csp(frames: str = "'none'") -> str:
    """The content policy. Nothing is loaded from anywhere but this site, and nothing may be framed except what `frames`
    names: only the dashboard, and only when the Google Sheet is connected and shared by link, frames Google (the preview)."""
    return ("default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; font-src 'self'; "
            f"connect-src 'self'; frame-src {frames}; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'none'; object-src 'none'")


CSP = csp()
CSP_WITH_SHEET = csp("https://docs.google.com")
# Browser features this app never uses. (interest-cohort is left out on purpose: FLoC no longer exists, so the entry would
# protect nothing, and a browser that does not know the name may complain about it.)
PERMISSIONS_POLICY = ("camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=(), accelerometer=(), "
                      "gyroscope=(), magnetometer=()")
MAX_AGE_VERSIONED = 365 * 86400                 # an address with ?v=<version of the files> never has other content
MAX_AGE_STATIC = 86400                          # logo, fonts, icons: they have no version in their address


_asset_seen: dict = {}                          # {"last": (what the files looked like, the version made from their content)}


def asset_version(paths) -> int:
    """A number that changes when the content of these files changes: the ?v= of the stylesheets and the script.
    It comes from the content, not from the files' dates: a host that unpacks every deployment with the same date on
    every file (Vercel does) would otherwise give a changed stylesheet the old address, and a year-long cache the
    old file. The files are read again only when their size or date changes."""
    seen = []
    for p in paths:
        try:
            st = Path(p).stat()
        except OSError:
            continue
        seen.append((str(p), st.st_mtime_ns, st.st_size))
    key = tuple(seen)
    last = _asset_seen.get("last")
    if not last or last[0] != key:
        crc = 0
        for name, _, _ in seen:
            try:
                crc = zlib.crc32(Path(name).read_bytes(), crc)
            except OSError:
                pass
        last = _asset_seen["last"] = (key, crc)
    return last[1]


def secure_cookies() -> bool:
    """True when the app is served over https: from the environment, or "secure_cookies": true under "app" in config.json."""
    if os.environ.get("ITLEADS_SECURE_COOKIES") == "1":
        return True
    try:
        return bool(config.load()["app"].get("secure_cookies"))
    except config.ConfigError:
        return False


LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}


def extra_hosts(cfg: dict) -> set:
    """Names the app is also meant to answer to: "allowed_hosts" in config.json and ITLEADS_ALLOWED_HOSTS (comma separated)."""
    names = list(cfg["app"].get("allowed_hosts") or []) + os.environ.get("ITLEADS_ALLOWED_HOSTS", "").split(",")
    return {str(n).strip().lower() for n in names if str(n).strip()}


def create_app(*, runner=None, start_scheduler: bool = False, enforce_hosts: bool = True, seed_login: bool = False) -> Flask:
    """enforce_hosts: answer only requests addressed to this machine by a loopback name (or a name in
    "allowed_hosts" in config.json). That stops a web page on the internet from reaching the app through your browser
    by rebinding its own name to 127.0.0.1. Turn it off only when the app is meant to be reached by other names."""
    config.tighten()
    cfg = config.load()
    env_secret = os.environ.get("ITLEADS_SECRET_KEY", "")              # a host with no lasting disk (Vercel) keeps it here
    if not env_secret and not cfg["app"].get("secret_key"):
        cfg = config.update(lambda c: c["app"].__setitem__("secret_key", c["app"].get("secret_key") or secrets.token_hex(32)))
    https = secure_cookies()
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.json.sort_keys = False
    app.config.update(
        SECRET_KEY=env_secret or cfg["app"]["secret_key"],
        SESSION_COOKIE_NAME="itleads",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=https,
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        SESSION_REFRESH_EACH_REQUEST=False,                            # one fixed 30 days from sign-in, not a new cookie on every poll
        MAX_CONTENT_LENGTH=64 * 1024,
    )
    if os.environ.get("ITLEADS_PROXY_FIX") == "1":                     # behind a host that sets X-Forwarded-For itself
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    if seed_login:
        auth.ensure_internal_login(cfg)
    app.manager = RunManager(runner)
    from .views import bp
    app.register_blueprint(bp)

    watched = [Path(app.static_folder) / n for n in ("app.css", "landing.css", "app.js")]
    versioned_paths = {"/static/" + p.name for p in watched}          # the files whose content makes up ?v=; the rest has no version

    app.asset_version = lambda: asset_version(watched)

    @app.context_processor
    def inject():
        return {"csrf_token": auth.csrf_token(), "me": auth.current_user(), "asset_v": app.asset_version(),    # any edit changes the address
                "read_only": config.read_only(), "snapshot_at": os.environ.get("ITLEADS_SNAPSHOT_AT", "")}

    allowed = LOOPBACK_NAMES | extra_hosts(cfg)

    def own_address() -> str:
        """The loopback address of THIS server, with the port the request came in on (not a port written in the code)."""
        try:
            port = urlsplit("//" + request.host).port
        except ValueError:
            port = None
        if not port:
            try:
                port = int(request.environ.get("SERVER_PORT") or 0)
            except ValueError:
                port = 0
        return "http://127.0.0.1" + (f":{port}" if port not in (0, 80) else "")

    @app.before_request
    def protect():
        if enforce_hosts:
            try:
                name = (urlsplit("//" + request.host).hostname or "").lower()
            except ValueError:
                name = ""
            if name not in allowed:
                raise auth.Refused("Wrong address", f"This address does not reach Hybrid Leads. Use {own_address()}.")
        if config.read_only():                                         # a published copy: look, preview, download, nothing else
            if request.path == "/settings" or (request.method in ("POST", "PUT", "PATCH", "DELETE")
                                               and request.path not in ("/login", "/logout")):
                return _error(403, "Read-only copy", "This copy of Hybrid Leads only shows the list. "
                                                      "Runs and settings are on the main installation.")
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            auth.check_csrf()

    @app.after_request
    def headers(resp):
        resp.headers["Content-Security-Policy"] = CSP_WITH_SHEET if g.get("sheet_frame") else CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Permissions-Policy"] = PERMISSIONS_POLICY
        resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        if https:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.path.startswith("/static/") or request.path == "/favicon.ico":
            if resp.status_code in (200, 304):                           # logo, fonts, icons, styles, script: not private
                versioned = request.path in versioned_paths and request.args.get("v") == str(app.asset_version())
                resp.headers["Cache-Control"] = ("public, max-age=%d, immutable" % MAX_AGE_VERSIONED if versioned
                                                 else "public, max-age=%d" % MAX_AGE_STATIC)
            else:
                resp.headers.setdefault("Cache-Control", "no-store")     # a missing file must not be remembered
        else:
            resp.headers.setdefault("Cache-Control", "no-store")         # pages show people and company data
        return resp

    def _error(code, title, message):
        if request.path.startswith("/api/"):
            return jsonify(ok=False, error=message), code
        return render_template("error.html", code=code, title=title, message=message), code

    @app.errorhandler(400)
    def e400(e):
        return _error(400, getattr(e, "title", "That did not work"), getattr(e, "description", "Bad request."))

    @app.errorhandler(403)
    def e403(e):
        return _error(403, "Admins only", "Only admins can open this page. Ask an admin for access.")

    @app.errorhandler(404)
    def e404(e):
        return _error(404, "Page not found", "There is nothing at this address.")

    @app.errorhandler(405)
    def e405(e):
        return _error(405, "Not available", "That address does not accept this kind of request.")

    @app.errorhandler(413)
    def e413(e):
        return _error(413, "Too large", "That request was too large.")

    @app.errorhandler(config.ConfigError)
    def econfig(e):
        app.logger.error("%s", e)                                   # the path is for the log, not for visitors
        return _error(500, "The settings file is damaged",
                      "config.json in the app folder cannot be read. Fix it, or delete it to start over (Google then "
                      "needs connecting again).")

    @app.errorhandler(auth.Busy)
    def ebusy(e):
        return _error(503, auth.BUSY_TITLE, auth.BUSY_MESSAGE)

    @app.errorhandler(500)
    def e500(e):
        return _error(500, "Something went wrong",
                      "The error was recorded. Try again in a moment. If it keeps happening, tell your admin.")

    if start_scheduler:
        app.scheduler = Scheduler(app.manager)
        app.scheduler.start()
    return app
