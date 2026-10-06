"""Server behaviour added by the QA fixes: headers per route, the content policy per page, sign-out that leaves the team
signed in, a failed sign-in that is a normal page, short `next`, static caching, wording of the Python-side messages.
Everything here uses invented data. No network, no Google."""
import os
import re
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from itleads import config, enrich, pipeline  # noqa: E402
from itleads.store import Store  # noqa: E402
from itleads.web import appdb, asset_version, auth, create_app, views  # noqa: E402
from tests.test_web import PASSWORD, WebCase  # noqa: E402

SCRIPT_URL = "https://script.google.com/macros/s/AKfycbxINVENTED0000/exec"
SHEET_ID = "1InventedSheetId0000"


def connect_google(link=True):
    config.update(lambda c: c.update(apps_script_url=SCRIPT_URL, sheet_id=SHEET_ID, share_link=link, token="t" * 24))


def directives(csp):
    """'a b; c d' -> {'a': 'b', 'c': 'd'}"""
    out = {}
    for part in csp.split(";"):
        part = part.strip()
        if part:
            name, _, value = part.partition(" ")
            out[name] = value
    return out


def pages_links(html):
    """Every /static/... address with a ?v= that the page links to."""
    return re.findall(r'(?:href|src)="(/static/[^"]+\?v=[^"]+)"', html)


class HeaderTests(WebCase):
    def check_common(self, r, where):
        h = r.headers
        self.assertEqual(h["X-Content-Type-Options"], "nosniff", where)
        self.assertEqual(h["X-Frame-Options"], "DENY", where)
        self.assertEqual(h["Referrer-Policy"], "same-origin", where)
        self.assertEqual(h["Cross-Origin-Opener-Policy"], "same-origin", where)
        self.assertEqual(h["Cross-Origin-Resource-Policy"], "same-origin", where)
        for feature in ("camera", "microphone", "geolocation", "payment", "usb"):
            self.assertIn(feature + "=()", h["Permissions-Policy"], where)
        self.assertNotIn("interest-cohort", h["Permissions-Policy"], where)      # FLoC is gone: the name protects nothing
        d = directives(h["Content-Security-Policy"])
        self.assertEqual(d["frame-ancestors"], "'none'", where)
        self.assertEqual(d["object-src"], "'none'", where)
        self.assertEqual(d["base-uri"], "'none'", where)

    def test_every_route_sends_the_same_security_headers_and_pages_are_not_cached(self):
        admin = self.user_client("admin@example.com")
        member = self.user_client("member@example.com")
        anon = self.app.test_client()
        self.seed()
        routes = [(anon, "/"), (anon, "/login"), (anon, "/healthz"), (anon, "/nope"), (anon, "/signup"), (anon, "/api/status"),
                  (admin, "/"), (admin, "/settings"), (admin, "/fragment/stats"), (admin, "/api/status"), (admin, "/api/preview"),
                  (admin, "/download.csv"), (admin, "/download.xlsx"), (member, "/settings"), (member, "/nope")]
        for client, path in routes:
            r = client.get(path)
            self.check_common(r, path)
            self.assertEqual(r.headers["Cache-Control"], "no-store", path)             # pages and data are never kept
            r.close()

    def test_the_policy_loads_nothing_from_anywhere_else(self):
        d = directives(self.app.test_client().get("/login").headers["Content-Security-Policy"])
        self.assertEqual(d["default-src"], "'self'")
        self.assertEqual(d["img-src"], "'self'")                  # no data: images: none are used
        for name in ("style-src", "script-src", "font-src", "connect-src"):
            self.assertEqual(d[name], "'self'", name)
        self.assertEqual(d["form-action"], "'self'")
        self.assertEqual(d["frame-src"], "'none'")
        csp = self.app.test_client().get("/login").headers["Content-Security-Policy"]
        for bad in ("unsafe-inline", "unsafe-eval", "data:", "*", "http:"):
            self.assertNotIn(bad, csp)

    def test_hsts_only_when_served_over_https(self):
        self.assertNotIn("Strict-Transport-Security", self.app.test_client().get("/login").headers)
        with mock.patch.dict("os.environ", {"ITLEADS_SECURE_COOKIES": "1"}):
            app = create_app(runner=self.fake_runner)
            self.managers.append(app.manager)
            h = app.test_client().get("/login").headers
            self.assertIn("max-age=31536000", h["Strict-Transport-Security"])
            self.check_common(app.test_client().get("/login"), "https mode")

    def test_the_session_cookie_is_not_sent_again_on_every_request(self):
        self.assertFalse(self.app.config["SESSION_REFRESH_EACH_REQUEST"])
        c = self.user_client()
        c.get("/")
        cookie = c.get_cookie("itleads").value
        for path in ("/api/status", "/fragment/stats", "/api/status", "/", "/settings"):      # the page's 15 second poll and the pages
            r = c.get(path)
            self.assertNotIn("Set-Cookie", r.headers, path)
        self.assertEqual(c.get_cookie("itleads").value, cookie)


