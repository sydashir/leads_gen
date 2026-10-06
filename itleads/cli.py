"""Command line: serve (the web app), run, status, doctor, export, open, install-service."""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
import threading
import time
import webbrowser
from datetime import date
from pathlib import Path

from . import afterrun, config, pipeline, service, sheet, sources, util
from .store import Store
from .web import appdb
from .web.jobs import counts_for_today, next_run_text

TTY = sys.stdout.isatty()
PROTECTED = (("documents",), ("desktop",), ("downloads",), ("library", "mobile documents"))   # macOS asks before background jobs touch these


def c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if TTY else s


bold = lambda s: c("1", s)
dim = lambda s: c("2", s)
accent = lambda s: c("38;5;29", s)
warn = lambda s: c("38;5;166", s)


def say(msg: str = "") -> None:
    print(msg, flush=True)


def banner() -> None:
    say()
    say("  " + bold("Hybrid Leads"))
    say("  " + dim("New IT company filings, checked every day."))
    say()


def in_protected_folder() -> bool:
    """macOS file names are not case-sensitive, so ~/desktop/it-leads is the Desktop too."""
    try:
        parts = [p.lower() for p in config.ROOT.resolve().relative_to(Path.home().resolve()).parts]
    except ValueError:
        return False
    return any(parts[:len(p)] == list(p) for p in PROTECTED)


def app_url(cfg: dict, host: str | None = None, port: int | None = None) -> str:
    host = host or os.environ.get("ITLEADS_HOST") or cfg["app"]["host"]
    port = port or int(os.environ.get("ITLEADS_PORT") or cfg["app"]["port"])
    return f"http://{'127.0.0.1' if host in ('0.0.0.0', '', '::') else host}:{port}"


