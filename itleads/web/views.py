"""Pages and API endpoints."""
from __future__ import annotations

import os
import re
from datetime import date, datetime

from flask import (Blueprint, Response, abort, current_app, g, jsonify, redirect, render_template, request,
                   url_for)

from .. import config, pipeline, sheet, sources, util
from ..store import Store
from . import auth, export
from .jobs import next_run_text

bp = Blueprint("views", __name__)

SCRIPT_URL = re.compile(r"^https://script\.google\.com/(a/macros/[^/]+/)?macros/s/[A-Za-z0-9_\-]+/exec$")
SHEET_ID = re.compile(r"^[A-Za-z0-9_\-]{6,120}$")
SOURCE_LABELS = {"tx": "Texas", "ct": "Connecticut", "seattle": "Seattle", "sf": "San Francisco", "la": "Los Angeles"}
STATUS_LABELS = {"ok": "Done", "partial": "Partly done", "failed": "Failed", "offline": "No internet"}
TRIGGER_LABELS = {"schedule": "Daily schedule", "manual": "Started by hand", "connect": "After connecting Google"}
PREVIEW_ROWS = 500
MAX_NEXT = 512                                           # the sign-in page repeats this back into a hidden field: keep it short


# ------------------------------------------------------------------ helpers
def store() -> Store:
    if "store" not in g:
        g.store = Store(config.DATA / "leads.db")
    return g.store


@bp.teardown_app_request
def _close(_exc):
    s = g.pop("store", None)
    if s is not None:
        s.close()


def _dt(iso: str) -> str:
    try:
        d = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    return f"{d.day} {d:%b %Y}, {d:%H:%M}"