class ContentPolicyPerPageTests(WebCase):
    GOOGLE = "https://docs.google.com"

    def frame_src(self, client, path="/"):
        r = client.get(path)
        value = directives(r.headers["Content-Security-Policy"])["frame-src"]
        r.close()
        return value

    def test_nothing_may_be_framed_by_default(self):
        admin = self.user_client("admin@example.com")
        member = self.user_client("member@example.com")
        anon = self.app.test_client()
        for client, path in ((anon, "/"), (anon, "/login"), (anon, "/nope"), (admin, "/"), (admin, "/settings"),
                             (admin, "/fragment/stats"), (admin, "/api/preview"), (member, "/settings"), (member, "/")):
            self.assertEqual(self.frame_src(client, path), "'none'", path)

    def test_google_is_framed_only_on_the_dashboard_when_connected_and_shared_by_link(self):
        admin = self.user_client("admin@example.com")
        connect_google(link=True)
        self.assertEqual(self.frame_src(admin, "/"), self.GOOGLE)
        for path in ("/settings", "/fragment/stats", "/api/preview", "/api/status", "/nope", "/login"):
            self.assertEqual(self.frame_src(admin, path), "'none'", path)           # no other page needs the frame
        self.assertEqual(self.frame_src(self.app.test_client(), "/"), "'none'")     # signed out: the start page, no frame
        self.assertEqual(self.frame_src(self.app.test_client(), "/login"), "'none'")

    def test_google_is_not_framed_when_the_sheet_is_private_or_not_connected(self):
        admin = self.user_client("admin@example.com")
        self.assertEqual(self.frame_src(admin, "/"), "'none'")                      # not connected
        connect_google(link=False)
        self.assertEqual(self.frame_src(admin, "/"), "'none'")                      # connected, but the preview is a plain table
        config.update(lambda c: c.update(apps_script_url="", share_link=True))
        self.assertEqual(self.frame_src(admin, "/"), "'none'")                      # a sheet id alone is not a connection

    def test_the_wider_policy_is_the_only_difference(self):
        admin = self.user_client("admin@example.com")
        plain = admin.get("/").headers["Content-Security-Policy"]
        connect_google()
        wide = admin.get("/").headers["Content-Security-Policy"]
        self.assertEqual(wide.replace("frame-src " + self.GOOGLE, "frame-src 'none'"), plain)

    def test_the_dashboard_still_embeds_the_sheet_the_page_script_asks_for(self):
        admin = self.user_client("admin@example.com")
        connect_google()
        info = admin.get("/api/preview").get_json()
        self.assertTrue(info["connected"] and info["linked"])
        self.assertTrue(info["embed_url"].startswith(self.GOOGLE + "/"))            # what the script puts in the frame is allowed


