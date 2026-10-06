"""The folder published on Vercel: what goes in, and above all what stays out."""
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "deploy" / "vercel"))
import build_bundle  # noqa: E402
from itleads.store import Store  # noqa: E402


def company(name, address="1 Hidden Street", email="owner@secret-holdings.example", source="tx", state="TX"):
    return {"id": f"{source}:{name}", "source": source, "name": name, "trade_name": "", "state": state, "city": "Austin",
            "address": address, "zip": "78701", "registered": "2026-10-02", "industry": "Software",
            "individual": False, "emails": [email], "people": []}


def enrichment(site):
    return {"website": site, "email": "hi@acme.io", "email_from": "company website", "emails": [], "phone": "5122100100",
            "phone_from": "company website", "why": ["full legal name on the site"], "created": "2026-09-30",
            "contact": {"name": "Ada Lovelace", "title": "Founder", "email": "", "linkedin": ""}, "linkedin": "",
            "linkedin_company": "", "it": {"level": "strong"}, "domain": "acme.io"}


class BundleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "leads.db"
        s = Store(self.db)
        today = date(2026, 10, 6)
        s.add(company("Acme LLC", address="1 Main Street", email="owner@acme.io"), today)
        s.record_attempt("tx:Acme LLC", enrichment("https://acme.io"), "ready", today)
        s.add(company("Secret Holdings LLC"), today)
        s.record_attempt("tx:Secret Holdings LLC", {**enrichment("https://secret-holdings.example"), "missing": ["phone"]}, "held", today)
        s.add(company("Nowhere Inc"), today)
        s.record_attempt("tx:Nowhere Inc", {"website": "", "email": "", "phone": "", "missing": ["website"]}, "held", today)
        s.log_run("2026-10-06T07:00:00", "2026-10-06T07:01:10", {"status": "ok", "added": 3, "held": 2, "pushed": 0})
        s.close()
        self.out = self.tmp / "dist"
        self.info = build_bundle.build(self.out, source_db=self.db, now=datetime(2026, 10, 6, 18, 45))

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_folder_holds_what_vercel_needs(self):
        for name in ("app.py", "vercel.json", ".python-version", "requirements.txt", "public/robots.txt", "public/static/app.css",
                     "public/static/landing.css", "public/static/app.js", "public/static/hybrid-logo.png",
                     "itleads/web/static/landing.css", "itleads/web/views.py", "snapshot/leads.db", "snapshot/info.json"):
            self.assertTrue((self.out / name).exists(), name)
        self.assertNotIn("waitress", (self.out / "requirements.txt").read_text())
        self.assertIn("flask==", (self.out / "requirements.txt").read_text())
        cfg = json.loads((self.out / "vercel.json").read_text())
        self.assertEqual(cfg["functions"]["app.py"]["maxDuration"], 30)
        info = json.loads((self.out / "snapshot" / "info.json").read_text())
        self.assertEqual((info["taken_date"], info["listed"]), ("2026-10-06", 1))
        self.assertTrue(info["taken_label"].startswith("6 Oct 2026, 18:45"))

    def test_every_static_file_ships_twice_and_the_cache_rules_match_the_app(self):
        """The CDN serves public/static; the same files stay in the package as the app's own fallback (landing.css and any
        file added later included: the whole folder is copied)."""
        static = ROOT / "itleads" / "web" / "static"
        names = sorted(str(p.relative_to(static)) for p in static.rglob("*") if p.is_file() and p.name != ".DS_Store")
        self.assertIn("landing.css", names)
        for where in (self.out / "public" / "static", self.out / "itleads" / "web" / "static"):
            shipped = sorted(str(p.relative_to(where)) for p in where.rglob("*") if p.is_file())
            self.assertEqual(shipped, names, str(where))
            self.assertEqual((where / "landing.css").read_bytes(), (static / "landing.css").read_bytes())
        rules = [h for h in json.loads((self.out / "vercel.json").read_text())["headers"] if h["source"] == "/static/(.*)"]
        by_kind = {("has" if "has" in r else "missing"): r for r in rules}
        self.assertEqual(sorted(by_kind), ["has", "missing"])                    # the two rules exclude each other
        for kind, r in by_kind.items():
            self.assertEqual(r[kind], [{"type": "query", "key": "v"}])
        self.assertEqual(by_kind["has"]["headers"], [{"key": "Cache-Control", "value": "public, max-age=31536000, immutable"}])
        self.assertEqual(by_kind["missing"]["headers"], [{"key": "Cache-Control", "value": "public, max-age=86400"}])
        self.assertEqual(build_bundle.CACHE_VERSIONED, "public, max-age=31536000, immutable")
        everything = [h for h in json.loads((self.out / "vercel.json").read_text())["headers"] if h["source"] == "/(.*)"]
        self.assertEqual(everything[0]["headers"], [{"key": "X-Robots-Tag", "value": "noindex, nofollow"}])   # unchanged

    def test_nothing_private_is_uploaded(self):
        names = {p.name for p in self.out.rglob("*") if p.is_file()}
        for banned in ("config.json", "app.db", ".env"):
            self.assertNotIn(banned, names)
        self.assertFalse(any(p.suffix == ".log" for p in self.out.rglob("*")))
        self.assertFalse(any("tests" in p.parts for p in self.out.rglob("*")))
        self.assertEqual(build_bundle.team_password((ROOT / "itleads" / "config.py").read_text()), "")   # the code ships no password
        self.assertEqual(build_bundle.team_password((self.out / "itleads" / "config.py").read_text()), "")
        # even if somebody hard-codes one again, the bundle's copy is blanked
        self.assertEqual(build_bundle.team_password(build_bundle.blank_password(
            'x = {"login": {"email": "a@b.co", "password": "hard-coded-secret-1", "show": False}}')), "")
        mine = self.tmp / "config.json"
        mine.write_text(json.dumps({"token": "sheet-token-123456", "app": {"secret_key": "s" * 40, "login": {"password": "a-team-password-1"}}}))
        self.assertEqual(sorted(build_bundle.known_secrets(mine)), sorted(["sheet-token-123456", "s" * 40, "a-team-password-1"]))
        self.assertEqual(build_bundle.known_secrets(self.tmp / "missing.json"), [])

    def test_companies_that_are_not_listed_are_reduced_to_a_count(self):
        raw = (self.out / "snapshot" / "leads.db").read_bytes()
        for secret in (b"Secret Holdings", b"secret-holdings", b"Nowhere Inc", b"1 Hidden Street"):
            self.assertFalse(secret in raw, secret.decode())                    # (not assertNotIn: it would print the file)
        self.assertTrue(b"Acme LLC" in raw and b"1 Main Street" in raw)          # the listed company keeps its details
        db = sqlite3.connect(self.out / "snapshot" / "leads.db")
        rows = dict((r[0], r[1]) for r in db.execute("SELECT state, COUNT(*) FROM companies GROUP BY state"))
        self.assertEqual(rows, {"ready": 1, "held": 2})
        listed = json.loads(db.execute("SELECT raw FROM companies WHERE state='ready'").fetchone()[0])
        self.assertEqual(listed["name"], "Acme LLC")                           # a listed company keeps its details
        self.assertEqual(db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)
        # what the dashboard counts still works on the reduced rows
        store = Store(self.out / "snapshot" / "leads.db")
        self.assertEqual(store.held_breakdown(), {"not_looked_up": 0, "no_website": 1, "missing_contact": 1})
        self.assertEqual(store.counts()["held"], 2)
        store.close()

    def test_it_will_not_build_over_the_program_or_a_folder_that_is_not_a_bundle(self):
        with self.assertRaises(build_bundle.BundleError):
            build_bundle.build(ROOT, source_db=self.db)
        with self.assertRaises(build_bundle.BundleError):
            build_bundle.build(ROOT / "itleads", source_db=self.db)
        mine = self.tmp / "my-files"
        mine.mkdir()
        (mine / "notes.txt").write_text("keep me")
        with self.assertRaises(build_bundle.BundleError):
            build_bundle.build(mine, source_db=self.db)
        self.assertTrue((mine / "notes.txt").exists())
        build_bundle.build(self.out, source_db=self.db)                        # an earlier bundle is replaced

    def test_the_check_refuses_a_folder_that_holds_a_secret(self):
        (self.out / "config.json").write_text("{}")
        with self.assertRaises(build_bundle.BundleError):
            build_bundle.check(self.out, "x")
        (self.out / "config.json").unlink()
        (self.out / "notes.txt").write_text("the password is hunter2-hunter2")
        with self.assertRaises(build_bundle.BundleError):
            build_bundle.check(self.out, ["unrelated-secret", "hunter2-hunter2"])

    def test_without_the_password_in_the_environment_the_entrypoint_refuses_to_start(self):
        import subprocess
        env = {"PATH": "/usr/bin:/bin", "ITLEADS_HOME": str(self.tmp / "home")}
        r = subprocess.run([sys.executable, "-c", "import app"], cwd=self.out, env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ITLEADS_LOGIN_PASSWORD", r.stderr)

    def test_with_the_environment_set_the_entrypoint_serves_the_snapshot(self):
        import subprocess
        code = ("import app; c = app.app.test_client(); import re;"
                "t = re.search(r'csrf-token\" content=\"([^\"]+)\"', c.get('/login').get_data(as_text=True)).group(1);"
                "r = c.post('/login', data={'_csrf': t, 'email': 'team@hybrid.agency', 'password': 'unit-test-pass-1'});"
                "h = c.get('/').get_data(as_text=True);"
                "print(r.status_code, 'read-only copy' in h, c.get('/api/preview').get_json()['total'], c.get('/healthz').status_code)")
        env = {"PATH": "/usr/bin:/bin", "ITLEADS_HOME": str(self.tmp / "home2"), "ITLEADS_LOGIN_PASSWORD": "unit-test-pass-1",
               "ITLEADS_SECRET_KEY": "k" * 48, "ITLEADS_SECURE_COOKIES": "0"}
        r = subprocess.run([sys.executable, "-c", code], cwd=self.out, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        self.assertEqual(r.stdout.split(), ["302", "True", "1", "200"])

    def test_the_published_copy_keeps_working_and_is_https_only(self):
        """Run app.py the way Vercel does (no ITLEADS_SECURE_COOKIES given: the entrypoint sets it) and look at what a visitor
        gets: Secure cookie and HSTS, a failed sign-in that is a normal page, sign-in, preview, downloads, 403 on runs and
        settings, and a sign-out that leaves the other person signed in (instances share no disk there)."""
        import subprocess
        code = r"""
import json, re, app
B = "https://copy.invalid"
def client():
    c = app.app.test_client()
    t = re.search(r'csrf-token" content="([^"]+)"', c.get("/login", base_url=B).get_data(as_text=True)).group(1)
    return c, t
def token(c):
    return re.search(r'csrf-token" content="([^"]+)"', c.get("/", base_url=B).get_data(as_text=True)).group(1)
good = {"email": "team@hybrid.agency", "password": "unit-test-pass-1"}
out = {}
a, t = client()
r = a.get("/login", base_url=B)
out["hsts"] = r.headers.get("Strict-Transport-Security")
out["isolation"] = [r.headers.get("Cross-Origin-Opener-Policy"), r.headers.get("Cross-Origin-Resource-Policy")]
out["login_csp_frames"] = re.search(r"frame-src ([^;]+)", r.headers["Content-Security-Policy"]).group(1)
bad = a.post("/login", base_url=B, data={"_csrf": t, "email": good["email"], "password": "not-the-password-1"})
out["failed"] = [bad.status_code, "do not match" in bad.get_data(as_text=True)]
r = a.post("/login", base_url=B, data=dict(good, _csrf=t))
out["signin"] = r.status_code
out["cookie"] = r.headers.get("Set-Cookie", "")
home = a.get("/", base_url=B)
html = home.get_data(as_text=True)
out["read_only"] = "read-only copy" in html
out["home_csp_frames"] = re.search(r"frame-src ([^;]+)", home.headers["Content-Security-Policy"]).group(1)
v = app.app.asset_version()
out["static"] = [a.get("/static/app.css?v=%d" % v, base_url=B).headers["Cache-Control"],
                 a.get("/static/landing.css", base_url=B).headers["Cache-Control"]]
p = a.get("/api/preview", base_url=B).get_json()
out["preview"] = [p["ok"], p["total"], p["connected"]]
out["downloads"] = [a.get("/download.csv", base_url=B).status_code, a.get("/download.xlsx", base_url=B).status_code]
tok = token(a)
h = {"X-CSRF-Token": tok}
out["run"] = [a.post("/api/run", base_url=B, json={}, headers=h).status_code, a.post("/api/run", base_url=B, json={}, headers=h).get_json()["ok"]]
out["settings"] = [a.get("/settings", base_url=B).status_code,
                   a.post("/api/settings/schedule", base_url=B, json={"time": "08:00"}, headers=h).status_code,
                   a.post("/api/settings/google", base_url=B, json={"url": "x"}, headers=h).status_code]
b, tb = client()
b.post("/login", base_url=B, data=dict(good, _csrf=tb))
out["second_in"] = b.get("/api/status", base_url=B).status_code
lo = a.post("/logout", base_url=B, data={"_csrf": tok})
out["logout"] = [lo.status_code, a.get("/api/status", base_url=B).status_code, b.get("/api/status", base_url=B).status_code]
print(json.dumps(out))
"""
        env = {"PATH": "/usr/bin:/bin", "ITLEADS_HOME": str(self.tmp / "home3"), "ITLEADS_LOGIN_PASSWORD": "unit-test-pass-1",
               "ITLEADS_SECRET_KEY": "k" * 48}
        r = subprocess.run([sys.executable, "-c", code], cwd=self.out, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-1200:])
        got = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertIn("max-age=", got["hsts"])                                               # item 13: HSTS
        for flag in ("Secure", "HttpOnly", "SameSite=Lax"):                                  # item 13: the session cookie
            self.assertIn(flag, got["cookie"])
        self.assertEqual(got["isolation"], ["same-origin", "same-origin"])
        self.assertEqual((got["login_csp_frames"], got["home_csp_frames"]), ("'none'", "'none'"))   # nothing to frame: no Google here
        self.assertEqual(got["failed"], [200, True])
        self.assertEqual((got["signin"], got["read_only"]), (302, True))
        self.assertEqual(got["static"], ["public, max-age=31536000, immutable", "public, max-age=86400"])
        self.assertEqual(got["preview"], [True, 1, False])
        self.assertEqual(got["downloads"], [200, 200])
        self.assertEqual(got["run"], [403, False])
        self.assertEqual(got["settings"], [403, 403, 403])
        self.assertEqual((got["second_in"], got["logout"]), (200, [302, 401, 200]))          # one person out, the other still in


if __name__ == "__main__":
    unittest.main()