def _day(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    return f"{d.day} {d:%b %Y}"


def google_info(cfg: dict) -> dict:
    """What the pages may show about the sheet. Addresses are built from the sheet id, never taken on trust."""
    sid = str(cfg.get("sheet_id") or "")
    connected = bool(cfg.get("apps_script_url")) and bool(SHEET_ID.match(sid))
    base = f"https://docs.google.com/spreadsheets/d/{sid}"
    return {"connected": connected, "linked": bool(cfg.get("share_link", True)),
            "sheet_url": base + "/edit" if connected else "", "embed_url": base + "/htmlview" if connected else ""}


def _run_lines(s: dict) -> tuple:
    """What a history row says. A run that never got going has no counts worth printing."""
    status = s.get("status", "")
    if status == "offline":
        return "Nothing changed", "no internet connection"
    if status == "failed":
        return "Nothing added", "every registry failed"
    return (f"{s.get('added', s.get('pushed', 0))} added",
            f"{s.get('pushed', 0)} sent to the sheet \u00b7 {s.get('held', 0)} held back")


def run_header() -> tuple:
    """('Updated ...' time, problem note). 'Updated' is the newest run that really refreshed the list; if the very
    latest attempt failed or found no internet, that is said separately instead of passing for an update."""
    updated, problem = "", ""
    for i, r in enumerate(store().recent_runs(10)):
        status = r["summary"].get("status", "")
        if i == 0 and status in ("failed", "offline"):
            problem = "Last run: " + STATUS_LABELS[status].lower()
        if status in ("ok", "partial"):
            updated = _dt(r.get("finished") or r["started"])      # when it finished, not when it began
            break
    return updated, problem


def _next_run(cfg: dict) -> str:
    """A read-only copy never runs, so it never says when the next run is."""
    return "" if config.read_only() else next_run_text(cfg)


def dashboard_context() -> dict:
    cfg = config.load()
    st = store().dashboard((config.read_only() and config.snapshot_date()) or date.today())
    runs = []
    for r in store().recent_runs(6):
        s = r["summary"]
        status = s.get("status", "")
        line1, line2 = _run_lines(s)
        runs.append({"when": _dt(r["started"]), "line1": line1, "line2": line2, "status": status,
                     "status_label": STATUS_LABELS.get(status, status),
                     "trigger": TRIGGER_LABELS.get(s.get("trigger", ""), ""),
                     "sources": ", ".join(f"{SOURCE_LABELS.get(k, k)} {v}" for k, v in (s.get("sources") or {}).items()),
                     "errors": s.get("errors") or []})
    updated, problem = run_header()
    top = max([d["n"] for d in st["by_day"]] + [1])
    return {"stats": st, "runs": runs, "updated": updated, "problem": problem, "top": top,
            "running": current_app.manager.snapshot()["running"], "next_run": _next_run(cfg),
            "google": google_info(cfg), "has_data": st["total"] > 0, "source_labels": SOURCE_LABELS}


@bp.app_template_filter("day")
def day_filter(iso):
    return _day(iso)


@bp.app_template_filter("short_day")
def short_day_filter(iso):
    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    return f"{d.day}"


@bp.app_template_filter("month")
def month_filter(iso):
    try:
        return date.fromisoformat(iso).strftime("%b")
    except (TypeError, ValueError):
        return ""


# --------------------------------------------------------------------- auth
def _login_page(error: str = "", email: str = "", nxt: str = "", code: int = 200):
    lg = auth.internal_login(config.load())
    hint = {"email": lg["email"], "password": lg["password"]} if lg["show"] and lg["email"] and lg["password"] else None
    return render_template("login.html", error=error, email=email, next=nxt, hint=hint), code


@bp.get("/login")
def login():
    if auth.current_user():
        return redirect(url_for("views.dashboard"))
    return _login_page(nxt=request.args.get("next", "")[:MAX_NEXT])


@bp.post("/login")
def login_post():
    email, nxt = request.form.get("email", "")[:254], request.form.get("next", "")[:MAX_NEXT]
    try:
        user = auth.authenticate(email, request.form.get("password", ""))
    except auth.Throttled:
        return _login_page(auth.THROTTLED_MESSAGE, email, nxt, 429)
    except auth.Busy:
        return _login_page(auth.BUSY_MESSAGE, email, nxt, 503)
    if not user:
        # 200, not 401: this is a form shown again with its message. A 401 makes the browser log a red error and
        # suggests a challenge (WWW-Authenticate) that does not exist. The throttle page stays 429 and busy 503.
        return _login_page(auth.NO_MATCH_MESSAGE, email, nxt)
    auth.login_user(user)
    return redirect(auth.safe_next(nxt))


@bp.get("/signup")
def signup():
    return redirect(url_for("views.login"))             # there is no sign-up: the team uses the shared sign-in


@bp.post("/logout")
def logout():
    auth.logout_user()
    return redirect(url_for("views.login"))


# ---------------------------------------------------------------- dashboard
@bp.get("/")
def dashboard():
    if not auth.current_user():
        return render_template("landing.html")
    ctx = dashboard_context()
    if ctx["google"]["connected"] and ctx["google"]["linked"]:
        g.sheet_frame = True                    # only this page, and only now, may frame Google's preview (see the content policy)
    return render_template("dashboard.html", **ctx)


@bp.get("/fragment/stats")
@auth.login_required
def fragment_stats():
    return render_template("_stats.html", **dashboard_context())


@bp.get("/api/status")
@auth.login_required
def api_status():
    updated, problem = run_header()
    return jsonify(ok=True, run=current_app.manager.snapshot(), next_run=_next_run(config.load()),
                   updated=updated, problem=problem)


@bp.post("/api/run")
@auth.login_required
def api_run():
    started, reason, message = current_app.manager.start("manual", admin=bool(auth.current_user()["is_admin"]))
    code = 202 if started else (409 if reason == "busy" else 429)
    return jsonify(ok=started, reason=reason, message=message, run=current_app.manager.snapshot()), code


@bp.get("/api/preview")
@auth.login_required
def api_preview():
    info = google_info(config.load())
    rows = export.rows_for(store())
    cols = ("company", "website", "email", "phone", "address", "registered")
    return jsonify(ok=True, **info, total=len(rows), rows=[{k: r[k] for k in cols} for r in rows[:PREVIEW_ROWS]])


@bp.get("/download.<fmt>")
@auth.login_required
def download(fmt):
    if fmt not in ("xlsx", "csv"):
        abort(404)
    body = export.cached(store(), fmt)
    mime = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if fmt == "xlsx" else "text/csv")
    return Response(body, mimetype=mime,
                    headers={"Content-Disposition": f'attachment; filename="new-it-companies-{date.today().isoformat()}.{fmt}"',
                             "Cache-Control": "no-store"})


@bp.get("/healthz")
def healthz():
    return jsonify(ok=True)


@bp.get("/favicon.ico")
def favicon():
    """Browsers and link previewers ask for this name by habit, whatever the page's own <link> says."""
    return current_app.send_static_file("favicon-32.png")


# ----------------------------------------------------------------- settings
@bp.get("/settings")
@auth.admin_required
def settings():
    cfg = config.load()
    if not cfg["token"]:
        cfg = config.update(lambda c: c.__setitem__("token", c["token"] or sheet.new_token()))
    ctx = {"cfg": cfg, "google": google_info(cfg), "script": sheet.render_script(cfg["token"]),
           "source_labels": SOURCE_LABELS, "all_sources": list(sources.ALL), "next_run": next_run_text(cfg),
           "time": f"{cfg['schedule']['hour']:02d}:{cfg['schedule']['minute']:02d}"}
    return render_template("settings.html", **ctx)