class SignOutTests(WebCase):
    def sign_in_two(self):
        self.make_user("team@example.com")
        a, b = self.app.test_client(), self.app.test_client()
        for c in (a, b):
            self.assertEqual(self.login(c, "team@example.com").status_code, 302)
        return a, b

    def sign_out(self, c):
        t = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/").get_data(as_text=True)).group(1)
        return c.post("/logout", data={"_csrf": t})

    def test_signing_out_ends_only_this_browsers_session(self):
        a, b = self.sign_in_two()
        before = appdb.user_by_email("team@example.com")["session_v"]
        with mock.patch.object(appdb, "bump_session") as bump:
            r = self.sign_out(a)
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/login"))
        bump.assert_not_called()
        self.assertEqual(appdb.user_by_email("team@example.com")["session_v"], before)    # the shared account is untouched
        self.assertIsNone(a.get_cookie("itleads"))                                         # this browser's cookie is gone
        self.assertEqual(a.get("/settings").status_code, 302)                              # ... and it is signed out
        self.assertEqual(b.get("/settings").status_code, 200)                              # the other person is still in
        self.assertEqual(b.get("/api/status").status_code, 200)                            # their page's poll keeps working
        self.assertEqual(b.get("/download.csv").status_code, 200)

    def test_the_other_person_can_keep_working_after_the_first_signs_out_and_back_in(self):
        a, b = self.sign_in_two()
        self.sign_out(a)
        self.assertEqual(self.login(a, "team@example.com").status_code, 302)
        self.assertEqual(a.get("/settings").status_code, 200)
        self.assertEqual(b.get("/settings").status_code, 200)

    def test_changing_the_team_password_still_signs_everyone_out(self):
        a, b = self.sign_in_two()
        appdb.set_password(appdb.user_by_email("team@example.com")["id"], auth.hash_password("a different long password"))
        self.assertEqual(a.get("/settings").status_code, 302)
        self.assertEqual(b.get("/settings").status_code, 302)

    def test_signing_out_twice_or_when_signed_out_is_harmless(self):
        a, _ = self.sign_in_two()
        t = re.search(r'name="csrf-token" content="([^"]+)"', a.get("/").get_data(as_text=True)).group(1)
        self.assertEqual(a.post("/logout", data={"_csrf": t}).status_code, 302)
        anon = self.app.test_client()
        t2 = re.search(r'name="csrf-token" content="([^"]+)"', anon.get("/login").get_data(as_text=True)).group(1)
        self.assertEqual(anon.post("/logout", data={"_csrf": t2}).status_code, 302)

    def test_the_code_says_what_signing_out_does_not_do(self):
        doc = auth.logout_user.__doc__
        self.assertIn("THIS browser", doc)
        self.assertIn("NOT bumped", doc)


class FailedSignInTests(WebCase):
    def post_login(self, c, email, password, **extra):
        t = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
        return c.post("/login", data=dict({"_csrf": t, "email": email, "password": password}, **extra))

    def test_a_wrong_password_is_the_form_again_with_status_200(self):
        self.make_user()
        c = self.app.test_client()
        r = self.post_login(c, "admin@example.com", "not the password", next="/settings")
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("WWW-Authenticate", r.headers)
        self.assertIn('role="alert"', html)
        self.assertIn("That email and password do not match.", html)
        self.assertIn("Check both and try again, or ask your admin for the team login.", html)
        self.assertIn('value="admin@example.com"', html)                              # the email is kept
        self.assertRegex(html, r'name="next"[^>]*value="/settings"|value="/settings"[^>]*name="next"')   # and so is where to go
        self.assertIn('name="password"', html)
        self.assertNotIn("not the password", html)                                    # the password never comes back
        self.assertEqual(c.get("/settings").status_code, 302)                         # and nobody is signed in

    def test_an_unknown_email_looks_exactly_the_same(self):
        self.make_user()
        known = self.post_login(self.app.test_client(), "admin@example.com", "wrong wrong wrong")
        unknown = self.post_login(self.app.test_client(), "nobody@example.com", "wrong wrong wrong")
        self.assertEqual((known.status_code, unknown.status_code), (200, 200))
        strip = lambda h, e: re.sub(r'name="csrf-token" content="[^"]+"|name="_csrf" value="[^"]+"', "", h.replace(e, ""))  # noqa: E731
        self.assertEqual(strip(known.get_data(as_text=True), "admin@example.com"),
                         strip(unknown.get_data(as_text=True), "nobody@example.com"))

    def test_the_lock_page_is_429_and_says_how_long(self):
        self.make_user()
        c = self.app.test_client()
        for _ in range(auth.LIMIT_PAIR):
            self.assertEqual(self.post_login(c, "admin@example.com", "nope nope nope").status_code, 200)
        r = self.post_login(c, "admin@example.com", PASSWORD)                         # even the right one waits
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 429)
        self.assertIn('role="alert"', html)
        self.assertIn("Too many sign-in attempts. Wait up to 15 minutes, then try again.", html)
        self.assertIn("If you are still locked out, ask your admin.", html)
        self.assertEqual(auth.WINDOW, 900)                                            # the 15 minutes of the message are the real window

    def test_a_busy_server_answers_on_the_form_with_503(self):
        self.make_user()
        from contextlib import contextmanager

        @contextmanager
        def full():
            raise auth.Busy()
            yield
        c = self.app.test_client()
        t = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
        with mock.patch.object(auth, "_hashing", full):
            r = c.post("/login", data={"_csrf": t, "email": "admin@example.com", "password": PASSWORD})
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 503)
        self.assertIn("Several people are signing in at once. Try again in a few seconds.", html)
        self.assertIn('name="password"', html)                                        # still the form, not a dead end
        self.assertIn('value="admin@example.com"', html)

    def test_the_busy_page_is_the_safety_net_for_any_other_route(self):
        @self.app.get("/_busy")
        def busy():
            raise auth.Busy()
        r = self.app.test_client().get("/_busy")
        self.assertEqual(r.status_code, 503)
        self.assertIn("Sign-in is busy", r.get_data(as_text=True))

    def test_a_right_password_still_signs_in(self):
        self.make_user()
        self.assertEqual(self.post_login(self.app.test_client(), "admin@example.com", PASSWORD).status_code, 302)


