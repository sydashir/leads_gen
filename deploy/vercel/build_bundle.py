"""Build the folder that is deployed to Vercel: a read-only copy of Hybrid Leads.

    python deploy/vercel/build_bundle.py            -> deploy/vercel/dist/
    python deploy/vercel/build_bundle.py --out DIR  -> DIR

The bundle holds only what a read-only copy needs: the program, a snapshot of the list, and Vercel's settings.
It never holds config.json (Google token, session secret), the accounts database, logs, tests or the team
password: those stay on this computer or in Vercel's environment variables. In the snapshot, a company that is
not on the list is reduced to a count (no name, no address, no website)."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from importlib import metadata
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PACKAGES = ("flask", "requests", "beautifulsoup4", "lxml", "openpyxl")      # waitress is not needed: Vercel runs the app itself
PYTHON = "3.13"
LISTED = ("ready", "pushed")

# The same caching the app itself sends (itleads/web/__init__.py): an address with ?v=<version of the styles and script> never
# changes its content, so it is kept for a year; logo, fonts and icons have no version and are kept for a day. The two rules
# exclude each other (has / missing), so their order does not matter. Pages are not static files and stay uncached.
CACHE_VERSIONED = "public, max-age=31536000, immutable"
CACHE_UNVERSIONED = "public, max-age=86400"
VERCEL_JSON = {
    "$schema": "https://openapi.vercel.sh/vercel.json",
    "functions": {"app.py": {"maxDuration": 30}},
    "headers": [
        {"source": "/(.*)", "headers": [{"key": "X-Robots-Tag", "value": "noindex, nofollow"}]},
        {"source": "/static/(.*)", "has": [{"type": "query", "key": "v"}],
         "headers": [{"key": "Cache-Control", "value": CACHE_VERSIONED}]},
        {"source": "/static/(.*)", "missing": [{"type": "query", "key": "v"}],
         "headers": [{"key": "Cache-Control", "value": CACHE_UNVERSIONED}]},
    ],
}
ROBOTS = "User-agent: *\nDisallow: /\n"


class BundleError(Exception):
    pass


def team_password(config_py: str) -> str:
    m = re.search(r'"login":\s*\{[^}]*"password":\s*"([^"]*)"', config_py)
    return m.group(1) if m else ""


def blank_password(config_py: str) -> str:
    """The shipped program must not carry the team password: it comes from the environment (fails closed without it)."""
    new, n = re.subn(r'("login":\s*\{[^}]*"password":\s*")[^"]*(")', r"\1\2", config_py, count=1)
    if n != 1:
        raise BundleError("could not find the default login in itleads/config.py")
    return new


def known_secrets(config_json: Path) -> list:
    """Values that must never be uploaded: this computer's team password, session secret and sheet secret."""
    try:
        c = json.loads(Path(config_json).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    app = c.get("app") or {}
    found = [(app.get("login") or {}).get("password"), app.get("secret_key"), c.get("token")]
    return [v for v in found if isinstance(v, str) and len(v) >= 8]


def pinned_requirements() -> str:
    lines = []
    for name in PACKAGES:
        try:
            lines.append(f"{name}=={metadata.version(name)}")
        except metadata.PackageNotFoundError:
            wanted = next((ln.strip() for ln in (REPO / "requirements.txt").read_text().splitlines()
                           if ln.lower().startswith(name)), name)
            lines.append(wanted)
    return "\n".join(lines) + "\n"


def make_snapshot(src_db: Path, dst_db: Path) -> dict:
    """A copy of the list for the read-only site. Companies that are not listed keep only what the counts need."""
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    dst = sqlite3.connect(str(dst_db))
    try:
        src.backup(dst)
    finally:
        src.close()
    dst.row_factory = sqlite3.Row
    try:
        others = dst.execute("SELECT id, source, enrich FROM companies WHERE state NOT IN (?, ?) ORDER BY id", LISTED).fetchall()
        for n, r in enumerate(others, 1):
            e = json.loads(r["enrich"]) if r["enrich"] else None
            enrich = None if e is None else json.dumps({"website": "yes" if e.get("website") else ""})
            dst.execute("UPDATE companies SET id=?, raw=?, enrich=?, domain=NULL, pushed_hash=NULL, next_due=NULL, "
                        "pushed_at=NULL, ready_at=NULL WHERE id=?",
                        (f"{r['source']}:x{n}", json.dumps({"source": r["source"]}), enrich, r["id"]))
        dst.execute("DELETE FROM source_state")
        dst.commit()
        dst.execute("PRAGMA journal_mode=DELETE")
        dst.execute("VACUUM")
        listed = dst.execute("SELECT COUNT(*) FROM companies WHERE state IN (?, ?)", LISTED).fetchone()[0]
        total = dst.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        leaked = [r[0] for r in dst.execute("SELECT raw FROM companies WHERE state NOT IN (?, ?)", LISTED)
                  if set(json.loads(r[0])) != {"source"}]
        if leaked:
            raise BundleError("a company that is not listed still carries its details")
    finally:
        dst.close()
    return {"listed": listed, "total": total, "reduced": len(others)}


SHEET_ID = re.compile(r"^[A-Za-z0-9_\-]{6,120}$")


def sheet_url(config_json: Path) -> str:
    """The address of the Google Sheet (not a secret: opening it still needs the owner or a share). '' when none is connected."""
    try:
        c = json.loads(Path(config_json).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    sid = str(c.get("sheet_id") or "")
    return f"https://docs.google.com/spreadsheets/d/{sid}/edit" if c.get("apps_script_url") and SHEET_ID.match(sid) else ""


def build(out: Path, repo: Path = REPO, source_db: Path | None = None, now: datetime | None = None,
          config_json: Path | None = None) -> dict:
    out = Path(out).resolve()
    if out == repo or out in repo.parents or repo / "itleads" in (out, *out.parents):
        raise BundleError(f"refusing to build into {out}")
    if out.exists():                                   # only ever replace an earlier bundle (or an empty folder)
        if any(out.iterdir()) and not (out / "vercel.json").exists():
            raise BundleError(f"{out} exists and is not an earlier bundle: choose another --out")
        shutil.rmtree(out)
    out.mkdir(parents=True)
    package = repo / "itleads"
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store")
    shutil.copytree(package, out / "itleads", ignore=ignore)
    config_py = (out / "itleads" / "config.py").read_text(encoding="utf-8")
    password = team_password(config_py)
    (out / "itleads" / "config.py").write_text(blank_password(config_py), encoding="utf-8")
    # Vercel's CDN serves public/**; the same files stay inside the package as a fallback
    shutil.copytree(package / "web" / "static", out / "public" / "static", ignore=ignore)
    (out / "public" / "robots.txt").write_text(ROBOTS, encoding="utf-8")

    db = Path(source_db) if source_db else repo / "data" / "leads.db"
    if not db.exists():
        raise BundleError(f"no list to publish: {db} does not exist (run the tool once first)")
    stats = make_snapshot(db, out / "snapshot" / "leads.db")
    now = now or datetime.now()
    info = {"taken": now.isoformat(timespec="seconds"), "taken_date": now.date().isoformat(),
            "taken_label": f"{now.day} {now:%b %Y}, {now:%H:%M} {time.strftime('%Z') or ''}".strip(),
            "sheet_url": sheet_url(config_json or repo / "config.json"), **stats}
    (out / "snapshot" / "info.json").write_text(json.dumps(info, indent=1), encoding="utf-8")

    shutil.copyfile(HERE / "app.py", out / "app.py")
    (out / "requirements.txt").write_text(pinned_requirements(), encoding="utf-8")
    (out / ".python-version").write_text(PYTHON + "\n", encoding="utf-8")
    (out / "vercel.json").write_text(json.dumps(VERCEL_JSON, indent=2) + "\n", encoding="utf-8")
    check(out, known_secrets(repo / "config.json") + ([password] if password else []))
    return info


def check(out: Path, secrets) -> None:
    """Nothing private may be in the folder that is uploaded."""
    banned_names = {"config.json", "app.db", ".env"}
    for p in out.rglob("*"):
        if not p.is_file():
            continue
        if p.name in banned_names or p.suffix in (".log", ".pem", ".key") or p.name.startswith("export-"):
            raise BundleError(f"{p.relative_to(out)} must not be uploaded")
        if secrets and p.suffix not in (".db", ".png", ".woff2"):
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if any(s and s in text for s in secrets):
                raise BundleError(f"a secret of this installation is inside {p.relative_to(out)}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(HERE / "dist"), help="where to build (default: deploy/vercel/dist)")
    ap.add_argument("--db", help="the list to publish (default: data/leads.db)")
    a = ap.parse_args(argv)
    try:
        info = build(Path(a.out), source_db=Path(a.db) if a.db else None)
    except BundleError as e:
        print(f"Not built: {e}", file=sys.stderr)
        return 1
    print(f"Built {a.out}\n  snapshot of {info['taken_label']}: {info['listed']} listed, "
          f"{info['reduced']} other companies reduced to a count (of {info['total']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