# ------------------------------------------------------------------------ serve
def is_local(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


def auth_internal(cfg: dict) -> dict:
    from .web import auth
    return auth.internal_login(cfg)


def login_line(lg: dict) -> str:
    """The sign-in line of the start-up banner. The password is never printed: this output ends up in log files."""
    if not lg["email"] or not lg["password"]:
        return bold("Sign in    ") + warn("no team password is set, so nobody can sign in. Set one with:  ./it-leads set-login --generate")
    return bold("Sign in    ") + lg["email"] + dim("   (the team password is set with ./it-leads set-login and is never printed)")


def wait_until_up(url: str, seconds: int = 90) -> bool:
    import urllib.request
    end = time.time() + seconds
    while True:
        try:
            if urllib.request.urlopen(url + "/healthz", timeout=2).status == 200:
                return True
        except Exception:
            pass
        if time.time() >= end:
            return False
        time.sleep(1)


def cmd_serve(a) -> int:
    from waitress import create_server
    from .web import create_app, extra_hosts, secure_cookies
    from .web.jobs import Scheduler
    os.umask(0o077)                                    # data, logs and the database stay private to this user
    cfg = config.load()
    config.ensure_dirs()
    host = a.host or os.environ.get("ITLEADS_HOST") or cfg["app"]["host"]
    port = a.port or int(os.environ.get("ITLEADS_PORT") or cfg["app"]["port"])
    proxy = os.environ.get("ITLEADS_TRUSTED_PROXY") or cfg["app"].get("trusted_proxy") or ""
    url = app_url(cfg, host, port)
    exposed = not is_local(host) or bool(proxy) or secure_cookies()
    app = create_app(enforce_hosts=not exposed or bool(extra_hosts(cfg)), seed_login=True)
    limits = dict(threads=8, ident="it-leads", max_request_body_size=64 * 1024, connection_limit=200,
                  channel_timeout=60, clear_untrusted_proxy_headers=True)
    if proxy:
        limits.update(trusted_proxy=proxy, trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto"})
    try:
        server = create_server(app, host=host, port=port, **limits)       # binds now: a busy port is known before any banner
    except OSError as e:
        if wait_until_up(url, 2):
            say("  " + f"It is already running at {accent(url)}.")
            return 0
        say("  " + warn(f"Could not start on {host}:{port} ({e.strerror or e}). Another program is using that port. "
                        f"Stop it, or set another port: \"app\": {{\"port\": 8766}} in {config.CONFIG_PATH}."))
        return 1
    app.scheduler = Scheduler(app.manager)
    app.scheduler.start()
    app.manager.listeners.append(afterrun.listener)
    banner()
    say("  " + bold("Running at  ") + accent(url))
    say("  " + dim(f"Daily run at {cfg['schedule']['hour']:02d}:{cfg['schedule']['minute']:02d} while this is running. "
                    "Press Ctrl+C to stop."))
    say("  " + login_line(auth_internal(cfg)))
    if exposed and not secure_cookies():
        say("  " + warn("This is reachable by others over plain http, so passwords are not encrypted on the way. "
                        "Put it behind https and start it with ITLEADS_SECURE_COOKIES=1."))
    if secure_cookies() and not proxy:
        say("  " + warn("Secure cookies are on but no trusted_proxy is set. Behind a reverse proxy (even on this machine) "
                        "set trusted_proxy, or every visitor looks like the proxy and the sign-in limits hit everyone."))
    if in_protected_folder():
        say("  " + warn("This folder is inside Documents, Desktop or Downloads; macOS may block the background "
                        "service there. Move it to ~/it-leads before running install-service."))
    say()
    if a.open:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    server.run()
    # run() returns quietly on Ctrl+C. A lookup run may still be going in the background (its helper threads would
    # keep this process alive for as long as the whole backlog takes, still holding the run lock and the port): code 130
    # makes main() end the process at once. Nothing is lost: every finished lookup is saved, the rest is picked up later.
    say("\n  Stopped.")
    return 130


# -------------------------------------------------------------------------- run
def cmd_run(a) -> int:
    cfg = config.load()
    try:
        since = date.fromisoformat(a.since) if a.since else None
    except ValueError:
        say("Use --since YYYY-MM-DD, for example --since 2026-09-01.")
        return 2
    only = [x.strip() for x in a.source.split(",") if x.strip()] if a.source else None
    if only:
        unknown = [x for x in only if x not in sources.ALL]
        if unknown:
            say(f"Unknown registry {', '.join(unknown)}. Choose from: {', '.join(sources.ALL)}.")
            return 2
    if a.limit is not None and a.limit < 1:
        say("--limit must be 1 or more.")
        return 2
    try:
        with pipeline.run_lock():
            res = pipeline.run(cfg, dry_run=a.dry_run, since=since, only=only, limit=a.limit, quiet=a.scheduled,
                               next_run=next_run_text(cfg, assume_done=counts_for_today(cfg, "schedule" if a.scheduled else "manual")),
                               trigger="schedule" if a.scheduled else "manual")
    except pipeline.Busy as e:
        say(f"Skipped: {e}.")
        return 0
    return 0 if res["status"] in ("ok", "partial", "offline") else 1


# ----------------------------------------------------------------------- status
def cmd_status(a) -> int:
    cfg = config.load()
    store = Store(config.DATA / "leads.db")
    banner()
    say("  " + bold("App        ") + app_url(cfg) + (dim("   (kept running by the Mac)") if service.installed() else
                                                      dim("   (start it: ./it-leads serve)")))
    say("  " + bold("Accounts   ") + str(appdb.count_users()))
    lg = auth_internal(cfg)
    say("  " + bold("Sign in    ") + (lg["email"] or warn("no team email set")) + "  "
        + (dim("(password set, never printed)") if lg["password"] else warn("(no password set: ./it-leads set-login --generate)")))
    say("  " + bold("Sheet      ") + (cfg["sheet_url"] or warn("not connected: open Settings in the app")))
    say("  " + bold("Daily run  ") + f"{cfg['schedule']['hour']:02d}:{cfg['schedule']['minute']:02d}  " + dim(next_run_text(cfg)))
    say("  " + bold("Registries ") + ", ".join(k for k, v in cfg["sources"].items() if v))
    last = store.last_run()
    if last:
        s = last["summary"]
        say("  " + bold("Last run   ") + f"{last['started'][:16].replace('T', ' ')}  {s.get('status')}  "
            f"{s.get('added', s.get('pushed', 0))} added, {s.get('held', 0)} held back, {s.get('seconds', 0)}s")
        for err in s.get("errors", []):
            say("             " + warn(err))
    else:
        say("  " + bold("Last run   ") + dim("none yet"))
    n = store.counts()
    w = store.held_breakdown()
    say("  " + bold("Companies  ") + f"{n['pushed'] + n['ready']} listed, {n['dropped']} dropped")
    say("  " + dim(f"             waiting: {w['no_website']} without a website yet, "
                    f"{w['missing_contact']} with a website but missing email or phone, "
                    f"{w['not_looked_up']} not looked up yet"))
    say()
    return 0


# ----------------------------------------------------------------------- doctor
def cmd_doctor(a) -> int:
    cfg = config.load()
    banner()
    bad = 0

    def line(ok, label, detail="", todo=False):
        nonlocal bad
        if not ok and not todo:
            bad += 1
        mark = accent("ok  ") if ok else (dim("todo") if todo else warn("FAIL"))
        say(f"  {mark}  {label:<22}{dim(detail)}")

    line(True, "python", sys.version.split()[0])
    line(not in_protected_folder(), "install location",
         str(config.ROOT) if not in_protected_folder() else "inside Documents/Desktop/Downloads: move it to ~/it-leads")
    for s in sources.build({k: True for k in sources.ALL}):
        t = time.time()
        try:
            for h in s.hosts:
                if not util.ensure_resolvable(h):
                    raise RuntimeError(f"{h} does not resolve")
            line(True, s.label, f"{s.check()}  ({time.time() - t:.0f}s)")
        except Exception as e:
            line(False, s.label, str(e)[:100])
    if cfg["apps_script_url"]:
        try:
            p = sheet.Bridge(cfg["apps_script_url"], cfg["token"]).ping()
            line(True, "google sheet", "connected" if p.get("configured") else "script ok, sheet not created")
        except sheet.BridgeError as e:
            line(False, "google sheet", str(e)[:100])
    else:
        line(False, "google sheet", "not connected yet (Settings in the app)", todo=True)
    n = appdb.count_users()
    line(n > 0, "accounts", str(n) if n else "none yet: run ./it-leads set-login --generate", todo=True)
    on, up = service.installed(), wait_until_up(app_url(cfg), 1)
    if on and up:
        line(True, "background service", "running")
    elif on:
        line(False, "background service", f"installed but not answering: read {config.LOGS / 'web.out.log'}")
    else:
        line(True, "background service", "running (started by hand)" if up else "not installed (optional: ./it-leads install-service)")
    say()
    return 1 if bad else 0


def cmd_open(a) -> int:
    url = app_url(config.load())
    if not wait_until_up(url, 1):
        say(f"The app is not running at {url}. Start it with: ./it-leads serve --open")
        return 1
    webbrowser.open(url)
    return 0


def cmd_export(a) -> int:
    from .web import export
    store = Store(config.DATA / "leads.db")
    base = Path(os.environ.get("ITLEADS_CWD") or Path.cwd())          # where the person typed the command
    out = (base / Path(a.out).expanduser()) if a.out else config.ROOT / f"new-it-companies-{date.today().isoformat()}.csv"
    if a.incomplete:
        cols = ["company", "website", "email", "phone", "address", "registered", "state", "source", "stage", "missing"]
        n = 0
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for it in store.all_records():
                if it["state"] not in ("held", "new") or not it["enrich"]:
                    continue
                row = sheet.sheet_row(it)
                cells = {"company": row["company"], "website": row["website"], "email": row["email"],
                         "phone": row["phone"], "address": row["address"], "registered": row["registered"],
                         "state": row["state"], "source": row["source"], "stage": it["state"],
                         "missing": ", ".join(it["enrich"].get("missing", []))}
                w.writerow({k: export.csv_safe(v) for k, v in cells.items()})       # registry text must not run as a formula
                n += 1
    else:
        rows = export.rows_for(store)
        out.write_bytes(export.to_csv(rows))
        n = len(rows)
    store.close()
    say(f"Wrote {n} rows to {out}")
    return 0


def cmd_install_service(a) -> int:
    if in_protected_folder():
        say(warn(f"This folder is inside Documents, Desktop or Downloads. Move it first: mv '{config.ROOT}' ~/it-leads"))
        return 1
    if not service.available():
        say(warn("The background service is a macOS feature (launchd). On another system, run './it-leads serve' "
                 "under systemd or supervisor."))
        return 1
    service.install()
    url = app_url(config.load())
    say("Installed. Starting the app...")
    if wait_until_up(url):
        say(f"Running at {accent(url)}. It starts when you log in and restarts if it stops.")
        return 0
    say(warn(f"Installed, but {url} is not answering yet. Give it a minute and open it again. If it never opens, read "
             f"{config.LOGS / 'web.out.log'} and {config.LOGS / 'web.err.log'}; macOS may also be asking you to allow "
             "it under System Settings > General > Login Items & Extensions."))
    return 1


def cmd_set_login(a) -> int:
    """Store the team login in config.json (never in the code): ask for a password, or make a random one and show it once."""
    import getpass
    import secrets
    from .web import auth
    email = auth.norm_email(a.email or auth.internal_login(config.load())["email"])
    if "@" not in email or " " in email:
        say(warn("Give the team email, for example:  ./it-leads set-login team@yourcompany.com"))
        return 1
    if a.generate:
        letters = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"        # no 0/O/1/l/I: easy to read out and retype
        pw = "-".join("".join(secrets.choice(letters) for _ in range(4)) for _ in range(4))
    else:
        pw = getpass.getpass("  New team password: ")
        if pw != getpass.getpass("  Again: "):
            say(warn("The two passwords differ. Nothing was changed."))
            return 1
    try:
        auth.validate_password(pw)
        if len(pw) < 12:
            raise auth.AuthError("The whole team shares this password: use at least 12 characters.")
    except auth.AuthError as e:
        say(warn(str(e)))
        return 1
    config.update(lambda c: c["app"].__setitem__("login", {**(c["app"].get("login") or {}), "email": email, "password": pw}))
    say(f"Saved the team login for {email} in config.json (readable by you only).")
    if a.generate:
        say("  The password is " + bold(pw) + ". It is shown only now: give it to the team.")
    say("  Restart the app to apply it.")
    return 0


def cmd_reset_password(a) -> int:
    import getpass
    from .web import auth
    email = a.email.strip().lower()
    team = auth.internal_login(config.load())
    if email and email == team["email"]:
        # the team login: its password is whatever config.json says, applied again at every start, so setting one here
        # would only last until then. What a locked-out person needs is the lockout lifted.
        appdb.clear_all_lockouts()
        say("Sign-in is unlocked.")
        say(f"{email} is the team login. Its password comes from config.json (app > login) or ITLEADS_LOGIN_PASSWORD and is "
            "applied again at every start. To change it, run  ./it-leads set-login  and restart the app.")
        return 0
    user = appdb.user_by_email(email)
    if not user:
        others = ", ".join(u["email"] for u in appdb.list_users())
        say(warn(f"No account with the email {a.email}. " + (f"Accounts: {others}" if others else
                                                           f"The only login is the team login, {team['email']}.")))
        return 1
    pw = getpass.getpass("  New password: ")
    if pw != getpass.getpass("  Again: "):
        say(warn("The two passwords differ. Nothing was changed."))
        return 1
    try:
        auth.validate_password(pw)
    except auth.AuthError as e:
        say(warn(str(e)))
        return 1
    appdb.set_password(user["id"], auth.hash_password(pw))              # also signs that account out everywhere
    appdb.clear_lockout(user["email"])                                  # and lets them try the new one right away
    say(f"Password changed for {user['email']}. Sign-in is unlocked.")
    return 0


def cmd_uninstall_service(a) -> int:
    if not service.available():
        say("There is no background service on this system.")
        return 0
    service.uninstall()
    say("Removed. The app no longer starts by itself.")
    return 0


def _backup_db(src: Path, dst: Path) -> None:
    """A consistent copy of the database (SQLite's own backup, so a half-written WAL is included), readable by this user only."""
    a, b = sqlite3.connect(str(src), timeout=30), sqlite3.connect(str(dst))
    try:
        a.backup(b)
    finally:
        b.close()
        a.close()
    os.chmod(dst, 0o600)


def cmd_repair(a) -> int:
    """Judge every company on the list again with today's checks, once: companies already sent to the sheet get cleaned values
    (no function mailbox, no placeholder phone, a contact who is a person) or, when they no longer qualify, go back to held and
    are looked up again on the next run. Nothing is looked up on the internet and nothing is sent. Dry run unless --apply."""
    import shutil
    import tempfile
    cfg = config.load()
    db = config.DATA / "leads.db"
    if not db.exists():
        say("There is no list yet, so there is nothing to repair.")
        return 1
    lines: list = []
    try:
        with pipeline.run_lock():                                     # no run may be writing while this reads and writes
            if a.apply:
                dst = config.DATA / "leads-before-repair.db"
                if dst.exists():                                      # keep the first backup; a later repair gets its own
                    dst = config.DATA / f"leads-before-repair-{time.strftime('%Y%m%d-%H%M%S')}.db"
                _backup_db(db, dst)
                say(f"Copied the database to {dst} first.")
                store, tmp = Store(db), None
            else:                                                     # a dry run does the same pass on a throwaway copy
                tmp = tempfile.mkdtemp(prefix="itleads-repair-")
                _backup_db(db, Path(tmp) / "leads.db")
                store = Store(Path(tmp) / "leads.db")
            try:
                st = pipeline.requalify_pass(store, cfg, date.today(), lines.append)
                listed = store.counts()
            finally:
                store.close()
                if tmp:
                    shutil.rmtree(tmp, ignore_errors=True)
    except pipeline.Busy as e:
        say(f"Skipped: {e}. Try again when the run has finished.")
        return 0
    say(("Changes made:" if a.apply else "Dry run, nothing was written. This is what --apply would do:"))
    for ln in lines[:200]:
        say("  " + ln.strip())
    if len(lines) > 200:
        say(f"  ... and {len(lines) - 200} more")
    say(f"  {st['unlisted']} companies already sent to the sheet no longer qualify: held back, looked up again on the next run")
    say(f"  {st['demoted']} companies waiting to be sent no longer qualify: held back or dropped")
    say(f"  {st['released']} companies held back earlier now qualify")
    say(f"  {st['cleaned']} companies kept, with their stored values cleaned")
    if st["errors"]:
        say(warn(f"  {st['errors']} companies could not be read and were left as they are (see above)"))
    say(f"  Listed afterwards: {listed['pushed'] + listed['ready']}.")
    say("  Rows already in the sheet are not changed (the script only fills empty cells); the downloads and the preview use the cleaned values.")
    if not a.apply:
        say("  To write these changes run:  ./it-leads repair --apply")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="it-leads", description="New IT company filings, checked every day.")
    p.add_argument("--debug", action="store_true", help="show full error details")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="start the web app")
    s.add_argument("--host"); s.add_argument("--port", type=int)
    s.add_argument("--open", action="store_true", help="open it in your browser")
    s.set_defaults(fn=cmd_serve)
    r = sub.add_parser("run", help="fetch, look up and (if Google is connected) send now, without the web app")
    r.add_argument("--dry-run", action="store_true", help="do everything except touch the sheet")
    r.add_argument("--since", help="YYYY-MM-DD")
    r.add_argument("--registry", "--source", dest="source", metavar="REGISTRIES", help="only these registries, for example ct,tx")
    r.add_argument("--limit", type=int); r.add_argument("--scheduled", action="store_true")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("status", help="what happened lately").set_defaults(fn=cmd_status)
    sub.add_parser("doctor", help="check every registry, the sheet and the background service").set_defaults(fn=cmd_doctor)
    sub.add_parser("open", help="open the app in your browser").set_defaults(fn=cmd_open)
    e = sub.add_parser("export", help="write a CSV of the list")
    e.add_argument("--incomplete", action="store_true", help="the ones held back"); e.add_argument("--out")
    e.set_defaults(fn=cmd_export)
    sub.add_parser("install-service", help="keep the app running on this Mac, starting at login").set_defaults(fn=cmd_install_service)
    sub.add_parser("uninstall-service", help="stop keeping the app running").set_defaults(fn=cmd_uninstall_service)
    sl = sub.add_parser("set-login", help="set the team email and password (stored in config.json, never in the code)")
    sl.add_argument("email", nargs="?", help="the team email (default: the one already set)")
    sl.add_argument("--generate", action="store_true", help="make a random password and show it once")
    sl.set_defaults(fn=cmd_set_login)
    rp = sub.add_parser("reset-password", help="unlock sign-in after too many wrong passwords (the team password is set with set-login)")
    rp.add_argument("email"); rp.set_defaults(fn=cmd_reset_password)
    rr = sub.add_parser("repair", help="judge every company on the list again with today's checks (dry run; --apply writes)")
    rr.add_argument("--apply", action="store_true", help="write the changes (the database is copied to data/leads-before-repair.db first)")
    rr.set_defaults(fn=cmd_repair)
    a = p.parse_args(argv)
    try:
        code = a.fn(a)
    except KeyboardInterrupt:
        say("\n  Stopped.")
        code = 130
    except ImportError as e:
        if a.debug:
            raise
        say("  " + warn(f"A part of the private Python environment is missing or does not fit this Mac ({e}). "
                        "Rebuild it:  rm -rf .venv && ./it-leads status"))
        code = 1
    except sqlite3.Error as e:
        if a.debug:
            raise
        say("  " + warn(f"The database in {config.DATA} could not be opened ({e}). If that folder is damaged, move it "
                        "away and start again (the list is rebuilt from the public registries)."))
        code = 1
    except (ValueError, OSError, RuntimeError, sheet.BridgeError) as e:
        if a.debug:
            raise
        say("  " + warn(f"Error: {e}"))
        code = 1
    if getattr(a, "scheduled", False) or code == 130:
        sys.stdout.flush()
        os._exit(code)          # lookups may still be running in the background; do not wait for them
    return code