class NextAddressTests(WebCase):
    def hidden_next(self, html):
        tag = re.search(r'<input[^>]*name="next"[^>]*>', html).group(0)
        return re.search(r'value="([^"]*)"', tag).group(1)

    def test_a_very_long_next_is_cut_before_it_is_repeated_into_the_page(self):
        r = self.app.test_client().get("/login?next=/" + "a" * 70000)
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertLessEqual(len(self.hidden_next(html)), 512)
        self.assertLess(len(html), 30_000)                                              # it was 73 KB
        self.assertTrue(self.hidden_next(html).startswith("/aaa"))

    def test_a_short_next_is_kept_whole(self):
        html = self.app.test_client().get("/login?next=/settings%3Fx%3D1").get_data(as_text=True)
        self.assertEqual(self.hidden_next(html), "/settings?x=1")

    def test_exactly_512_characters_pass_and_513_are_cut(self):
        for n, want in ((512, 512), (513, 512)):
            html = self.app.test_client().get("/login?next=/" + "b" * (n - 1)).get_data(as_text=True)
            self.assertEqual(len(self.hidden_next(html)), want, n)

    def test_the_posted_next_is_cut_too_and_still_only_ever_a_local_path(self):
        self.make_user()
        c = self.app.test_client()
        r = self.login(c, "admin@example.com", nxt="/settings?q=" + "z" * 5000)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(len(r.headers["Location"]), 512)
        self.assertTrue(r.headers["Location"].startswith("/settings?q=zzz"))
        r = self.login(self.app.test_client(), "admin@example.com", nxt="//evil.example/" + "y" * 5000)
        self.assertEqual(r.headers["Location"], "/")                                    # unchanged rule: nothing leaves the site


class StaticCacheTests(WebCase):
    def get(self, path, **kw):
        c = kw.pop("client", None) or self.app.test_client()
        r = c.get(path, **kw)
        r.get_data()
        r.close()
        return r

    def setUp(self):
        super().setUp()
        self.v = self.app.asset_version()

    def test_addresses_with_the_current_version_are_kept_for_a_year(self):
        for name in ("app.css", "landing.css", "app.js"):
            r = self.get(f"/static/{name}?v={self.v}")
            self.assertEqual(r.status_code, 200, name)
            self.assertEqual(r.headers["Cache-Control"], "public, max-age=31536000, immutable", name)

    def test_files_without_a_version_are_kept_for_a_day(self):
        for path in ("/static/app.css", "/static/app.js", "/static/hybrid-logo.png", "/static/mark.svg", "/static/favicon-32.png",
                     "/static/fonts/inter-latin.woff2", "/static/site.webmanifest", "/favicon.ico"):
            r = self.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertEqual(r.headers["Cache-Control"], "public, max-age=86400", path)
            self.assertNotIn("immutable", r.headers["Cache-Control"], path)

    def test_a_version_that_is_not_the_current_one_does_not_get_the_long_life(self):
        for v in ("1", "0", "abc", str(self.v + 1), ""):
            r = self.get(f"/static/app.css?v={v}")
            self.assertEqual(r.headers["Cache-Control"], "public, max-age=86400", v)
        for name in ("hybrid-logo.png", "fonts/inter-latin.woff2", "favicon.svg"):    # not part of the version: never the long life
            r = self.get(f"/static/{name}?v={self.v}")
            self.assertEqual(r.headers["Cache-Control"], "public, max-age=86400", name)

    def test_a_missing_file_is_not_remembered(self):
        r = self.get("/static/nothing-here.css")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.headers["Cache-Control"], "no-store")

    def test_a_conditional_request_gets_a_304_with_the_same_caching(self):
        c = self.app.test_client()
        for path, want in ((f"/static/app.css?v={self.v}", "public, max-age=31536000, immutable"),
                           ("/static/hybrid-logo.png", "public, max-age=86400")):
            first = self.get(path, client=c)
            again = self.get(path, client=c, headers={"If-None-Match": first.headers["ETag"]})
            self.assertEqual(again.status_code, 304, path)
            self.assertEqual(again.headers["Cache-Control"], want, path)

    def test_the_pages_link_the_current_version_and_nothing_is_preloaded(self):
        admin = self.user_client()
        for client, path in ((self.app.test_client(), "/"), (self.app.test_client(), "/login"), (admin, "/"), (admin, "/settings"),
                             (self.app.test_client(), "/nope")):
            html = client.get(path).get_data(as_text=True)
            links = pages_links(html)
            self.assertTrue(any("app.css" in x for x in links) and any("app.js" in x for x in links), path)
            for link in links:
                self.assertTrue(link.endswith("?v=%d" % self.v), (path, link))
                r = self.get(link)
                self.assertEqual(r.headers["Cache-Control"], "public, max-age=31536000, immutable", link)
            self.assertNotIn('rel="preload"', html, path)                               # the warm-cache warning came from here
        self.assertTrue(any("landing.css" in x for x in pages_links(self.app.test_client().get("/").get_data(as_text=True))))

    def test_the_version_follows_the_content_not_the_date_of_the_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp, "a.css"), Path(tmp, "b.js")
            a.write_text("body{}")
            b.write_text("1")
            v1 = asset_version([a, b])
            os.utime(a, (1540000000, 1540000000))                                       # every file with the same old date (Vercel)
            os.utime(b, (1540000000, 1540000000))
            self.assertEqual(asset_version([a, b]), v1)                                 # a new date alone changes nothing
            a.write_text("body{color:red}")
            os.utime(a, (1540000000, 1540000000))                                       # new content, the very same date
            v2 = asset_version([a, b])
            self.assertNotEqual(v2, v1)
            self.assertEqual(asset_version([a, b]), v2)                                 # stable until the content changes again
            self.assertEqual(asset_version([a, Path(tmp, "missing.css")]), asset_version([a, Path(tmp, "missing.css")]))
            self.assertIsInstance(v2, int)

    def test_the_real_files_are_all_part_of_the_version(self):
        static = ROOT / "itleads" / "web" / "static"
        for name in ("app.css", "landing.css", "app.js"):
            self.assertTrue((static / name).exists(), name)
        self.assertNotEqual(asset_version([static / "app.css"]), asset_version([static / "app.css", static / "landing.css"]))