def _body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, "Send JSON.")
    return data


def _fail(message: str, code: int = 400):
    return jsonify(ok=False, error=message), code


def _script_url_ok(url: str) -> bool:
    dev = os.environ.get("ITLEADS_DEV_SCRIPT_PREFIX", "")        # development only: a local stand-in for Google
    return bool(SCRIPT_URL.match(url)) or bool(dev and url.startswith(dev))


@bp.post("/api/settings/google")
@auth.admin_required
def settings_google():
    d = _body()
    url = str(d.get("url", "")).strip()
    if not _script_url_ok(url):
        return _fail("That is not an Apps Script Web app URL. It should look like https://script.google.com/macros/s/.../exec")
    cfg = config.load()
    link = bool(d["link"]) if "link" in d else bool(cfg.get("share_link", True))
    token = cfg["token"] or sheet.new_token()
    bridge = sheet.Bridge(url, token, timeout=120, retries=1)    # one try: a person is waiting for the answer
    try:
        bridge.ping()
        info = bridge.init(util.tz_name(), [], link=link)
    except sheet.BridgeError as e:
        return _fail(str(e))
    sid = str(info.get("id") or "")
    if not SHEET_ID.match(sid):
        return _fail("Google answered, but not with a sheet Hybrid Leads understands. Copy the script again, "
                     "deploy it as a new version and retry.")

    shared = bool(info.get("shared"))
    problem = str(info.get("share_error") or "")
    changed = sid != cfg.get("sheet_id")                 # a different sheet starts empty: send everything to it again

    def save(c):
        c.update(apps_script_url=url, token=token, sheet_id=sid, share_link=link and shared,
                 sheet_url=google_info({**c, "sheet_id": sid, "apps_script_url": url})["sheet_url"])

    cfg = config.update(save)
    if changed:
        store().reset_pushed()
    waiting = store().counts()["ready"]
    sending = current_app.manager.queue_send() if waiting else ""     # "started", "queued" or nothing to send
    warning = ""
    if link and not shared:                              # the account's rules said no: connected, but not embeddable
        warning = ("Google would not let this sheet be shared by link" + (f" ({problem})" if problem else "") +
                   ". The preview here shows a plain table; the sheet itself opens fine from your Drive.")
    return jsonify(ok=True, sheet_url=google_info(cfg)["sheet_url"], shared=shared, warning=warning, waiting=waiting,
                   sending=bool(sending), queued=sending == "queued", google=google_info(cfg))


@bp.post("/api/settings/sharing")
@auth.admin_required
def settings_sharing():
    link = bool(_body().get("link"))
    cfg = config.load()
    if cfg.get("apps_script_url"):
        try:
            res = sheet.Bridge(cfg["apps_script_url"], cfg["token"], timeout=60, retries=2).share(link)
        except sheet.BridgeError as e:
            return _fail(str(e))
        if link and not res.get("shared"):
            problem = str(res.get("share_error") or "")
            return _fail("Google would not let this sheet be shared by link" + (f" ({problem})" if problem else "") +
                         ". Your account's rules may forbid it. The preview here stays a plain table.")
    cfg = config.update(lambda c: c.update(share_link=link))
    return jsonify(ok=True, google=google_info(cfg))


@bp.post("/api/settings/schedule")
@auth.admin_required
def settings_schedule():
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(_body().get("time", "")).strip())
    if not m:
        return _fail("Use a time like 07:30.")
    cfg = config.update(lambda c: c.update(schedule={"hour": int(m.group(1)), "minute": int(m.group(2))}))
    return jsonify(ok=True, next_run=next_run_text(cfg))


@bp.post("/api/settings/rules")
@auth.admin_required
def settings_rules():
    d = _body()
    wanted, picked = d.get("require"), d.get("sources")
    if not isinstance(wanted, list) or not isinstance(picked, dict):
        return _fail("That form was incomplete. Reload the page and try again.")
    req = [x for x in ("website", "email", "phone") if x in wanted]
    if "website" not in req:
        req.insert(0, "website")                        # a company without a website is never listed
    srcs = {k: bool(picked.get(k)) for k in sources.ALL}
    if not any(srcs.values()):
        return _fail("Turn on at least one registry.")
    it = bool(d.get("require_it_signal", True))
    old = bool(d.get("skip_established", False))
    either = bool(d.get("contact_either", False))
    cfg = config.update(lambda c: c.update(require=req, require_it_signal=it, skip_established=old, contact_either=either,
                                           sources=srcs))
    released = pipeline.requalify(store(), cfg, date.today())       # companies held back earlier may qualify now
    sending = bool(released and cfg.get("apps_script_url") and current_app.manager.queue_send())
    return jsonify(ok=True, released=released, sending=sending)
