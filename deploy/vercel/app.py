"""Vercel entrypoint: a read-only copy of Hybrid Leads (a snapshot of the list; no runs, no settings).

Vercel runs this file as a serverless function. Nothing lasts between instances except what is bundled with the
deployment, so: the snapshot database ships in ./snapshot and is copied to /tmp (the only writable place) when an
instance starts; the sign-in and the session secret come from environment variables, not from files.
Required environment variables: ITLEADS_LOGIN_EMAIL, ITLEADS_LOGIN_PASSWORD, ITLEADS_SECRET_KEY."""
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("ITLEADS_HOME") or "/tmp/itleads")
os.environ["ITLEADS_HOME"] = str(HOME)                        # must be set before the package reads its settings
os.environ.setdefault("ITLEADS_READ_ONLY", "1")
os.environ.setdefault("ITLEADS_SECURE_COOKIES", "1")
os.environ.setdefault("ITLEADS_PROXY_FIX", "1")               # Vercel puts the visitor's address in X-Forwarded-For
sys.path.insert(0, str(HERE))

(HOME / "data").mkdir(parents=True, exist_ok=True)
snapshot = HERE / "snapshot"
if (snapshot / "leads.db").exists() and not (HOME / "data" / "leads.db").exists():
    shutil.copyfile(snapshot / "leads.db", HOME / "data" / "leads.db")
try:
    info = json.loads((snapshot / "info.json").read_text(encoding="utf-8"))
    os.environ.setdefault("ITLEADS_SNAPSHOT_AT", info["taken_label"])
    os.environ.setdefault("ITLEADS_SNAPSHOT_DATE", info["taken_date"])
    if info.get("sheet_url"):
        os.environ.setdefault("ITLEADS_SHEET_URL", info["sheet_url"])
except (OSError, KeyError, ValueError):
    pass

for needed in ("ITLEADS_LOGIN_PASSWORD", "ITLEADS_SECRET_KEY"):
    if not os.environ.get(needed):
        raise RuntimeError(f"{needed} is not set for this deployment (vercel env add {needed} production).")

from itleads.web import create_app  # noqa: E402  (needs the environment prepared above)

app = create_app(enforce_hosts=False, seed_login=True)