class WordingTests(WebCase):
    def test_the_host_guard_names_the_port_the_request_came_in_on(self):
        c = self.app.test_client()
        for host, want in (("evil.example:8803", "http://127.0.0.1:8803"), ("evil.example:9000", "http://127.0.0.1:9000"),
                           ("evil.example:8765", "http://127.0.0.1:8765")):
            r = c.get("/login", headers={"Host": host})
            html = r.get_data(as_text=True)
            self.assertEqual(r.status_code, 400, host)
            self.assertIn(want + ".", html, host)
            self.assertIn("Wrong address", html, host)
            self.assertIn("Hybrid Leads", html, host)
        r = c.get("/login", headers={"Host": "evil.example"}, environ_overrides={"SERVER_PORT": "8803"})   # no port in the name
        self.assertIn("http://127.0.0.1:8803.", r.get_data(as_text=True))
        r = c.get("/login", headers={"Host": "evil.example:notaport"})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("8765", r.get_data(as_text=True))                             # no port is written in the code any more
        self.assertNotIn("8765", (ROOT / "itleads" / "web" / "__init__.py").read_text())

    def test_an_expired_session_has_its_own_title_for_pages_and_a_plain_message_for_the_api(self):
        c = self.user_client()
        r = c.post("/logout")                                                           # no token: what a stale page sends
        self.assertEqual(r.status_code, 400)
        html = r.get_data(as_text=True)
        self.assertIn("Session expired", html)
        self.assertNotIn("That did not work", html)
        self.assertIn("Your session expired. Reload the page and try again.", html)
        r = c.post("/api/run", json={})
        self.assertEqual(r.get_json(), {"ok": False, "error": auth.SESSION_EXPIRED})

    def test_other_400s_keep_the_general_title(self):
        admin = self.user_client()
        r = self.api(admin, "/api/settings/schedule", None)
        self.assertEqual(r.status_code, 400)                                           # JSON expected, none sent
        self.assertEqual(r.get_json()["error"], "Send JSON.")

    def test_only_admins_page_says_what_to_do(self):
        self.user_client("admin@example.com")
        member = self.user_client("member@example.com")
        html = member.get("/settings").get_data(as_text=True)
        self.assertIn("Admins only", html)
        self.assertIn("Only admins can open this page. Ask an admin for access.", html)
        self.assertNotIn("Your account cannot open", html)

    def test_the_error_page_for_a_server_fault_says_who_to_tell(self):
        c = self.user_client()
        self.app.testing = False
        self.app.config["PROPAGATE_EXCEPTIONS"] = False
        with mock.patch.object(views, "dashboard_context", side_effect=RuntimeError("boom")):
            with self.assertLogs(self.app.logger, level="ERROR"):
                r = c.get("/")
        self.assertEqual(r.status_code, 500)
        html = r.get_data(as_text=True)
        self.assertIn("The error was recorded. Try again in a moment. If it keeps happening, tell your admin.", html)
        self.assertNotIn("boom", html)

    def test_settings_messages(self):
        admin = self.user_client()
        r = self.api(admin, "/api/settings/google", {"url": "https://example.com/nope"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("It should look like https://script.google.com/macros/s/", r.get_json()["error"])
        self.assertNotIn("It looks like", r.get_json()["error"])
        r = self.api(admin, "/api/settings/rules", {"require": ["email"], "sources": {"tx": False, "ct": False}})
        self.assertEqual((r.status_code, r.get_json()["error"]), (400, "Turn on at least one registry."))
        r = self.api(admin, "/api/settings/schedule", {"time": "later"})
        self.assertEqual(r.get_json()["error"], "Use a time like 07:30.")

    def test_run_history_says_added_and_names_registries(self):
        self.assertEqual(views._run_lines({"status": "ok", "added": 3, "pushed": 2, "held": 4}),
                         ("3 added", "2 sent to the sheet · 4 held back"))
        self.assertEqual(views._run_lines({"status": "partial", "pushed": 5, "held": 0})[0], "5 added")      # an older entry without "added"
        self.assertEqual(views._run_lines({"status": "failed"}), ("Nothing added", "every registry failed"))
        self.assertEqual(views._run_lines({"status": "offline"}), ("Nothing changed", "no internet connection"))
        c = self.user_client()
        store = Store(config.DATA / "leads.db")
        now = datetime.now().isoformat()
        store.log_run(now, now, {"status": "ok", "added": 22, "held": 1, "pushed": 22, "sources": {"ct": 22}, "errors": [], "trigger": "schedule"})
        store.close()
        page = c.get("/").get_data(as_text=True)
        self.assertIn("22 added", page)
        self.assertNotIn("22 new", page)

    def test_no_python_message_uses_british_spelling_or_the_old_terms(self):
        files = [ROOT / "itleads" / "web" / n for n in ("__init__.py", "views.py", "auth.py", "appdb.py", "jobs.py", "export.py")]
        files.append(ROOT / "itleads" / "pipeline.py")
        for f in files:
            text = f.read_text(encoding="utf-8")
            for bad in ("licence", "honour", "colour", "organis", "ticked", "untick", "every source failed", "qualified so far",
                        "It looks like", "Keep at least one source", "this app understands", "Too many attempts"):
                self.assertNotIn(bad, text, f"{f.name}: {bad}")


class PipelineWordingTests(WebCase):
    def test_progress_says_how_many_were_added_so_far(self):
        s = Store(config.DATA / "leads.db")
        s.add({"id": "tx:Acme LLC", "source": "tx", "name": "Acme LLC", "trade_name": "", "state": "TX", "city": "Austin",
               "address": "1 Main St", "zip": "78701", "registered": "2026-10-02", "industry": "Software", "individual": False,
               "emails": [], "people": []}, date.today())
        notes, lines = [], []
        found = dict(enrich.empty_result(), website="https://acme.io", domain="acme.io")
        try:
            with mock.patch.object(pipeline.enrich, "enrich_one", return_value=found), \
                    mock.patch.object(pipeline.enrich, "qualify", return_value=("ready", [])):
                stats = pipeline.enrich_pending(s, config.load(), date.today(), lines.append,
                                                progress=lambda stage, done, total, note="": notes.append((stage, done, total, note)))
        finally:
            s.close()
        self.assertEqual(stats["ready"], 1)
        self.assertEqual(notes, [("lookup", 1, 1, "1 added so far")])
        self.assertTrue(any("added so far: 1" in x for x in lines), lines)
        self.assertFalse(any("qualified" in n[3] for n in notes))

    def test_the_run_summary_line_says_added(self):
        lines = []
        with mock.patch.object(pipeline, "online", return_value=True), mock.patch.object(pipeline.sources, "build", return_value=[]):
            summary = pipeline.run(config.load(), dry_run=True, log=lines.append)
        self.assertEqual(summary["status"], "ok")
        self.assertRegex(lines[-1], r"^Done in \d+s: 0 added, 0 sent to the sheet, 0 held back, status ok\.$")


if __name__ == "__main__":
    unittest.main()


class StoredContactCleaningTests(unittest.TestCase):
    """Waiting (not yet sent) companies are cleaned by today's checks before they reach the sheet."""

    def setUp(self):
        import tempfile
        from datetime import date
        from itleads import config, pipeline
        from itleads.store import Store
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "leads.db"
        self.store, self.today, self.pipeline, self.cfg = Store(self.path), date(2026, 10, 6), pipeline, config.load() if False else {
            "require": ["website", "email", "phone"], "require_it_signal": True, "skip_established": False, "contact_either": False}
        rec = {"id": "tx:1", "source": "tx", "name": "Wexford Cloud LLC", "trade_name": "", "state": "TX", "city": "Austin", "address": "1 Main St",
               "zip": "78701", "registered": "2026-10-02", "industry": "Software", "individual": False, "emails": [],
               "people": [{"name": "Jordan Reyes", "title": "Director"}]}
        self.rec = rec
        self.store.add(rec, self.today)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def enrich(self, **over):
        e = {"website": "https://wexfordcloud.io", "email": "hello@wexfordcloud.io", "email_from": "company website", "emails": [],
             "phone": "5122100100", "phone_from": "company website", "phones": ["5122100100"], "why": ["full legal name on the site"],
             "created": "2026-09-30", "domain": "wexfordcloud.io", "it": {"level": "strong"},
             "contact": {"name": "Jobs Pipeline", "title": "Owner summary: 2 new leads today", "email": "", "linkedin": ""},
             "linkedin": "", "linkedin_company": ""}
        e.update(over)
        return e

    def test_a_non_person_contact_is_replaced_and_the_company_stays_waiting(self):
        self.store.record_attempt("tx:1", self.enrich(), "ready", self.today)
        self.pipeline.requalify(self.store, self.cfg, self.today)
        item = self.store.ready()[0]
        self.assertNotEqual(item["enrich"]["contact"].get("name"), "Jobs Pipeline")
        self.assertEqual(item["state"], "ready")

    def test_a_placeholder_phone_sends_the_company_back_to_held(self):
        self.store.record_attempt("tx:1", self.enrich(phone="8605550341", phones=["8605550341"]), "ready", self.today)
        self.pipeline.requalify(self.store, self.cfg, self.today)
        self.assertEqual(self.store.ready(), [])
        self.assertEqual(self.store.counts()["held"], 1)


class CliWordingAndSecretsTests(WebCase):
    """The command line: the team password is never printed, one headline, 'registries', honest messages. Invented data only."""
    KNOWN = "Invented-Pass-4711-xyz"

    def run_cli(self, *argv):
        import contextlib
        import io
        from itleads import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch("os._exit", side_effect=SystemExit), \
                mock.patch.object(cli.service, "installed", return_value=False):
            try:
                code = cli.main(list(argv))
            except SystemExit as e:
                code = e.code if e.code is not None else 0
        return code, buf.getvalue()

    def set_password(self, pw):
        config.update(lambda c: c["app"]["login"].update(email="crew@agency.test", password=pw))

    def test_status_never_prints_the_team_password(self):
        self.set_password(self.KNOWN)
        code, text = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertNotIn(self.KNOWN, text)
        self.assertIn("crew@agency.test", text)
        self.assertIn("password set", text)
        with mock.patch.dict("os.environ", {"ITLEADS_LOGIN_PASSWORD": "Other-Invented-Pass-9"}):    # the environment one is hidden too
            self.assertNotIn("Other-Invented-Pass-9", self.run_cli("status")[1])

    def test_status_says_when_no_password_is_set(self):
        self.set_password("")
        text = self.run_cli("status")[1]
        self.assertIn("no password set", text)
        self.assertIn("set-login --generate", text)

    def test_no_command_prints_the_password_or_the_session_secret(self):
        self.set_password(self.KNOWN)
        secret = config.load()["app"].get("secret_key") or "no-secret-yet"
        for argv in (("status",), ("doctor",), ("-h",), ("run", "-h"), ("export",), ("reset-password", "crew@agency.test")):
            with mock.patch.object(sys.modules["itleads.cli"].sources, "build", return_value=[]), \
                    mock.patch("getpass.getpass", return_value="x"), mock.patch.object(sys.modules["itleads.cli"], "wait_until_up", return_value=False):
                text = self.run_cli(*argv)[1]
            self.assertNotIn(self.KNOWN, text, argv)
            self.assertNotIn(secret, text, argv)

    def test_the_banner_and_help_carry_the_headline_and_promise_no_google(self):
        self.set_password(self.KNOWN)
        status = self.run_cli("status")[1]
        self.assertIn("New IT company filings, checked every day.", status)
        self.assertNotIn("Google Sheet", status)
        code, text = self.run_cli("-h")
        self.assertEqual(code, 0)
        flat = " ".join(text.split())
        self.assertIn("New IT company filings, checked every day.", flat)
        self.assertNotIn("Google Sheet", flat)
        self.assertIn("check every registry", flat)
        self.assertNotIn("every source", flat)

    def test_registries_not_sources_in_the_command_line(self):
        self.set_password(self.KNOWN)
        text = self.run_cli("status")[1]
        self.assertIn("Registries", text)
        self.assertNotIn("Sources", text)
        for flag in ("--registry", "--source"):                                    # the old option keeps working
            code, out = self.run_cli("run", flag, "nowhere")
            self.assertEqual(code, 2)
            self.assertIn("Unknown registry nowhere", out)
            self.assertNotIn("Unknown source", out)
        self.assertIn("--registry", " ".join(self.run_cli("run", "-h")[1].split()))

    def test_both_run_options_pick_the_same_registries(self):
        from itleads import cli
        seen = []
        for flag in ("--registry", "--source"):
            with mock.patch.object(cli.pipeline, "run", side_effect=lambda cfg, **kw: seen.append(kw["only"]) or {"status": "ok"}), \
                    mock.patch.object(cli.pipeline, "run_lock"):
                self.assertEqual(self.run_cli("run", flag, "ct,tx")[0], 0)
        self.assertEqual(seen, [["ct", "tx"], ["ct", "tx"]])

    def test_the_log_line_of_a_run_says_registries(self):
        lines = []
        fake = mock.Mock(key="ct")
        with mock.patch.object(pipeline, "online", return_value=True), mock.patch.object(pipeline.sources, "build", return_value=[fake]), \
                mock.patch.object(pipeline, "fetch_all", return_value={"ct": ([], None)}), \
                mock.patch.object(pipeline, "source_window", return_value=(date.today(), date.today())):
            try:
                pipeline.run(config.load(), dry_run=True, log=lines.append)
            except Exception:
                pass                                                                # only the first lines matter here
        first = [x for x in lines if "registries:" in x or "sources:" in x]
        self.assertTrue(first and "registries: ct" in first[0], lines)

    def test_doctor_says_how_to_make_the_first_login(self):
        from itleads import cli
        with mock.patch.object(cli.sources, "build", return_value=[]), mock.patch.object(cli.appdb, "count_users", return_value=0), \
                mock.patch.object(cli, "wait_until_up", return_value=False):
            text = self.run_cli("doctor")[1]
        self.assertIn("none yet: run ./it-leads set-login --generate", text)
        self.assertNotIn("create the first one", text)

    def test_the_default_csv_name_matches_the_download_in_the_app(self):
        code, text = self.run_cli("export")
        self.assertEqual(code, 0)
        name = f"new-it-companies-{date.today().isoformat()}.csv"
        self.assertTrue((config.ROOT / name).exists(), text)
        c = self.user_client()
        self.assertIn(f'filename="{name}"', c.get("/download.csv").headers["Content-Disposition"])

    def test_the_deploy_script_points_at_a_section_that_exists(self):
        script = (ROOT / "deploy" / "vercel" / "deploy.sh").read_text()
        self.assertNotIn("README.md", script)
        self.assertIn("docs/guide.md", script)
        self.assertIn("## Publish a read-only copy on Vercel", (ROOT / "docs" / "guide.md").read_text())


class UpdatedTimeTests(WebCase):
    def test_updated_is_when_the_run_finished_not_when_it_started(self):
        c = self.user_client()
        s = Store(config.DATA / "leads.db")
        s.log_run(datetime(2026, 10, 5, 17, 23).isoformat(), datetime(2026, 10, 5, 18, 3).isoformat(),
                  {"status": "ok", "added": 1, "pushed": 1, "held": 0, "seconds": 2400, "sources": {"ct": 1}, "errors": [], "trigger": "schedule"})
        s.close()
        self.assertEqual(c.get("/api/status").get_json()["updated"], "5 Oct 2026, 18:03")
        page = c.get("/").get_data(as_text=True)
        self.assertRegex(page, r"Updated[^<]*5 Oct 2026, 18:03")
