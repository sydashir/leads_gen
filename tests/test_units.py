"""Offline unit tests: no network, no Google."""
import json
import plistlib
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from itleads import config, enrich, pipeline, service, sheet, util  # noqa: E402
from itleads.enrich import contacts, itclass, qualify, rdap, website  # noqa: E402
from itleads.sources import registries  # noqa: E402
from itleads.sources.base import Socrata, email_kind, iso, looks_like_company  # noqa: E402
from itleads.store import RETRY_DAYS, Store, row_hash  # noqa: E402


class UtilTests(unittest.TestCase):
    def test_time_zone_comes_from_the_environment_when_there_is_one(self):
        with mock.patch.dict("os.environ", {"TZ": "Asia/Karachi"}):
            self.assertEqual(util.tz_name(), "Asia/Karachi")
        with mock.patch.dict("os.environ", {"TZ": ":America/Chicago"}):
            self.assertEqual(util.tz_name(), "America/Chicago")
        with mock.patch.dict("os.environ", {"TZ": "UTC"}):
            self.assertEqual(util.tz_name(), "UTC")
        with mock.patch.dict("os.environ", {"TZ": "PKT-5"}), mock.patch("os.readlink", side_effect=OSError):
            self.assertEqual(util.tz_name(), "UTC")                                  # not a zone name: falls back

    def test_slugs(self):
        self.assertEqual(util.slugs("Tiny Fix LLC"), ["tinyfix", "tiny-fix"])
        self.assertIn("willow-heron", util.slugs("WILLOW HERON, LLC"))
        self.assertIn("acme", util.slugs("Acme Software Solutions Inc"))

    def test_tidy(self):
        self.assertEqual(util.tidy_name("NOYAM LLC"), "Noyam LLC")
        self.assertEqual(util.tidy_name("Mixed Case Inc"), "Mixed Case Inc")
        self.assertEqual(util.tidy_address("200 NW FIRST ST # 212"), "200 NW First St # 212")
        self.assertEqual(util.tidy_address("100 MAIN AVE N # 414"), "100 Main Ave N # 414")
        self.assertEqual(util.compose_address("500 9TH AVE", "SEATTLE", "wa", "98104-2287"),
                         "500 9th Ave, Seattle, WA 98104")

    def test_phone(self):
        self.assertEqual(util.fmt_phone("2065550123"), "(206) 555-0123")
        self.assertEqual(util.norm_phone("+1 (206) 555-0123"), "2065550123")
        self.assertEqual(util.norm_phone("12345"), "")

    def test_state_from_zip(self):
        for z, st in (("84048", "UT"), ("95816", "CA"), ("19801", "DE"), ("98101-2287", "WA"), ("78701", "TX"),
                      ("10001", "NY"), ("06880", "CT"), ("02139", "MA"), ("", "")):
            self.assertEqual(util.state_from_zip(z), st, z)


class FetchGuardTests(unittest.TestCase):
    """A company's website must never be able to point this program at a machine inside the network."""

    @staticmethod
    def resolves(mapping):
        def fake(host, *a, **k):
            ips = mapping.get(host)
            if ips is None:
                raise socket.gaierror(socket.EAI_NONAME, "no such host")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]
        return mock.patch.object(util, "_orig_getaddrinfo", fake)

    @staticmethod
    def session(*responses):
        sess = mock.MagicMock()
        sess.__enter__.return_value = sess
        sess.get.side_effect = list(responses)
        return mock.patch.object(util, "_guard_session", return_value=sess), sess

    class Local:
        """A tiny web server on this machine that counts how often it is asked, and can drip its answer."""
        def __init__(self, drip=False):
            import socketserver
            outer = self
            self.hits, self.drip = 0, drip

            class H(socketserver.BaseRequestHandler):
                def handle(self):
                    outer.hits += 1
                    try:
                        self.request.recv(4096)
                        self.request.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: text/html\r\nContent-Length: 100000\r\n\r\n")
                        for _ in range(100 if outer.drip else 0):
                            self.request.sendall(b"x")
                            time.sleep(0.3)
                    except OSError:
                        pass
            self.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
            self.srv.daemon_threads = True
            self.port = self.srv.server_address[1]
            import threading
            threading.Thread(target=self.srv.serve_forever, daemon=True).start()

        def close(self):
            self.srv.shutdown()
            self.srv.server_close()

    def test_only_public_addresses_count(self):
        with self.resolves({"ok.example": ["93.184.216.34"], "lan.example": ["192.168.1.5"], "mixed.example": ["93.184.216.34", "10.0.0.2"],
                            "lo.example": ["127.0.0.1"], "meta.example": ["169.254.169.254"], "cgnat.example": ["100.64.0.9"]}):
            self.assertTrue(util.is_public_host("ok.example"))
            for bad in ("lan.example", "mixed.example", "lo.example", "meta.example", "cgnat.example", "missing.example"):
                self.assertFalse(util.is_public_host(bad), bad)

    def test_a_pinned_dns_answer_is_checked_too(self):
        with mock.patch.dict(util._dns_pin, {"evil.example": "127.0.0.1", "gov.example": "93.184.216.34"}):
            self.assertFalse(util.is_public_host("evil.example"))
            self.assertTrue(util.is_public_host("gov.example"))

    def test_a_private_address_is_refused_before_any_request(self):
        patch, sess = self.session()
        with self.resolves({"lan.example": ["192.168.1.5"]}), mock.patch.object(util, "ensure_resolvable", return_value=True), patch:
            with self.assertRaises(util.BlockedAddress):
                util.get_bounded("http://lan.example/admin")
        sess.get.assert_not_called()

    def test_odd_addresses_are_refused_outright(self):
        for bad in ("http://127.0.0.1:1\\@example.test/", "http://user:pw@example.test/", "http://exa mple.test/",
                    "file:///etc/passwd", "http:///nohost", "ftp://example.test/"):
            with self.assertRaises(util.BlockedAddress, msg=bad):
                util.get_bounded(bad)

    def test_a_redirect_into_the_network_is_refused(self):
        class Redirect:
            is_redirect, is_permanent_redirect = True, False
            headers = {"location": "http://localhost:8765/settings"}
            def close(self): pass
        patch, sess = self.session(Redirect())
        with self.resolves({"ok.example": ["93.184.216.34"], "localhost": ["127.0.0.1"]}), \
                mock.patch.object(util, "ensure_resolvable", return_value=True), patch:
            with self.assertRaises(util.BlockedAddress):
                util.get_bounded("https://ok.example/")
        self.assertEqual(sess.get.call_count, 1)                        # the second hop was never fetched
        self.assertFalse(sess.get.call_args.kwargs["allow_redirects"])

    def test_redirects_are_followed_one_checked_hop_at_a_time(self):
        class Hop:
            is_redirect, is_permanent_redirect = True, False
            headers = {"location": "/next"}
            def close(self): pass
        class Page:
            is_redirect, is_permanent_redirect = False, False
            url = ""
            def iter_content(self, chunk_size): yield b"<html>hello</html>"
            def close(self): pass
        patch, sess = self.session(Hop(), Page())
        with self.resolves({"ok.example": ["93.184.216.34"]}), mock.patch.object(util, "ensure_resolvable", return_value=True), patch:
            r, body = util.get_bounded("https://ok.example/start")
        self.assertEqual(body, b"<html>hello</html>")
        self.assertEqual(r.url, "https://ok.example/next")

    def test_the_connection_itself_goes_only_to_a_public_address(self):
        connected = []
        class FakeSock:
            def __init__(self, *a): pass
            def setsockopt(self, *a): pass
            def settimeout(self, t): pass
            def connect(self, sa): connected.append(sa)
            def close(self): pass
        guard = {"sock": None, "blocked": False}
        with mock.patch.object(util, "_orig_getaddrinfo", lambda host, port, *a, **k: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]), \
                mock.patch.object(util.socket, "socket", FakeSock):
            util._tls.guard = guard
            try:
                util._guarded_create_connection(("anywhere.example", 80), 5)
            finally:
                util._tls.guard = None
        self.assertEqual(connected, [("93.184.216.34", 80)])            # the private one was skipped, never tried

    def test_a_name_that_points_inside_the_network_is_never_connected_to(self):
        guard = {"sock": None, "blocked": False}
        with mock.patch.object(util, "_orig_getaddrinfo", lambda host, port, *a, **k: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]):
            util._tls.guard = guard
            try:
                with self.assertRaises(OSError):
                    util._guarded_create_connection(("anywhere.example", 80), 5)
            finally:
                util._tls.guard = None
        self.assertTrue(guard["blocked"])

    def test_without_a_guard_the_normal_connect_is_used(self):
        with mock.patch.object(util, "_orig_create_connection", return_value="normal") as orig:
            self.assertEqual(util._guarded_create_connection(("api.example", 443), 5), "normal")
        orig.assert_called_once()

    def test_a_name_that_only_looks_public_to_the_early_check_still_cannot_reach_a_local_server(self):
        """Parser differences, rebinding, unicode names, redirects: whatever fooled the early check, the connection
        is made to what the name really resolves to at that moment, and that is refused."""
        local = self.Local()
        try:
            with mock.patch.object(util, "public_addresses", return_value={"93.184.216.34"}), \
                    mock.patch.object(util, "ensure_resolvable", return_value=True), \
                    self.resolves({"looks-public.example": ["127.0.0.1"]}):
                with self.assertRaises(util.BlockedAddress):
                    util.get_bounded(f"http://looks-public.example:{local.port}/x")
            self.assertEqual(local.hits, 0)                              # the local server was never touched
        finally:
            local.close()

    def test_a_slow_drip_cannot_hold_the_fetch_past_its_deadline(self):
        local = self.Local(drip=True)
        try:
            with mock.patch.object(util, "public_addresses", return_value={"127.0.0.1"}), \
                    mock.patch.object(util, "ensure_resolvable", return_value=True), \
                    mock.patch.object(util, "_is_global", return_value=True):          # this one test may reach this machine
                t = time.monotonic()
                r, body = util.get_bounded(f"http://127.0.0.1:{local.port}/", deadline=1.5, timeout=3)
            self.assertLess(time.monotonic() - t, 6)                     # a byte every 0.3 s would take half a minute
            self.assertLess(len(body), 100)
        finally:
            local.close()


class RdapCacheTests(unittest.TestCase):
    class R:
        def __init__(self, code, body=None):
            self.status_code, self._body = code, body or {}
        def json(self):
            return self._body

    def setUp(self):
        rdap._cache.clear()
        self.boot = mock.patch.object(rdap, "_load_bootstrap", return_value={"com": "https://rdap.example/"})
        self.boot.start()

    def tearDown(self):
        self.boot.stop()
        rdap._cache.clear()

    def test_a_busy_registry_is_not_remembered_as_no_evidence(self):
        with mock.patch("requests.get", return_value=self.R(429)) as get, mock.patch("time.sleep"):
            self.assertIsNone(rdap.created("acme.com"))
            self.assertEqual(get.call_count, 3)
        ok = self.R(200, {"events": [{"eventAction": "registration", "eventDate": "2026-09-01T00:00:00Z"}]})
        rdap._cache["acme.com"] = (None, time.time() - 1)                      # the ten minutes are over
        with mock.patch("requests.get", return_value=ok):
            self.assertEqual(rdap.created("acme.com"), date(2026, 9, 1))

    def test_a_real_answer_is_remembered(self):
        with mock.patch("requests.get", return_value=self.R(404)) as get:
            self.assertIsNone(rdap.created("nobody.com"))
            self.assertIsNone(rdap.created("nobody.com"))
            self.assertEqual(get.call_count, 1)

    def test_no_bootstrap_means_no_answer_and_nothing_remembered(self):
        with mock.patch.object(rdap, "_load_bootstrap", return_value={}), mock.patch("requests.get") as get:
            self.assertIsNone(rdap.created("acme.com"))
            get.assert_not_called()
        self.assertNotIn("acme.com", rdap._cache)


class DnsPinTests(unittest.TestCase):
    def test_a_fallback_address_is_used_only_for_a_while(self):
        with mock.patch.dict(util._dns_pin, {"gov.example": "93.184.216.34"}, clear=False), \
                mock.patch.dict(util._dns_pin_until, {"gov.example": time.time() + 60}, clear=False):
            self.assertEqual(util._pinned("gov.example"), "93.184.216.34")
            util._dns_pin_until["gov.example"] = time.time() - 1
            self.assertIsNone(util._pinned("gov.example"))                      # expired: the normal resolver is asked again
            self.assertNotIn("gov.example", util._dns_pin)


class SourceHelperTests(unittest.TestCase):
    def test_email_kind(self):
        self.assertEqual(email_kind("a@gmail.com"), "free")
        self.assertEqual(email_kind("x@northwestregisteredagent.com"), "agent")
        self.assertEqual(email_kind("hello@acme.io"), "own")
        self.assertEqual(email_kind("hi@northwestern-software.com"), "own")      # no longer a substring false alarm
        self.assertEqual(email_kind("nope"), "none")

    def test_company_suffix_and_dates(self):
        self.assertTrue(looks_like_company("Pixelogy LLC"))
        self.assertFalse(looks_like_company("Rafelski Susanne M"))
        self.assertEqual(iso("2026-10-04T00:00:00.000"), "2026-10-04")
        self.assertEqual(iso("N/A"), "")

    def test_every_source_pages_with_a_unique_tiebreaker(self):
        seen = {}

        def fake_rows(self, where, order, select="", page=1000, cap=60000):
            seen[self.dataset] = order
            return []

        with mock.patch.object(Socrata, "rows", fake_rows):
            for cls in registries.ALL.values():
                cls().fetch(date(2026, 9, 1), date(2026, 10, 1))
        self.assertEqual(len(seen), 5)
        for ds, order in seen.items():
            self.assertIn(",", order, f"{ds} orders only by a non-unique column: {order}")

    def test_lags_cover_batch_publishing(self):
        self.assertGreaterEqual(registries.Texas.lag, 14)                       # weekly batches
        self.assertGreaterEqual(registries.LosAngeles.lag, 35)                  # monthly batches

    def test_individuals_are_flagged(self):
        rows = [{"taxpayer_name": "JANE HALE", "outlet_name": "JD CONSULTING LLC", "taxpayer_organization_type": "IS",
                 "taxpayer_number": "1", "outlet_number": "1", "outlet_naics_code": "541511",
                 "outlet_permit_issue_date": "2026-10-01T00:00:00.000", "outlet_city": "AUSTIN", "outlet_state": "TX"}]
        with mock.patch.object(Socrata, "rows", lambda *a, **k: rows):
            r = registries.Texas().fetch(date(2026, 9, 1), date(2026, 10, 1))
        self.assertTrue(r[0]["individual"])

    def test_la_state_comes_from_the_zip(self):
        rows = [{"business_name": "BRIGHTWORKS LLC", "street_address": "100 N EXAMPLE WAY", "city": "LEHI",
                 "zip_code": "84048-", "naics": "513210", "location_account": "1",
                 "location_start_date": "2026-10-01T00:00:00.000"}]
        with mock.patch.object(Socrata, "rows", lambda *a, **k: rows):
            r = registries.LosAngeles().fetch(date(2026, 9, 1), date(2026, 10, 1))
        self.assertEqual(r[0]["state"], "UT")


class ItClassTests(unittest.TestCase):
    def test_levels(self):
        it = itclass.classify("Acme | Custom software and SaaS platform",
                              "We build cloud software, APIs and integrations for developers. " * 5)
        self.assertEqual(it["level"], "strong")
        non = itclass.classify("Joe's Family Restaurant", "Our restaurant and catering menu. Fresh bakery daily. " * 20)
        self.assertEqual(non["level"], "none")
        self.assertEqual(itclass.classify("", "")["level"], "unknown")


class ContactTests(unittest.TestCase):
    HTML = """<html><head><title>Acme</title>
    <script>var vendor = "tracker@vendor-widgets.io";</script>
    <script type="application/ld+json">{"@type":"Organization","telephone":"+1 206-210-0188","founder":{"@type":"Person","name":"Grace Hopper"}}</script></head>
    <body><a href="mailto:hello@acme.io?subject=hi">mail</a> <span>call (425) 210-0223, fax (425) 210-0299</span>
    <p>sales [at] acme [dot] io</p><img src="logo@2x.png">
    <div>Jane Hale</div><div>Co-Founder &amp; CEO</div>
    <blockquote>"Great tool!" <cite>Rory McLeod, CEO</cite></blockquote>
    <blockquote>Jane Roe - CEO of Acme Freight</blockquote></body></html>"""

    def test_emails_phones_people(self):
        soup = BeautifulSoup(self.HTML, "lxml")
        text = contacts._visible(soup)
        emails = contacts.emails_in(soup, text)
        self.assertIn("hello@acme.io", emails)
        self.assertIn("sales@acme.io", emails)
        self.assertNotIn("tracker@vendor-widgets.io", emails)                   # script code is not page content
        self.assertFalse(any("2x.png" in e for e in emails))
        phones = contacts.phones_in(soup, text)
        self.assertIn("4252100223", phones)
        self.assertNotIn("4252100299", phones)                                  # fax
        ld = contacts.people_ld(contacts._jsonld(BeautifulSoup(self.HTML, "lxml")))
        self.assertEqual(ld[0]["name"], "Grace Hopper")

    def test_customer_quotes_are_not_the_companys_people(self):
        soup = BeautifulSoup(self.HTML, "lxml")
        contacts._visible(soup)
        names = [(p["name"], p["title"]) for p in contacts.leaders_text(soup)]
        self.assertIn(("Jane Hale", "Co-Founder & CEO"), names)
        self.assertFalse(any(n in ("Rory McLeod", "Jane Roe") for n, _ in names))

    def test_same_site_needs_a_dot_boundary(self):
        self.assertTrue(contacts.same_site("summitsoftware.com", "summitsoftware.com"))
        self.assertTrue(contacts.same_site("mail.summitsoftware.com", "www.summitsoftware.com"))
        self.assertFalse(contacts.same_site("notsummitsoftware.com", "summitsoftware.com"))

    def test_linkedin_goes_to_the_right_person(self):
        links = [("https://www.linkedin.com/in/aliceyoung", "", ["Alice Young Designer Bob Marley CEO"]),
                 ("https://www.linkedin.com/in/bob-marley-42", "", [])]
        self.assertEqual(contacts.match_linkedin("Bob Marley", ["Alice Young"], links),
                         "https://www.linkedin.com/in/bob-marley-42")
        # a shared team block never makes Alice's profile Bob's
        self.assertEqual(contacts.match_linkedin("Bob Marley", ["Alice Young"], links[:1]), "")
        self.assertEqual(contacts.match_linkedin("Alice Young", ["Bob Marley"], links[:1]),
                         "https://www.linkedin.com/in/aliceyoung")

    def test_odd_links_and_types_on_a_page_do_not_crash_the_parsers(self):
        soup = BeautifulSoup('<a href="telecom.html">x</a><a href="tel">y</a><a href="tel:+1 512 210 0200">z</a>', "lxml")
        self.assertEqual(contacts.phones_in(soup, ""), ["5122100200"])       # only the real tel: link gives a number
        self.assertEqual(contacts.people_ld([{"@type": [{"x": 1}, "Person"], "name": "Ada Lovelace"}])[0]["name"], "Ada Lovelace")
        self.assertEqual(contacts._abs("https://acme.io/", "http://["), "")                       # no browser could follow it
        contacts._abs("https://acme.io/", "https://[YOUR-URL]/contact")                            # raises on some Pythons: never here
        self.assertEqual(contacts._abs("https://acme.io/a/", "../b"), "https://acme.io/b")

    def test_fictional_and_press_contact_numbers_are_not_the_companys(self):
        soup = BeautifulSoup('<a href="tel:+1-817-555-0144">call</a><a href="tel:+1 512 210 0200">x</a>', "lxml")
        text = ("Pay to the order of Boudreau (817) 555-0144.  Media Contact: Brooke Nail, Public Relations (737) 394-1911. "
                "Call us on (888) 690-0977.")
        self.assertEqual(contacts.phones_in(soup, text), ["5122100200", "8886900977"])
        self.assertEqual(contacts.phones_in(BeautifulSoup("", "lxml"), "Fax (512) 210-0211 and phone (512) 210-0222"), ["5122100222"])

    def test_every_555_number_is_a_stock_number_not_a_company_line(self):
        """Two listed companies showed (415) 555-1234 and (860) 555-0341: the stock numbers of a mock-up, not only the 555-01xx
        range that is reserved for films. Directory enquiries 555-1212 is nobody's company line either."""
        for n in ("(415) 555-1234", "860-555-0341", "(212) 555-1212", "312.555.5555", "+1 206 555 0100", "(206) 555-0199", "737 555 0200"):
            self.assertEqual(contacts.phones_in(BeautifulSoup("", "lxml"), f"Call us on {n} today"), [], n)
            tel = BeautifulSoup(f'<a href="tel:{n}">call</a>', "lxml")
            self.assertEqual(contacts.phones_in(tel, ""), [], n)
        # a real number next to a stock one is kept
        self.assertEqual(contacts.phones_in(BeautifulSoup("", "lxml"), "Office (415) 555-1234 or (415) 210-4000"), ["4152104000"])
        # only the exchange (the middle three digits) makes a 555 number a stock number; 555 inside a line is ordinary
        self.assertEqual(contacts.phones_in(BeautifulSoup("", "lxml"), "(212) 210-0555"), ["2122100555"])

    def test_numbers_that_cannot_exist_or_belong_to_nobody_are_not_company_lines(self):
        for bad in ("8001234567", "(800) 123-4567", "1234567890", "0000000000", "2222222222", "5005001000", "9005551111",
                    "7005550100", "2125550100", "4155551234", "8605550341", "2121100000", "", None, "12345", "212-110-0000"):
            self.assertTrue(util.placeholder_phone(bad), repr(bad))
            self.assertEqual(util.usable_phone(bad), "", repr(bad))
        for good in ("(415) 210-4000", "+1 860 210 0341", "888-690-0977", "7132104499", "8445002020"):
            self.assertFalse(util.placeholder_phone(good), good)
            self.assertEqual(util.usable_phone(good), util.norm_phone(good), good)

    def test_a_json_ld_phone_that_is_a_stock_number_is_not_used(self):
        def scrape_with(telephone):
            html = ('<html><head><script type="application/ld+json">{"@type":"Organization","telephone":"%s"}</script></head>'
                    '<body>hello</body></html>' % telephone)
            with mock.patch.object(contacts, "load", side_effect=lambda u, timeout=8: contacts.Page("https://acme.io/", html.encode())
                                   if u.rstrip("/") == "https://acme.io" else None):
                return contacts.scrape("https://acme.io", "acme.io", "Acme LLC")
        self.assertEqual(scrape_with("+1 415-555-1234")["phones"], [])
        self.assertEqual(scrape_with("+1 415-210-4000")["phones"], ["4152104000"])

    def test_the_first_equally_good_mailbox_in_the_page_wins_not_the_alphabetical_one(self):
        self.assertEqual(enrich._pick_email(["hello@acme.io", "hello@acme.io"], [], "acme.io")[0], "hello@acme.io")
        self.assertEqual(enrich._pick_email(["jane@acme.io", "bob@acme.io"], [], "acme.io")[0], "jane@acme.io")

    def test_pages_that_carry_other_peoples_contact_details_are_not_read(self):
        for path in ("/articles/news/mindgauge-selected-for-three-posters", "/press/2026/launch", "/media-kit", "/careers/",
                     "/about/careers/", "/jobs/engineer", "/events/summit", "/blog/post-1", "/privacy-policy"):
            self.assertTrue(contacts.SKIP_PATH.search(path), path)
        for path in ("/contact", "/contact-us", "/about", "/about-us", "/team", "/our-story", "/leadership"):
            self.assertFalse(contacts.SKIP_PATH.search(path), path)

    def test_only_plain_linkedin_profile_addresses_are_kept(self):
        self.assertEqual(contacts.li_clean("https://www.linkedin.com/in/jane-doe?utm=1#x"), "https://www.linkedin.com/in/jane-doe")
        self.assertEqual(contacts.li_clean("https://uk.linkedin.com/company/acme-ltd/"), "https://www.linkedin.com/company/acme-ltd")
        for bad in ("javascript:alert(1)//linkedin.com/in/jane", "=HYPERLINK(\"http://evil\")linkedin.com/in/x",
                    "https://linkedin.com.evil.example/in/jane", "https://evil.example/?u=linkedin.com/in/jane",
                    "https://www.linkedin.com/in/", "https://www.linkedin.com/in/a b", "", None):
            self.assertEqual(contacts.li_clean(bad), "", bad)

    def test_cloudflare_email_decode(self):
        key = 0x2a
        enc = format(key, "02x") + "".join(format(ord(ch) ^ key, "02x") for ch in "me@x.com")
        self.assertEqual(contacts._cf_decode(enc), "me@x.com")


class PersonNameTests(unittest.TestCase):
    """A contact name must look like a person's. Found on the list: 'Jobs Pipeline', 'CFO Questionnaire', 'Explore Platform',
    'BBB National Programs', 'Tarnus Corporation. This', 'OneGCP DotNet', 'Pencil Your Media LLC'."""

    def test_what_is_taken_for_a_person(self):
        for good, shown in (("Pat Marlow", "Pat Marlow"), ("Derek O'Hare", "Derek O'Hare"), ("Derek O’Hare", "Derek O'Hare"),
                            ("Sarah Brandt-Mueller", "Sarah Brandt-Mueller"), ("Simon Anthony Abbot-Fenn", "Simon Anthony Abbot-Fenn"),
                            ("Anil R Bhardwaj", "Anil R Bhardwaj"), ("Francis van Roden", "Francis van Roden"),
                            ("José García", "José García"), ("JOHN SMITH", "John Smith"), ("Dr. Jane Hale", "Jane Hale"),
                            ("Engr. Muhammad Hamza Kader", "Muhammad Hamza Kader"), ("John Smith III", "John Smith III"),
                            ("JP Quill", "JP Quill"), ("Rory McLeod", "Rory McLeod"), ("Michael DAvenant", "Michael DAvenant")):
            self.assertEqual(contacts.okname(good), shown, good)

    def test_what_is_not(self):
        for bad in ("Jobs Pipeline", "CFO Questionnaire", "Explore Platform", "BBB National Programs", "Tarnus Corporation. This",
                    "Toby Gallagher. Questions", "Wexmere. No", "OneGCP DotNet", "Pencil Your Media LLC", "Fernhill Holdings LLC",
                    "Meet The Team", "Our Team", "Contact Us", "Support Team", "Acme Labs", "Joe Studio", "Jane Careers",
                    "Leads Team", "Quillsec Solutions", "Pat Marlow Services", "Madonna", "One Two Three Four Five", "John Smith, CEO",
                    "John 2 Smith", "John: Smith", "john smith", "Jane\nDoe", "J. Smith", "Q Z", "A" * 70 + " Smith", "", None, 7):
            self.assertIsNone(contacts.okname(bad), repr(bad))

    def test_a_state_filing_may_write_names_in_small_letters_a_web_page_may_not(self):
        self.assertEqual(contacts.okname("alina arbuckle", registry=True), "Alina Arbuckle")
        self.assertEqual(contacts.okname("MARY O'NEIL", registry=True), "Mary O'Neil")
        self.assertEqual(contacts.okname("francis van roden", registry=True), "Francis van Roden")
        self.assertIsNone(contacts.okname("alina arbuckle"))
        self.assertIsNone(contacts.okname("pencil your media llc", registry=True))

    def test_titles_are_plain_text_of_at_most_60_characters(self):
        for raw, shown in (("Founder", "Founder"), ("THE FOUNDER", "Founder"), ("Meet the Founder", "Founder"),
                           ("CHIEF EXECUTIVE OFFICER", "Chief Executive Officer"), ("Co-Founder &", "Co-Founder"),
                           ("Owner of", "Owner"), ("(CEO & Director Operations)", "CEO & Director Operations"),
                           ("Officer;Director", "Officer;Director"), ("CEO · Commercial Subcontracting", "CEO · Commercial Subcontracting"),
                           ("Founder · MBA Candidate, Harvard Business School · Former Consultant at a Very Large Firm", "Founder")):
            self.assertEqual(contacts.clean_title(raw), shown, raw)
        for bad in ('Owner summary: "2 new leads today — $3,400 potential. Mike i', "<b>CEO</b>", "x" * 61, "Chief " * 12, "", None,
                    "Founder $5 off", "see https://acme.io/ceo"):
            self.assertEqual(contacts.clean_title(bad), "", repr(bad))
        self.assertLessEqual(len(contacts.clean_title("Chief Executive Officer, Board Director and Founder of Many Things")), 60)

    def test_a_dashboard_mock_up_next_to_the_word_owner_is_not_a_person(self):
        text = ("Jane Hale\nFounder\nJobs Pipeline\nOwner summary: \"2 new leads today — $3,400 potential. Mike is following up on both.\"\n"
                "CFO Questionnaire\nCEO Questionnaire\nExplore Platform\nA Note From the CEO")
        self.assertEqual([(p["name"], p["title"]) for p in contacts.people_text(text)], [("Jane Hale", "Founder")])

    def test_a_sentence_does_not_end_up_in_a_name(self):
        text = "Acme was founded by Toby Gallagher. Questions? Write to us. Tarnus was founded by Tarnus Corporation. This page..."
        self.assertEqual([p["name"] for p in contacts.people_text(text)], ["Toby Gallagher"])

    def test_json_ld_people_are_checked_too(self):
        ld = [{"@type": "Person", "name": "Quillsec Team", "jobTitle": "Founder"},
              {"@type": "Person", "name": "Ada Lovelace", "jobTitle": "Founder & CEO"},
              {"@type": "Organization", "founder": [{"@type": "Person", "name": "BBB National Programs"}, "Grace Hopper"]},
              {"@type": "Person", "name": "Alan Turing", "jobTitle": "Owner: see https://x.example"}]
        got = [(p["name"], p["title"]) for p in contacts.people_ld(ld)]
        self.assertEqual(got, [("Ada Lovelace", "Founder & CEO"), ("Grace Hopper", "Founder"), ("Alan Turing", "")])


class ScrapeChoiceTests(unittest.TestCase):
    """The lookup of a whole site, with the pages served from a dictionary."""

    @staticmethod
    def site(pages):
        def load(url, timeout=8):
            hit = pages.get(url.split("#")[0].rstrip("/"))
            if hit is None:
                return None
            final, html = hit if isinstance(hit, tuple) else (url.rstrip("/"), hit)
            return contacts.Page(final, html.encode())
        return load

    def scrape(self, pages, domain="acme.io"):
        with mock.patch.object(contacts, "load", self.site(pages)):
            return contacts.scrape("https://" + domain, domain, "Acme LLC")

    HOME = ('<html><head><title>Acme software</title></head><body><a href="/contact">Contact</a><a href="/about">About</a>'
            '<a href="/privacy">Privacy</a></body></html>')

    def test_a_page_that_leads_to_another_companys_site_is_not_read(self):
        """vexlon.com's contact and about links led to a mortgage servicer's site: its three phone numbers and four emails were
        stored as the company's own."""
        other = '<html><body>Call <a href="tel:+1 855 210 3690">855-210-3690</a> <a href="mailto:help@servicer.example">mail</a> Jane Hale, CEO</body></html>'
        got = self.scrape({"https://acme.io": self.HOME,
                           "https://acme.io/contact": ("https://lending.servicer.example/contact-us", other),
                           "https://acme.io/about": ("https://lending.servicer.example/about-us", other),
                           "https://acme.io/privacy": ("https://lending.servicer.example/privacy-policy", other)})
        self.assertTrue(got["ok"])
        self.assertEqual((got["phones"], got["emails"], got["people"]), ([], [], []))
        own = self.scrape({"https://acme.io": self.HOME,
                           "https://acme.io/contact": '<html><body>Call <a href="tel:+1 855 210 3690">x</a></body></html>'})
        self.assertEqual(own["phones"], ["8552103690"])

    def test_a_number_shown_only_in_the_privacy_policy_is_marked_so(self):
        """pixelward.ai shows its number only as the data protection officer's line in the privacy policy."""
        privacy = ("<html><body><h1>Privacy</h1>Our officer is reachable at (415) 210-4000, help@acme.io or by post."
                   "</body></html>")
        got = self.scrape({"https://acme.io": self.HOME, "https://acme.io/privacy": privacy})
        self.assertEqual((got["phones"], got["legal_only_phones"]), (["4152104000"], ["4152104000"]))
        self.assertEqual((got["emails"], got["legal_only_emails"]), (["help@acme.io"], ["help@acme.io"]))
        site = {"url": "https://acme.io/", "domain": "acme.io", "why": ["x"], "created": "", "_page": None}
        with mock.patch.object(website, "discover", return_value=dict(site)), mock.patch.object(contacts, "scrape", return_value=dict(got)):
            e = enrich.enrich_one({"name": "Acme LLC", "emails": [], "phone": ""})
        self.assertEqual((e["phone"], e["phone_from"]), ("4152104000", "company website (privacy or terms page only)"))
        self.assertEqual((e["email"], e["email_from"]), ("help@acme.io", "company website (privacy or terms page only)"))
        shown = self.scrape({"https://acme.io": self.HOME, "https://acme.io/privacy": privacy,
                             "https://acme.io/contact": "<html><body>Phone: 415-210-4000 <a href='mailto:help@acme.io'>mail</a></body></html>"})
        self.assertEqual((shown["phones"], shown["legal_only_phones"], shown["legal_only_emails"]), (["4152104000"], [], []))
        with mock.patch.object(website, "discover", return_value=dict(site)), mock.patch.object(contacts, "scrape", return_value=dict(shown)):
            e = enrich.enrich_one({"name": "Acme LLC", "emails": [], "phone": ""})
        self.assertEqual((e["phone_from"], e["email_from"]), ("company website", "company website"))

    def test_a_number_on_a_real_page_goes_before_one_in_the_privacy_policy(self):
        privacy = "<html><head><script type='application/ld+json'>{\"@type\":\"Organization\",\"telephone\":\"+1 415 210 4001\"}</script></head><body>x</body></html>"
        contact = "<html><body>Phone 415-210-4000</body></html>"
        got = self.scrape({"https://acme.io": self.HOME, "https://acme.io/privacy": privacy, "https://acme.io/contact": contact})
        self.assertEqual(got["phones"], ["4152104000", "4152104001"])
        self.assertEqual(got["legal_only_phones"], ["4152104001"])

    def test_stock_numbers_on_a_page_are_not_the_companys_phone(self):
        contact = "<html><body>Call (860) 555-0341 or (415) 555-1234. Sales: <a href='tel:212-555-1212'>x</a></body></html>"
        got = self.scrape({"https://acme.io": self.HOME, "https://acme.io/contact": contact})
        self.assertEqual(got["phones"], [])

    def test_a_dashboard_mock_up_on_the_page_gives_no_contact_person(self):
        about = ("<html><body><div>Jobs Pipeline</div><div>Owner summary: \"2 new leads today — $3,400 potential.\"</div>"
                 "<div>Explore Platform</div><div>A Note From the CEO</div></body></html>")
        got = self.scrape({"https://acme.io": self.HOME, "https://acme.io/about": about})
        self.assertEqual(got["people"], [])

    def test_customer_stories_and_articles_are_not_read_for_contacts(self):
        for path in ("/client-success-stories/hospital-recovers-data", "/case-studies/legal-teams", "/perspectives/how-to-scale-ai",
                     "/insights/ai", "/customer-stories/acme", "/testimonials", "/resources/ebook-download.html"):
            self.assertTrue(contacts.SKIP_PATH.search(path), path)
        for path in ("/our-story", "/about/leadership", "/people", "/company", "/contact"):
            self.assertFalse(contacts.SKIP_PATH.search(path), path)


class WrongMailboxTests(unittest.TestCase):
    """Found on the list: concerns@acmeconsulting.com (where to report a scam invoice), remove.me@ordnex.com, pam.hr@,
    corporatenotice@vexlon.com (the address for legal notices, taken from the state filing)."""

    def test_function_mailboxes_are_never_the_companys_address(self):
        pick = enrich._pick_email
        for local in ("concerns", "complaints", "fraud", "scam", "phishing", "report", "whistleblower", "ethics", "dpo",
                      "data-protection", "corporatenotice", "legal-notices", "notices", "registeredagent", "remove.me", "optout",
                      "pam.hr", "jobs", "careers", "privacy", "legal", "billing"):
            self.assertTrue(enrich.not_a_contact(local), local)
            self.assertEqual(pick([f"{local}@acme.io"], [], "acme.io"), ("", ""), local)
            self.assertEqual(pick([], [f"{local}@acme.io"], "acme.io"), ("", ""), local)
        for local in ("hello", "info", "support", "sales", "jane", "jane.doe", "dallas", "hrisha", "chris"):
            self.assertFalse(enrich.not_a_contact(local), local)

    def test_a_scam_report_address_is_skipped_and_the_office_of_the_filings_city_goes_first(self):
        site = ["concerns@acme.io", "atlanta@acme.io", "bogota@acme.io", "dallas@acme.io", "houston@acme.io"]
        self.assertEqual(enrich._pick_email(site, [], "acme.io", "Dallas"), ("dallas@acme.io", "company website"))
        self.assertEqual(enrich._pick_email(site, [], "acme.io"), ("atlanta@acme.io", "company website"))      # no city: page order
        self.assertEqual(enrich._pick_email(site + ["info@acme.io"], [], "acme.io", "Dallas")[0], "info@acme.io")  # a front door beats it
        self.assertEqual(enrich._pick_email(["los-angeles@acme.io"], [], "acme.io", "Los Angeles")[0], "los-angeles@acme.io")

    def test_an_address_from_the_state_filing_is_used_only_on_the_websites_own_domain(self):
        """Seven listed Connecticut companies carry the filing's address, which is not on their site."""
        pick = enrich._pick_email
        self.assertEqual(pick([], ["jordan@acme.io"], "acme.io"), ("jordan@acme.io", "state registry"))
        self.assertEqual(pick([], ["jordan@mail.acme.io"], "www.acme.io"), ("jordan@mail.acme.io", "state registry"))
        self.assertEqual(pick([], ["jordan@acme-llc.com"], "acme.io"), ("", ""))                # another domain
        self.assertEqual(pick([], ["jordan@notacme.io"], "acme.io"), ("", ""))                  # a look-alike
        self.assertEqual(pick([], ["jordan@gmail.com"], "acme.io"), ("", ""))                   # a person's private mailbox
        self.assertEqual(pick([], ["jordan@registeredagentsinc.com"], "acme.io"), ("", ""))     # the agent's address
        self.assertEqual(pick([], ["jordan@bank.co.uk"], "acme.co.uk"), ("", ""))               # a whole country's suffix is not a site
        self.assertEqual(pick([], ["jordan@mail.acme.co.uk"], "acme.co.uk"), ("jordan@mail.acme.co.uk", "state registry"))

    def test_the_same_site_rule_knows_second_level_country_suffixes(self):
        self.assertTrue(contacts.same_site("acme.co.uk", "www.acme.co.uk"))
        self.assertFalse(contacts.same_site("other.co.uk", "acme.co.uk"))
        self.assertFalse(contacts.same_site("evilacme.co.uk", "acme.co.uk"))
        self.assertFalse(contacts.same_site("acme.io", ""))

    def test_the_extra_mailboxes_listed_are_not_function_mailboxes_either(self):
        site = {"url": "https://acme.io/", "domain": "acme.io", "why": ["x"], "created": "", "_page": None}
        sc = {"ok": True, "emails": ["hello@acme.io", "legal@acme.io", "sales@acme.io", "concerns@acme.io"], "phones": [], "people": [],
              "linkedin_company": "", "head": "software", "body": ""}
        with mock.patch.object(website, "discover", return_value=dict(site)), mock.patch.object(contacts, "scrape", return_value=sc):
            e = enrich.enrich_one({"name": "Acme LLC", "emails": [], "phone": ""})
        self.assertEqual((e["email"], e["emails"]), ("hello@acme.io", ["sales@acme.io"]))


class ContactChoiceTests(unittest.TestCase):
    SITE = {"url": "https://acme.io/", "domain": "acme.io", "why": ["x"], "created": "", "_page": None}

    def one(self, people, registry_people=()):
        sc = {"ok": True, "emails": ["hi@acme.io"], "phones": [], "people": list(people), "linkedin_company": "", "head": "software",
              "body": ""}
        rec = {"name": "Acme LLC", "emails": [], "phone": "", "people": list(registry_people)}
        with mock.patch.object(website, "discover", return_value=dict(self.SITE)), mock.patch.object(contacts, "scrape", return_value=sc):
            return enrich.enrich_one(rec)

    def test_a_heading_taken_for_a_person_gives_way_to_the_filings_officer(self):
        """Recall Point Systems showed 'BBB National Programs, Founder'; Crewmark 'Jobs Pipeline, Owner summary: ...'."""
        bad = [{"name": "BBB National Programs", "title": "Founder", "email": "", "linkedin": ""},
               {"name": "Jobs Pipeline", "title": "Owner summary: \"2 new leads today", "email": "", "linkedin": ""}]
        e = self.one(bad, [{"name": "Dana Rivers", "title": "Director"}])
        self.assertEqual(e["contact"], {"name": "Dana Rivers", "title": "Director", "email": "", "linkedin": "", "from": "state registry"})
        self.assertEqual(self.one(bad)["contact"], {})                                     # nobody to name: nobody is named

    def test_the_first_real_person_is_chosen_and_tidied(self):
        people = [{"name": "Explore Platform", "title": "A Note From the CEO", "email": "", "linkedin": ""},
                  {"name": "JANE HALE", "title": "CO-FOUNDER &", "email": "jane@acme.io", "linkedin": ""}]
        c = self.one(people)["contact"]
        self.assertEqual((c["name"], c["title"], c["from"]), ("Jane Hale", "Co-Founder", "company website"))

    def test_a_company_or_a_label_in_the_filing_is_not_the_contact(self):
        reg = [{"name": "Pencil Your Media LLC", "title": ""}, {"name": "OneGCP DotNet", "title": ""}, {"name": "samuel nyberg", "title": ""}]
        self.assertEqual(self.one([], reg)["contact"]["name"], "Samuel Nyberg")
        self.assertEqual(self.one([], reg[:2])["contact"], {})
        odd = [None, "x", {"name": None}, {"name": "Pat Marlow", "title": "Officer;Director"}]
        self.assertEqual(self.one([], odd)["contact"]["title"], "Officer;Director")

    def test_the_lookup_of_a_page_never_names_a_non_person(self):
        about = ("<html><body><div>Jobs Pipeline</div><div>Owner summary: \"2 new leads today — $3,400 potential. Mike is on both.\"</div>"
                 "<div>Jane Hale</div><div>Founder</div></body></html>")
        pages = {"https://acme.io": '<html><body><a href="/about">About</a></body></html>', "https://acme.io/about": about}
        def load(url, timeout=8):
            hit = pages.get(url.rstrip("/"))
            return contacts.Page(url.rstrip("/"), hit.encode()) if hit else None
        with mock.patch.object(contacts, "load", load):
            got = contacts.scrape("https://acme.io", "acme.io", "Acme LLC")
        self.assertEqual([(p["name"], p["title"]) for p in got["people"]], [("Jane Hale", "Founder")])


class StoredResultTests(unittest.TestCase):
    """Rules that tighten apply to results stored earlier, so the whole list follows them."""
    REC = {"id": "ct:1", "source": "ct", "name": "Acme LLC", "registered": "2026-09-22", "individual": False, "emails": [],
           "people": [{"name": "Dana Rivers", "title": "Officer"}]}
    CFG = {"require": ["website", "email", "phone"], "require_it_signal": True}

    def e(self, **kw):
        base = {"website": "https://acme.io", "domain": "acme.io", "email": "hello@acme.io", "email_from": "company website",
                "phone": "4152104000", "phone_from": "company website", "phones": ["4152104000"], "emails": [],
                "contact": {"name": "Jane Hale", "title": "Founder", "email": "", "linkedin": "", "from": "company website"},
                "linkedin": "", "it": {"level": "strong", "score": 9}}
        base.update(kw)
        return base

    def test_a_stored_stock_number_counts_as_no_phone(self):
        for stock in ("4155551234", "8605550341", "2125551212", "5125550100", "8001234567", "5005001000"):
            self.assertEqual(qualify(self.REC, self.e(phone=stock), self.CFG), ("held", ["phone"]), stock)
            either = dict(self.CFG, contact_either=True)
            self.assertEqual(qualify(self.REC, self.e(phone=stock, email=""), either), ("held", ["email or phone"]), stock)
            self.assertEqual(qualify(self.REC, self.e(phone=stock), either), ("ready", []), stock)           # the email is enough
        self.assertEqual(qualify(self.REC, self.e(), self.CFG), ("ready", []))

    def test_a_stored_function_mailbox_counts_as_no_email(self):
        for box in ("concerns@acme.io", "remove.me@acme.io", "corporatenotice@acme.io", "pam.hr@acme.io"):
            self.assertEqual(qualify(self.REC, self.e(email=box), self.CFG), ("held", ["email"]), box)

    def test_clean_stored_takes_out_what_the_checks_now_refuse(self):
        e = self.e(phone="8605550341", phones=["8605550341"], email="concerns@acme.io",
                   contact={"name": "Jobs Pipeline", "title": "Owner summary: \"2 new", "email": "", "linkedin": "https://www.linkedin.com/in/x",
                            "from": "company website"}, linkedin="https://www.linkedin.com/in/x")
        before = copy_of(e)
        c = enrich.clean_stored(e, self.REC)
        self.assertEqual(e, before)                                                   # the stored result itself is never changed
        self.assertEqual((c["phone"], c["phone_from"], c["phones"]), ("", "", []))
        self.assertEqual((c["email"], c["email_from"]), ("", ""))
        self.assertEqual(c["contact"], {"name": "Dana Rivers", "title": "Officer", "email": "", "linkedin": "", "from": "state registry"})
        self.assertEqual(c["linkedin"], "")
        self.assertEqual(enrich.clean_stored(e)["contact"], {})                       # without the filing: no contact

    def test_clean_stored_keeps_the_good_and_tidies_the_title(self):
        self.assertEqual(enrich.clean_stored(self.e()), self.e())
        c = enrich.clean_stored(self.e(contact={"name": "Randy Smith", "title": "Owner of", "email": "", "linkedin": "", "from": "company website"}))
        self.assertEqual((c["contact"]["name"], c["contact"]["title"]), ("Randy Smith", "Owner"))
        two = enrich.clean_stored(self.e(phone="4155551234", phones=["4155551234", "4152104001"]))
        self.assertEqual((two["phone"], two["phones"]), ("4152104001", ["4152104001"]))       # the next stored number takes its place
        reg = {"name": "alina arbuckle", "title": "", "email": "", "linkedin": "", "from": "state registry"}
        self.assertEqual(enrich.clean_stored(self.e(contact=reg))["contact"]["name"], "Alina Arbuckle")
        web = dict(reg, name="Pat Marlow", title="", **{"from": "company website"})        # a website contact needs a leader's title
        self.assertEqual(enrich.clean_stored(self.e(contact=web), self.REC)["contact"]["from"], "state registry")

    def test_requalify_pulls_companies_not_yet_sent_that_show_a_stock_number_or_a_function_mailbox(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d) / "t.db")
            day = date(2026, 10, 6)
            rows = {"ct:1": self.e(), "ct:2": self.e(domain="b.io", phone="4155551234", phones=["4155551234"]),
                    "ct:3": self.e(domain="c.io", email="concerns@c.io")}
            for cid, e in rows.items():
                s.add(dict(self.REC, id=cid), day)
                s.record_attempt(cid, e, "ready", day)
            s.add(dict(self.REC, id="ct:4"), day)                    # already in the sheet: not touched here
            s.record_attempt("ct:4", self.e(domain="d.io", phone="4155551234"), "ready", day)
            s.mark_pushed("ct:4", "h", day)
            self.assertEqual(pipeline.requalify(s, self.CFG, day), 0)
            self.assertEqual(sorted(i["id"] for i in s.ready()), ["ct:1"])
            held = s.db.execute("SELECT id, state, next_due FROM companies WHERE state='held' ORDER BY id").fetchall()
            self.assertEqual([(r["id"], r["next_due"]) for r in held], [("ct:2", "2026-10-07"), ("ct:3", "2026-10-07")])   # looked at again tomorrow
            self.assertEqual(s.counts()["pushed"], 1)
            s.close()

    def test_a_held_company_whose_only_phone_is_a_stock_number_is_not_released(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d) / "t.db")
            day = date(2026, 10, 6)
            s.add(dict(self.REC), day)
            s.record_attempt("ct:1", dict(self.e(phone="4155551234", phones=["4155551234"]), missing=["phone"]), "held", day)
            self.assertEqual(pipeline.requalify(s, self.CFG, day), 0)
            self.assertEqual(pipeline.requalify(s, dict(self.CFG, require=["website", "email"]), day), 1)   # without the phone rule: yes
            s.close()


def copy_of(x):
    import copy
    return copy.deepcopy(x)


class HostilePageTests(unittest.TestCase):
    """Pages written to hurt: they must cost little time and leave nothing the database or a spreadsheet cannot hold."""

    def test_a_page_of_blanks_does_not_freeze_the_email_patterns(self):
        html = ("<html><body><p>a" + " " * 200000 + "b</p></body></html>").encode()
        soup = BeautifulSoup(html, "lxml")
        t = time.monotonic()
        contacts.emails_in(soup, contacts._visible(soup))
        self.assertLess(time.monotonic() - t, 2.0)                       # it took minutes to hours before

    def test_a_page_of_thousands_of_profile_links_is_cheap(self):
        links = "".join(f'<div><span>Person {i}</span><a href="https://www.linkedin.com/in/p{i}">x</a></div>' for i in range(3000))
        soup = BeautifulSoup(f"<html><body>{links}</body></html>", "lxml")
        t = time.monotonic()
        company, people = contacts.linkedin(soup)
        self.assertLess(time.monotonic() - t, 2.0)
        self.assertLessEqual(len(people), 40)

    def test_text_taken_from_a_page_is_made_safe(self):
        self.assertEqual(util.clean_text("ab\udc00c\x00d\uffffe\ufffef"), "abcdef")
        self.assertEqual(util.clean_text("x" * 1000, 50), "x" * 50)
        self.assertEqual(util.scrub({"a": ["b\udc00", {"c": "d\x07"}], "n": 3}), {"a": ["b", {"c": "d"}], "n": 3})
        self.assertTrue(util.valid_email("jane.doe@acme.io"))
        for bad in ("x\udc00@acme.test", "a@b", "a b@acme.io", "ab\uffff@acme.test", "a@acme.io\n", "a" * 70 + "@acme.io", ""):
            self.assertFalse(util.valid_email(bad), repr(bad))

    def test_a_lone_surrogate_in_a_json_ld_email_is_dropped(self):
        ld = [{"@type": "Person", "name": "Ada Lovelace", "jobTitle": "Founder", "email": "ada\udc00@acme.test"},
              {"@type": "Organization", "founder": {"name": "Bob\udc00 Ray", "email": "bob@acme.test\n"}}]
        people = contacts.people_ld(ld)
        self.assertEqual([p["email"] for p in people], ["", "bob@acme.test"])      # the stray newline is trimmed, the surrogate one is dropped
        for p in people:
            p["name"].encode("utf-8")                                    # would raise on a lone surrogate

    def test_a_lookup_result_can_always_be_stored(self):
        site = {"url": "https://acme.io/", "domain": "acme.io", "why": ["x"], "created": "", "_page": None}
        scrape = {"ok": True, "emails": ["ab\uffff@acme.io"], "phones": [], "linkedin_company": "https://www.linkedin.com/company/x\udc00",
                  "people": [{"name": "Ada\udc00", "title": "Founder\x00", "email": "", "linkedin": ""}], "head": "h\udc00", "body": "b"}
        with mock.patch.object(website, "discover", return_value=site), mock.patch.object(contacts, "scrape", return_value=scrape):
            out = enrich.enrich_one({"name": "Acme LLC", "emails": [], "phone": ""})
        import json
        json.dumps(out).encode("ascii")                                  # the default json dump, then the database
        self.assertNotIn("\udc00", json.dumps(out, ensure_ascii=False))

    def test_the_database_takes_whatever_it_is_given(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d) / "t.db")
            s.add({"id": "ct:1", "source": "ct", "name": "Bad\udc00 LLC"}, date(2026, 10, 1))
            s.record_attempt("ct:1", {"contact": {"email": "x\udc00@y.z"}}, "held", date(2026, 10, 1))
            self.assertEqual(s.counts()["held"], 1)
            s.close()
        row_hash({"a": "b\udc00"})                                      # hashing a row cannot raise either

    def test_one_company_that_cannot_be_recorded_does_not_stop_the_others(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d) / "t.db")
            for i in (1, 2):
                s.add({"id": f"ct:{i}", "source": "ct", "name": f"Co {i} LLC", "registered": "2026-10-01", "individual": False, "emails": []},
                      date(2026, 10, 1))
            real = s.record_attempt
            calls = []
            def flaky(cid, enrich_, state, today):
                calls.append(cid)
                if cid == "ct:1" and len(calls) == 1:
                    raise UnicodeEncodeError("utf-8", "x", 0, 1, "surrogates not allowed")
                return real(cid, enrich_, state, today)
            lines = []
            with mock.patch.object(enrich, "enrich_one", return_value=dict(enrich.empty_result())), \
                    mock.patch.object(s, "record_attempt", flaky):
                stats = pipeline.enrich_pending(s, {"workers": 2, "require": ["website"], "require_it_signal": False},
                                                date(2026, 10, 1) + timedelta(days=40), lines.append)
            self.assertEqual(stats["crashed"], 1)
            self.assertEqual(s.db.execute("SELECT COUNT(*) FROM companies WHERE attempts>0").fetchone()[0], 2)   # both were recorded
            s.close()


class WebsiteTests(unittest.TestCase):
    def test_candidates_hint_first(self):
        rec = {"name": "Corvane LLC", "trade_name": "", "emails": ["jordan@corvanelabs.io", "x@gmail.com"]}
        c = website.candidates(rec)
        self.assertEqual(c[0], ("corvanelabs.io", True))
        self.assertIn(("corvane.com", False), c)
        self.assertFalse(any(d.startswith("gmail") for d, _ in c))

    def test_address_pattern(self):
        ap = website.addr_pattern("200 NW FIRST ST # 212")
        self.assertTrue(ap.search("visit us at 200 nw first st, seattle"))
        self.assertFalse(ap.search("1546 nw market"))

    def test_a_name_no_dns_can_hold_is_simply_not_a_site(self):
        with mock.patch.object(socket, "getaddrinfo", side_effect=UnicodeError("label too long")):
            self.assertEqual(website.dns_state("a" * 70 + ".com"), "nx")

    def test_dns_blip_is_not_a_missing_website(self):
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror(socket.EAI_NONAME, "no such name")):
            self.assertEqual(website.dns_state("nothing-here.example"), "nx")
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror(socket.EAI_AGAIN, "try again")):
            util.reset_net_errors()
            self.assertEqual(website.dns_state("x.example"), "temp")
            self.assertEqual(util.net_errors(), 1)

    def _page(self, html):
        return contacts.Page("https://summitsoftware.com/", html.encode())

    def test_another_companys_site_that_only_names_the_state_is_rejected(self):
        rec = {"name": "Summit Software LLC", "trade_name": "", "city": "Austin", "state": "TX", "address": "9 Pine Rd",
               "phone": "", "registered": "2026-09-01", "individual": False}
        denver = "<html><head><title>Summit Software</title></head><body>Offices: Denver, CO and Dallas, TX. Call 303-555-0143</body></html>"
        with mock.patch.object(contacts, "load", return_value=self._page(denver)), \
                mock.patch.object(rdap, "created", return_value=None):
            self.assertIsNone(website.check_domain(rec, "summitsoftware.com", False))
        austin = denver.replace("Denver, CO and Dallas, TX", "Austin, TX")
        with mock.patch.object(contacts, "load", return_value=self._page(austin)), \
                mock.patch.object(rdap, "created", return_value=None):
            ev = website.check_domain(rec, "summitsoftware.com", False)
        self.assertIsNotNone(ev)
        self.assertIn("city", " ".join(ev["why"]))

    def test_domain_created_within_weeks_of_the_filing_is_enough(self):
        rec = {"name": "Quietwater Labs LLC", "trade_name": "", "city": "Hartford", "state": "CT", "address": "",
               "phone": "", "registered": "2026-09-10", "individual": False}
        page = contacts.Page("https://quietwaterlabs.com/", b"<html><head><title>Quietwater Labs</title></head><body>hi</body></html>")
        with mock.patch.object(contacts, "load", return_value=page), \
                mock.patch.object(rdap, "created", return_value=date(2026, 9, 10)):
            ev = website.check_domain(rec, "quietwaterlabs.com", False)
        self.assertIsNotNone(ev)
        self.assertTrue(any("domain registered" in w for w in ev["why"]))


class DiscoveryRecallTests(unittest.TestCase):
    """What the parallel review of 40 'no website found' companies showed: 25 had a site the tool missed."""

    def test_name_variants_the_guesser_used_to_miss(self):
        def sl(name):
            return util.slugs(name)
        self.assertIn("wordlark", sl("Wordlark Language Labs, Inc."))                      # first word
        self.assertIn("larkcamp", sl("Larkcamp Advisory Group, INC."))
        self.assertEqual(sl("Veltra Oy")[0], "veltra")                                  # a foreign company form is not part of the name
        self.assertEqual(sl("Wynlet Limited Liability Company"), ["wynlet"])            # 'Limited Liability Company' is not part of it
        self.assertIn("bxglobaltech", sl("Bx Global Technologies LLC"))                 # Technologies -> tech
        self.assertIn("reefandtide", sl("Reef & Tide Money LLC"))                         # trailing word dropped
        self.assertIn("tmobile", sl("T-Mobile West LLC"))
        self.assertEqual(sl("As You Wish Inc")[0], "asyouwish")                         # 'as' only counts as a suffix at the end
        self.assertEqual(sl("Summit Software LLC")[:2], ["summitsoftware", "summit-software"])   # the likeliest still come first

    def test_more_places_to_look(self):
        cands = [d for d, _ in website.candidates({"name": "Glintlyai, INC.", "trade_name": "", "emails": []})]
        self.assertIn("glintly.ai", cands)                                                # a name ending in 'ai'
        self.assertIn("glintlyai.llc", cands)
        self.assertIn("glintlyai.agency", cands)
        alias = [d for d, _ in website.candidates({"name": "Jon K Nelman Co", "trade_name": "Jon K Nelma Co/nelman Tyler", "emails": []})]
        self.assertIn("nelmantyler.com", alias)                                           # a trade name written as 'A/B'

    def _site(self, pages):
        def load(url, timeout=8):
            path = "/" + url.split("//", 1)[1].split("/", 1)[1] if "/" in url.split("//", 1)[1] else "/"
            html = pages.get(path)
            return contacts.Page(url if path != "/" else "https://wrenkeep.ai/", html.encode()) if html else None
        return mock.patch.object(contacts, "load", side_effect=load)

    REC = {"name": "Wrenkeep LLC", "trade_name": "", "city": "Austin", "state": "TX", "zip": "78731",
           "address": "100 EXAMPLE DR STE 100", "phone": "", "registered": "2026-09-10", "individual": False}

    def test_the_address_on_the_terms_page_proves_a_site_whose_home_page_does_not(self):
        pages = {"/": "<html><head><title>Wrenkeep CRM</title></head><body><h1>The custom CRM for you</h1></body></html>",
                 "/terms": "<html><body>These terms bind Wrenkeep LLC, 100 Example Drive STE 100, Austin, TX 78731.</body></html>"}
        with self._site(pages), mock.patch.object(rdap, "created", return_value=date(2020, 1, 1)):    # an old domain: no date help
            ev = website.check_domain(self.REC, "wrenkeep.ai", False)
        self.assertIsNotNone(ev)
        self.assertIn("terms or contact page", " ".join(ev["why"]))

    def test_the_legal_name_alone_on_a_legal_page_is_not_enough(self):
        pages = {"/": "<html><head><title>Wrenkeep CRM</title></head><body>hello</body></html>",
                 "/terms": "<html><body>Wrenkeep LLC, a company in Reykjavik, Iceland.</body></html>"}
        with self._site(pages), mock.patch.object(rdap, "created", return_value=date(2020, 1, 1)):
            self.assertIsNone(website.check_domain(self.REC, "wrenkeep.ai", False))
        pages["/terms"] = "<html><body>Wrenkeep LLC, Austin, Texas.</body></html>"                 # the legal name AND the city
        with self._site(pages), mock.patch.object(rdap, "created", return_value=date(2020, 1, 1)):
            self.assertIsNotNone(website.check_domain(self.REC, "wrenkeep.ai", False))

    def test_a_domain_that_is_the_exact_name_gets_a_wider_window_after_the_filing_date_matters(self):
        page = contacts.Page("https://wynlet.app/", b"<html><head><title>Wynlet</title></head><body>lease apps</body></html>")
        rec = {"name": "Wynlet LLC", "trade_name": "", "city": "Mesquite", "state": "TX", "zip": "75149", "address": "",
               "phone": "", "registered": "2026-10-01", "individual": False}
        with mock.patch.object(contacts, "load", return_value=page):
            with mock.patch.object(rdap, "created", return_value=date(2026, 8, 12)):           # 50 days before: exact name
                self.assertIsNotNone(website.check_domain(rec, "wynlet.app", False))
            with mock.patch.object(rdap, "created", return_value=date(2026, 3, 1)):            # seven months before: no
                self.assertIsNone(website.check_domain(rec, "wynlet.app", False))
            other = dict(rec, name="Wynlet Labs LLC")                                         # not the exact name: still 21 days
            with mock.patch.object(rdap, "created", return_value=date(2026, 8, 12)):
                self.assertIsNone(website.check_domain(other, "wynlet.app", False))


class WebsiteValueTests(unittest.TestCase):
    SCRAPE = {"ok": True, "emails": [], "phones": [], "people": [], "linkedin_company": "", "head": "", "body": ""}
    REC = {"name": "Acme LLC", "emails": [], "phone": ""}

    def run_with(self, url):
        site = {"url": url, "domain": "acme.io", "why": ["x"], "created": "", "_page": None}
        with mock.patch.object(website, "discover", return_value=site), mock.patch.object(contacts, "scrape", return_value=dict(self.SCRAPE)):
            return enrich.enrich_one(dict(self.REC))

    def test_a_normal_site_is_kept_as_scheme_and_host(self):
        self.assertEqual(self.run_with("https://www.acme.io/about?x=1")["website"], "https://www.acme.io")

    def test_odd_addresses_count_as_no_website(self):
        for bad in ("javascript:alert(1)", "https://acme.io\"onmouseover=\"x", "https://ac me.io/", "ftp://acme.io/", "https://=cmd.io/"):
            self.assertEqual(self.run_with(bad)["website"], "", bad)


class EmailPolicyTests(unittest.TestCase):
    def test_pick(self):
        pick = enrich._pick_email
        # a third-party address on the page is never the company's
        self.assertEqual(pick(["support@hostco-web.net"], [], "summitsoftware.com"), ("", ""))
        self.assertEqual(pick(["x@notsummitsoftware.com"], [], "summitsoftware.com"), ("", ""))
        self.assertEqual(pick(["info@summitsoftware.com", "me@gmail.com"], [], "summitsoftware.com"),
                         ("info@summitsoftware.com", "company website"))
        # a free-mail address counts only when the website itself shows it
        self.assertEqual(pick(["owner@gmail.com"], [], "x.com"), ("owner@gmail.com", "company website"))
        self.assertEqual(pick([], ["owner@gmail.com"], "x.com"), ("", ""))
        self.assertEqual(pick([], ["jordan@x.com"], "x.com"), ("jordan@x.com", "state registry"))

    def test_the_front_door_mailbox_beats_the_support_desk_and_function_mailboxes_are_never_used(self):
        site = ["support@acme.io", "contact@acme.io", "hello@acme.io", "privacy@acme.io", "press@acme.io", "jane@acme.io"]
        self.assertEqual(enrich._pick_email(site, [], "acme.io"), ("hello@acme.io", "company website"))
        self.assertEqual(enrich._pick_email(["support@acme.io", "jane@acme.io"], [], "acme.io")[0], "support@acme.io")
        self.assertEqual(enrich._pick_email(["privacy@acme.io", "legal@acme.io", "careers@acme.io", "contracts@acme.io"], [], "acme.io"), ("", ""))
        self.assertEqual(enrich._pick_email([], ["billing@acme.io"], "acme.io"), ("", ""))

    def test_network_trouble_is_not_a_verdict(self):
        def blip(rec, max_checks=6):
            util.note_net_error(); util.note_net_error(); util.note_net_error()
            return None
        with mock.patch.object(website, "discover", blip), mock.patch.object(util, "reachable", return_value=False):
            with self.assertRaises(util.Transient):                       # the internet really is down
                enrich.enrich_one({"name": "A LLC", "trade_name": ""})
        with mock.patch.object(website, "discover", blip), mock.patch.object(util, "reachable", return_value=True):
            self.assertEqual(enrich.enrich_one({"name": "A LLC", "trade_name": ""})["website"], "")   # dead look-alikes only
        with mock.patch.object(website, "discover", lambda rec, max_checks=6: None), \
                mock.patch.object(util, "reachable", return_value=True):
            self.assertEqual(enrich.enrich_one({"name": "A LLC", "trade_name": ""})["website"], "")   # a plain miss

    def test_no_website_is_never_a_verdict_while_the_internet_is_down(self):
        """A Mac whose lid closed mid-run gets 'no such name' for every name: those are not misses (measured: 790 of 1141
        companies were wrongly judged 'no website' that way)."""
        with mock.patch.object(website, "discover", lambda rec, max_checks=6: None), \
                mock.patch.object(util, "reachable", return_value=False):
            with self.assertRaises(util.Transient):
                enrich.enrich_one({"name": "A LLC", "trade_name": ""})

    def test_a_phone_filed_with_the_city_is_never_used_only_one_the_company_shows(self):
        site = {"url": "https://jd.studio/", "domain": "jd.studio", "why": ["x"], "created": "", "_page": object()}
        sc = {"ok": True, "emails": ["hi@jd.studio"], "phones": [], "people": [], "linkedin_company": "", "head": "software", "body": ""}
        for individual in (True, False):
            rec = {"name": "JD Studio", "trade_name": "", "phone": "2065550100", "individual": individual, "emails": [], "people": []}
            with mock.patch.object(website, "discover", return_value=dict(site)), mock.patch.object(contacts, "scrape", return_value=dict(sc)):
                self.assertEqual(enrich.enrich_one(rec)["phone"], "")             # 3 of 3 checked were the owner's private line
        with mock.patch.object(website, "discover", return_value=dict(site)), \
                mock.patch.object(contacts, "scrape", return_value=dict(sc, phones=["4152100123"])):
            self.assertEqual(enrich.enrich_one(dict(rec))["phone"], "4152100123")  # the number the site shows is used



class EitherContactTests(unittest.TestCase):
    REC = {"id": "ct:1", "source": "ct", "name": "Tarvane LLC", "registered": "2026-09-22", "individual": False, "emails": []}

    def e(self, email="", phone=""):
        return {"website": "https://tarvane.io", "email": email, "phone": phone, "phone_from": "company website" if phone else "",
                "domain": "tarvane.io", "it": {"level": "strong", "score": 9}}

    def test_both_are_needed_unless_one_is_enough(self):
        both = {"require": ["website", "email", "phone"]}
        either = dict(both, contact_either=True)
        self.assertEqual(qualify(self.REC, self.e("hello@tarvane.io"), both), ("held", ["phone"]))
        self.assertEqual(qualify(self.REC, self.e("hello@tarvane.io"), either), ("ready", []))      # an email is enough
        self.assertEqual(qualify(self.REC, self.e("", "2122100200"), either), ("ready", []))        # so is a phone
        self.assertEqual(qualify(self.REC, self.e(), either), ("held", ["email or phone"]))        # but not nothing
        only_email = {"require": ["website", "email"], "contact_either": True}
        self.assertEqual(qualify(self.REC, self.e("", "2122100200"), only_email), ("held", ["email"]))   # 'either' needs both boxes ticked


class EstablishedCompanyTests(unittest.TestCase):
    REC = {"id": "tx:1", "source": "tx", "name": "MindGauge Inc", "registered": "2026-09-22", "individual": False, "emails": []}

    def e(self, created, domain="mindgauge.com"):
        return {"website": f"https://{domain}", "email": f"support@{domain}", "phone": "7373941911", "domain": domain,
                "created": created, "it": {"level": "strong", "score": 9}}

    def test_off_by_default_the_company_is_listed(self):
        self.assertEqual(qualify(self.REC, self.e("2001-08-05"), {"require": ["website", "email", "phone"]})[0], "ready")

    def test_when_on_a_website_years_older_than_the_filing_is_skipped(self):
        cfg = {"require": ["website", "email", "phone"], "skip_established": True}
        state, why = qualify(self.REC, self.e("2001-08-05"), cfg)
        self.assertEqual(state, "dropped")
        self.assertIn("established company (website since 2001)", why[0])
        self.assertEqual(qualify(self.REC, self.e("2026-08-20"), cfg)[0], "ready")      # a site made weeks before the filing
        self.assertEqual(qualify(self.REC, self.e("2024-06-01"), cfg)[0], "ready")      # two years: still counts as new
        self.assertEqual(qualify(self.REC, self.e(""), cfg)[0], "ready")                # age unknown: never guessed

    def test_a_city_licence_phone_does_not_count_even_in_results_stored_earlier(self):
        e = dict(self.e("2026-08-20", "new.example"), phone_from="city licence")
        cfg = {"require": ["website", "email", "phone"]}
        self.assertEqual(qualify(self.REC, e, cfg), ("held", ["phone"]))
        self.assertEqual(qualify(self.REC, dict(e, phone_from="company website"), cfg)[0], "ready")   # the site itself shows it

    def test_turning_the_rule_on_also_pulls_companies_not_yet_sent_and_turning_it_off_brings_them_back(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d) / "t.db")
            day = date(2026, 10, 6)
            for cid, created, domain in (("tx:1", "2001-08-05", "old.example"), ("tx:2", "2026-08-20", "new.example")):
                s.add(dict(self.REC, id=cid), day)
                s.record_attempt(cid, self.e(created, domain), "ready", day)
            on = {"require": ["website", "email", "phone"], "skip_established": True}
            off = dict(on, skip_established=False)
            pipeline.requalify(s, on, day)
            self.assertEqual(sorted(i["id"] for i in s.ready()), ["tx:2"])               # the established one left the list
            self.assertEqual(s.counts()["dropped"], 1)
            self.assertEqual(pipeline.requalify(s, off, day), 1)                         # and it returns when the rule is off
            self.assertEqual(sorted(i["id"] for i in s.ready()), ["tx:1", "tx:2"])
            s.close()


class QualifyTests(unittest.TestCase):
    cfg = {"require": ["website", "email", "phone"], "require_it_signal": True}

    def e(self, **kw):
        base = {"website": "https://a.com", "email": "i@a.com", "phone": "2062100100", "it": {"level": "strong"}}
        base.update(kw)
        return base

    def test_rules(self):
        self.assertEqual(qualify({}, self.e(), self.cfg), ("ready", []))
        self.assertEqual(qualify({}, self.e(phone=""), self.cfg), ("held", ["phone"]))
        self.assertEqual(qualify({}, self.e(website=""), self.cfg)[0], "held")
        self.assertEqual(qualify({}, self.e(it={"level": "none"}), self.cfg)[0], "dropped")
        self.assertEqual(qualify({}, self.e(it={"level": "unknown"}), self.cfg)[0], "ready")
        relaxed = dict(self.cfg, require=["website", "email"])
        self.assertEqual(qualify({}, self.e(phone=""), relaxed)[0], "ready")


class StoreTests(unittest.TestCase):
    def test_retry_schedule_and_dedupe(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d) / "t.db")
            day0 = date(2026, 10, 1)
            rec = {"id": "tx:1", "source": "tx", "name": "A"}
            self.assertTrue(s.add(rec, day0))
            self.assertFalse(s.add(rec, day0))
            self.assertEqual(len(s.due(day0)), 1)
            s.record_attempt("tx:1", {"domain": ""}, "held", day0)
            self.assertEqual(len(s.due(day0)), 0)
            self.assertEqual(len(s.due(day0 + timedelta(days=RETRY_DAYS[1]))), 1)
            for i in range(1, len(RETRY_DAYS) + 1):
                s.record_attempt("tx:1", {"domain": ""}, "held", day0 + timedelta(days=RETRY_DAYS[min(i, 3)]))
            self.assertEqual(s.counts()["dropped"], 1)

    def test_domain_owner(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d) / "t.db")
            for i in (1, 2):
                s.add({"id": f"sf:{i}", "source": "sf"}, date(2026, 10, 1))
            s.record_attempt("sf:1", {"domain": "acme.com"}, "ready", date(2026, 10, 1))
            self.assertEqual(s.domain_owner("acme.com", "sf:2"), "sf:1")
            self.assertIsNone(s.domain_owner("acme.com", "sf:1"))

    def test_export_version_changes_with_the_list(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d) / "t.db")
            empty = s.export_version()
            s.add({"id": "ct:1", "source": "ct", "registered": "2026-10-01"}, date(2026, 10, 1))
            s.record_attempt("ct:1", {"website": "https://a.com", "email": "x@a.com", "phone": "5125550100"}, "ready", date(2026, 10, 1))
            one = s.export_version()
            self.assertNotEqual(empty, one)
            self.assertEqual(s.export_version(), one)                      # stable while nothing changes
            s.mark_pushed("ct:1", "hash", date(2026, 10, 2))
            self.assertNotEqual(s.export_version(), one)
            s.close()

    def test_a_new_sheet_makes_everything_waiting_again(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d) / "t.db")
            for i in (1, 2):
                s.add({"id": f"ct:{i}", "source": "ct", "registered": "2026-10-01"}, date(2026, 10, 1))
                s.record_attempt(f"ct:{i}", {"website": "https://a.com"}, "ready", date(2026, 10, 1))
            s.mark_pushed("ct:1", "hash", date(2026, 10, 2))
            self.assertEqual(len(s.ready()), 1)
            self.assertEqual(s.reset_pushed(), 1)
            self.assertEqual(len(s.ready()), 2)
            self.assertEqual(s.counts()["pushed"], 0)
            s.close()

    def test_strikes_count_network_trouble_and_reset_on_an_answer(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d) / "t.db")
            s.add({"id": "ct:1", "source": "ct"}, date(2026, 10, 1))
            self.assertEqual([s.add_strike("ct:1"), s.add_strike("ct:1")], [1, 2])
            s.record_attempt("ct:1", {"domain": ""}, "held", date(2026, 10, 1))
            self.assertEqual(s.add_strike("ct:1"), 1)
            s.close()

    def test_a_database_from_an_older_version_gets_the_new_columns_even_when_two_open_it_at_once(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.db"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE companies (id TEXT PRIMARY KEY, source TEXT NOT NULL, raw TEXT NOT NULL, enrich TEXT, "
                       "state TEXT NOT NULL DEFAULT 'new', first_seen TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
                       "next_due TEXT, domain TEXT, pushed_hash TEXT, pushed_at TEXT)")
            db.commit()
            db.close()
            errors = []
            def open_it():
                try:
                    Store(path).close()
                except Exception as e:                       # noqa: BLE001
                    errors.append(e)
            import threading
            ts = [threading.Thread(target=open_it) for _ in range(6)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            self.assertEqual(errors, [])
            cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(companies)")}
            self.assertTrue({"ready_at", "strikes"} <= cols)

    def test_waiting_breakdown(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d) / "t.db")
            for i in (1, 2, 3):
                s.add({"id": f"ct:{i}", "source": "ct"}, date(2026, 10, 1))
            s.record_attempt("ct:1", {"website": "https://a.com"}, "held", date(2026, 10, 1))
            s.record_attempt("ct:2", {"website": ""}, "held", date(2026, 10, 1))
            self.assertEqual(s.held_breakdown(), {"not_looked_up": 1, "no_website": 1, "missing_contact": 1})


class LookupStageTests(unittest.TestCase):
    """What happens to a company when its lookup crashes, or keeps running into network trouble."""
    REC = {"id": "ct:1", "source": "ct", "name": "Acme LLC", "registered": "2026-10-01", "individual": False, "emails": []}
    CFG = {"workers": 2, "require": ["website", "email", "phone"], "require_it_signal": True}

    def stage(self, side_effect, times=1, internet=True):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d) / "t.db")
            s.add(dict(self.REC), date(2026, 10, 1))
            out = []
            for _ in range(times):
                lines = []
                with mock.patch.object(enrich, "enrich_one", side_effect=side_effect), \
                        mock.patch.object(util, "reachable", return_value=internet):
                    stats = pipeline.enrich_pending(s, self.CFG, date(2026, 10, 1) + timedelta(days=40), lines.append)
                out.append((stats, lines))
            row = s.db.execute("SELECT state, attempts, strikes, enrich FROM companies").fetchone()
            s.close()
            return out, dict(row)

    def test_a_crash_is_logged_and_costs_an_attempt_instead_of_repeating_forever(self):
        out, row = self.stage(IndexError("tuple index out of range"))
        stats, lines = out[0]
        self.assertEqual((stats["crashed"], stats["checked"], stats["held"]), (1, 1, 1))
        self.assertTrue(any("ct:1" in l and "IndexError" in l for l in lines))
        self.assertEqual((row["state"], row["attempts"]), ("held", 1))
        self.assertIn("lookup error", row["enrich"])

    def test_network_trouble_is_retried_a_few_times_then_judged(self):
        out, row = self.stage(util.Transient("dead host"), times=pipeline.TRANSIENT_LIMIT)
        first, last = out[0][0], out[-1][0]
        self.assertEqual((first["errors"], first["checked"]), (1, 0))             # not judged yet, no attempt used
        self.assertEqual((last["checked"], last["held"]), (1, 1))                  # the third time it is judged as it stands
        self.assertEqual((row["state"], row["attempts"]), ("held", 1))

    def test_while_the_internet_is_down_nothing_is_ever_judged_however_often_it_is_tried(self):
        out, row = self.stage(util.Transient("no internet"), times=pipeline.TRANSIENT_LIMIT + 3, internet=False)
        self.assertTrue(all(stats["checked"] == 0 and stats["errors"] == 1 for stats, _ in out))
        self.assertEqual((row["state"], row["attempts"], row["strikes"]), ("new", 0, 0))       # untouched, no strike either

    def test_rules_that_change_release_companies_already_held(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d) / "t.db")
            day = date(2026, 10, 1)
            found = {"website": "https://acme.io", "email": "hi@acme.io", "phone": "", "domain": "acme.io",
                     "it": {"level": "strong", "score": 9}}
            s.add(dict(self.REC), day)
            s.record_attempt("ct:1", dict(found, missing=["phone"]), "held", day)
            s.add(dict(self.REC, id="ct:2"), day)                                  # same website as the first: stays out
            s.record_attempt("ct:2", dict(found, missing=["phone"]), "held", day)
            s.add(dict(self.REC, id="ct:3"), day)                                  # no website at all: stays out
            s.record_attempt("ct:3", dict(found, website="", domain="", missing=["website"]), "held", day)
            strict = dict(self.CFG)
            relaxed = dict(self.CFG, require=["website", "email"])
            self.assertEqual(pipeline.requalify(s, strict, day), 0)
            self.assertEqual(pipeline.requalify(s, relaxed, day), 1)
            self.assertEqual(s.counts()["ready"], 1)
            ready = s.db.execute("SELECT id FROM companies WHERE state='ready'").fetchone()["id"]
            self.assertIn(ready, ("ct:1", "ct:2"))
            self.assertEqual(s.db.execute("SELECT ready_at FROM companies WHERE id=?", (ready,)).fetchone()[0], "2026-10-01")
            s.close()

    def test_a_company_dropped_for_having_no_it_work_comes_back_when_that_rule_is_turned_off(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d) / "t.db")
            day = date(2026, 10, 1)
            s.add(dict(self.REC), day)
            s.record_attempt("ct:1", {"website": "https://acme.io", "email": "hi@acme.io", "phone": "5122100100",
                                      "domain": "acme.io", "it": {"level": "none", "score": 0},
                                      "dropped_because": ["no sign of IT work on the website"]}, "dropped", day)
            self.assertEqual(pipeline.requalify(s, self.CFG, day), 0)
            self.assertEqual(pipeline.requalify(s, dict(self.CFG, require_it_signal=False), day), 1)
            s.close()


class PipelineTests(unittest.TestCase):
    cfg = {"backfill_days": 45}

    def test_each_source_has_its_own_window(self):
        today = date(2026, 10, 5)
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d) / "t.db")
            tx, ct = registries.Texas(), registries.Connecticut()
            self.assertEqual(pipeline.source_window(self.cfg, s, tx, today), today - timedelta(days=45))
            s.set_source_ok("tx", date(2026, 10, 4))
            s.set_source_ok("ct", date(2026, 10, 4))
            self.assertEqual(pipeline.source_window(self.cfg, s, tx, today), date(2026, 9, 13))   # last good fetch minus the batch lag
            self.assertEqual(pipeline.source_window(self.cfg, s, ct, today), date(2026, 9, 27))
            # a source that failed for ten days keeps its old date, so its window reaches back to it
            s.set_source_ok("ct", date(2026, 9, 25))
            self.assertEqual(pipeline.source_window(self.cfg, s, ct, today), date(2026, 9, 18))
            # never further back than the backfill limit, and --since always wins
            s.set_source_ok("ct", date(2026, 6, 1))
            self.assertEqual(pipeline.source_window(self.cfg, s, ct, today), today - timedelta(days=45))
            self.assertEqual(pipeline.source_window(self.cfg, s, ct, today, date(2026, 9, 1)), date(2026, 9, 1))


class SheetRowTests(unittest.TestCase):
    def test_fit_says_when_the_website_is_much_older_than_the_filing(self):
        rec = {"id": "tx:1", "source": "tx", "name": "A LLC", "registered": "2026-10-01"}
        def fit(created, level="strong"):
            return sheet._fit(rec, {"created": created, "it": {"level": level}})
        self.assertEqual(fit("2001-08-05"), "Confirmed · site since 2001")        # an established company with a new permit
        self.assertEqual(fit("2026-08-20"), "Confirmed")                            # a site made weeks before the filing
        self.assertEqual(fit("2024-02-01"), "Confirmed")                            # under three years: not flagged
        self.assertEqual(fit(""), "Confirmed")                                      # no date known: nothing claimed
        self.assertEqual(fit("2001-08-05", "weak"), "Likely · site since 2001")
        self.assertEqual(fit("not a date"), "Confirmed")

    def item(self, **rec_over):
        rec = {"id": "ct:1", "source": "ct", "name": "Corvane LLC", "trade_name": "Corvane", "address": "40 Example Ave",
               "city": "Old Greenwich", "state": "CT", "zip": "06870", "registered": "2026-10-04", "industry": "X",
               "individual": False}
        rec.update(rec_over)
        return {"raw": rec,
                "enrich": {"website": "https://corvane.io", "email": "j@corvane.io", "email_from": "state registry", "emails": [],
                           "phone": "2035550100", "phone_from": "company website", "why": ["a"], "created": "2026-10-01",
                           "contact": {"name": "Jordan S", "title": "", "email": "", "linkedin": ""},
                           "linkedin": "", "linkedin_company": "https://linkedin.com/company/corvane", "it": {"level": "weak"}}}

    def test_row(self):
        r = sheet.sheet_row(self.item())
        self.assertEqual(r["company"], "Corvane LLC (Corvane)")
        self.assertEqual(r["phone"], "(203) 555-0100")
        self.assertEqual(r["address"], "40 Example Ave, Old Greenwich, CT 06870")
        self.assertEqual(r["fit"], "Likely")
        self.assertEqual(r["linkedin_label"], "Company")

    def test_individuals_get_city_only_and_bad_dates_vanish(self):
        r = sheet.sheet_row(self.item(individual=True, registered="N/A"))
        self.assertEqual(r["address"], "Old Greenwich, CT")
        self.assertEqual(r["registered"], "")

    def test_verified_by_says_where_each_value_came_from_and_that_a_filing_address_is_on_the_websites_domain(self):
        r = sheet.sheet_row(self.item())
        self.assertIn("Email: state registry (same domain as the website)", r["proof"])
        self.assertIn("Phone: company website", r["proof"])
        item = self.item()
        item["enrich"]["domain"] = "corvane.io"
        item["enrich"]["email"] = "hello@mail.corvane.io"
        self.assertIn("Email: state registry (same domain as the website)", sheet.sheet_row(item)["proof"])    # a subdomain is the same site
        item["enrich"]["email"] = "j@corvane-llc.com"
        self.assertIn("Email: state registry (domain differs from the website)", sheet.sheet_row(item)["proof"])
        item["enrich"].update(email="j@corvane.io", email_from="company website", phone_from="company website (privacy or terms page only)")
        proof = sheet.sheet_row(item)["proof"]
        self.assertIn("Email: company website", proof)
        self.assertNotIn("same domain", proof)
        self.assertIn("Phone: company website (privacy or terms page only)", proof)


class BridgeClientTests(unittest.TestCase):
    class Resp:
        def __init__(self, text, status=200):
            self.text, self.status_code = text, status

        def json(self):
            import json
            return json.loads(self.text)

    def test_retries_html_then_succeeds(self):
        b = sheet.Bridge("https://script.google.com/macros/s/X/exec", "t")
        seq = [self.Resp("<html>oops</html>", 500), self.Resp("<html>oops</html>", 500),
               self.Resp('{"ok": true, "version": %d}' % sheet.EXPECTED_VERSION)]
        with mock.patch("requests.post", side_effect=seq), mock.patch("time.sleep"):
            self.assertTrue(b.ping()["ok"])

    def test_html_forever_is_explained_without_leaking_the_url(self):
        b = sheet.Bridge("https://script.google.com/macros/s/SECRET/exec", "t")
        with mock.patch("requests.post", return_value=self.Resp("<html>x</html>", 403)), mock.patch("time.sleep"):
            with self.assertRaises(sheet.BridgeError) as cm:
                b.ping()
        self.assertIn("Anyone", str(cm.exception))
        self.assertNotIn("SECRET", str(cm.exception))

    def test_old_script_version_is_caught_on_every_call(self):
        b = sheet.Bridge("https://script.google.com/macros/s/X/exec", "t")
        with mock.patch("requests.post", return_value=self.Resp('{"ok": true, "version": 1}')):
            with self.assertRaises(sheet.BridgeError) as cm:
                b.upsert([], "2026-10-05")
        self.assertIn("New version", str(cm.exception))

    def test_wrong_token_points_to_update_script(self):
        b = sheet.Bridge("https://script.google.com/macros/s/X/exec", "t")
        with mock.patch("requests.post", return_value=self.Resp('{"ok": false, "error": "Wrong token. Run setup"}')):
            with self.assertRaises(sheet.BridgeError) as cm:
                b.ping()
        self.assertIn("Open Settings", str(cm.exception))

    def test_a_cut_off_answer_is_retried_then_explained(self):
        b = sheet.Bridge("https://script.google.com/macros/s/X/exec", "t")
        cut = self.Resp('{"ok": tru')
        with mock.patch("requests.post", return_value=cut) as post, mock.patch("time.sleep"):
            with self.assertRaises(sheet.BridgeError) as cm:
                b.ping()
        self.assertEqual(post.call_count, 3)
        self.assertIn("could not be read", str(cm.exception))
        seq = [cut, self.Resp('{"ok": true, "version": %d}' % sheet.EXPECTED_VERSION)]
        with mock.patch("requests.post", side_effect=seq), mock.patch("time.sleep"):
            self.assertTrue(b.ping()["ok"])

    def test_one_try_when_a_person_is_waiting(self):
        b = sheet.Bridge("https://script.google.com/macros/s/X/exec", "t", retries=1)
        with mock.patch("requests.post", return_value=self.Resp("<html>x</html>", 500)) as post, mock.patch("time.sleep"):
            with self.assertRaises(sheet.BridgeError):
                b.ping()
        self.assertEqual(post.call_count, 1)

    def test_no_internet_message_is_plain(self):
        import requests
        b = sheet.Bridge("https://script.google.com/macros/s/SECRET/exec", "t")
        with mock.patch("requests.post", side_effect=requests.ConnectionError("HTTPSConnectionPool(host='script.google.com')")), \
                mock.patch("time.sleep"):
            with self.assertRaises(sheet.BridgeError) as cm:
                b.ping()
        self.assertNotIn("SECRET", str(cm.exception))
        self.assertIn("internet", str(cm.exception))


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._saved = (config.ROOT, config.DATA, config.LOGS, config.CONFIG_PATH)
        config.ROOT, config.DATA, config.LOGS, config.CONFIG_PATH = root, root / "data", root / "logs", root / "config.json"
        self.which = mock.patch("shutil.which", return_value="/bin/launchctl")
        self.which.start()

    def tearDown(self):
        self.which.stop()
        config.ROOT, config.DATA, config.LOGS, config.CONFIG_PATH = self._saved
        self._tmp.cleanup()

    def run_with(self, plist, fake_run):
        with mock.patch.object(service, "PLIST", plist), mock.patch("subprocess.run", fake_run):
            return service.install()

    def test_a_failed_reinstall_puts_the_working_service_back_and_says_so(self):
        plist = config.ROOT / "com.itleads.web.plist"
        plist.write_bytes(b"OLD")
        seen = []
        def fake_run(cmd, **kw):
            seen.append(cmd[1])
            bad = cmd[1] == "bootstrap" and seen.count("bootstrap") == 1
            return subprocess.CompletedProcess(cmd, 5 if bad else 0, "", "Bootstrap failed: 5: Input/output error" if bad else "")
        with self.assertRaises(RuntimeError) as cm:
            self.run_with(plist, fake_run)
        self.assertEqual(plist.read_bytes(), b"OLD")                              # the previous file is back
        self.assertEqual(seen.count("bootstrap"), 2)                              # and it was loaded again
        self.assertIn("previous service was loaded again", str(cm.exception))
        self.assertIn("Login Items", str(cm.exception))                           # the usual cause, in plain words
        self.assertIn("enable", seen)                                             # a switched-off service is switched on

    def test_if_the_old_service_cannot_come_back_that_is_said_too(self):
        plist = config.ROOT / "com.itleads.web.plist"
        plist.write_bytes(b"OLD")
        fail = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1 if cmd[1] == "bootstrap" else 0, "", "nope")
        with self.assertRaises(RuntimeError) as cm:
            self.run_with(plist, fail)
        self.assertIn("could not be loaded again either", str(cm.exception))

    def test_a_failed_first_install_leaves_no_file_behind(self):
        plist = config.ROOT / "com.itleads.web.plist"
        fail = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1 if cmd[1] == "bootstrap" else 0, "", "nope")
        with self.assertRaises(RuntimeError):
            self.run_with(plist, fail)
        self.assertFalse(plist.exists())

    def test_the_working_file_is_never_cut_short_if_building_the_new_one_fails(self):
        plist = config.ROOT / "com.itleads.web.plist"
        plist.write_bytes(b"OLD")
        with mock.patch.object(service, "build_plist", side_effect=RuntimeError("cannot build")):
            with self.assertRaises(RuntimeError):
                self.run_with(plist, lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))
        self.assertEqual(plist.read_bytes(), b"OLD")

    def test_without_launchctl_there_is_no_service_and_nothing_crashes(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertFalse(service.available())
            self.assertFalse(service.installed())
            service.uninstall()                                                   # a no-op
            with self.assertRaises(RuntimeError):
                service.install()

    def test_plist_is_valid_restarts_after_a_crash_and_carries_what_was_given_at_install(self):
        with mock.patch.dict("os.environ", {"ITLEADS_PORT": "8800", "ITLEADS_SECURE_COOKIES": "1"}, clear=False):
            data = service.build_plist()
        self.assertTrue(data["RunAtLoad"])
        self.assertEqual(data["KeepAlive"], {"SuccessfulExit": False})            # restart after a crash, not after "already running"
        self.assertNotIn("ProcessType", data)                      # "Background" throttles CPU and disk (measured 3-10x slower)
        self.assertEqual(data["ProgramArguments"][1:], ["-m", "itleads", "serve"])
        self.assertEqual(data["EnvironmentVariables"]["ITLEADS_PORT"], "8800")
        self.assertEqual(data["EnvironmentVariables"]["ITLEADS_SECURE_COOKIES"], "1")
        self.assertNotIn("ITLEADS_HOST", data["EnvironmentVariables"])
        with tempfile.NamedTemporaryFile(suffix=".plist", delete=False) as f:
            plistlib.dump(data, f)
        r = subprocess.run(["plutil", "-lint", f.name], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


# ---------------------------------------------------------------------------------------------------------------------
# Corpora for the name and title checks. Every string is generic or invented: headings and buttons found on any site, and
# first names and surnames put together by arithmetic (no real person is meant).
JUNK_STRINGS = {
    "headings / nav": (
        "Meet the Team|Meet The Team|Our Story|Our Team|Contact Us|About Us|Who We Are|What We Do|Our Mission|Our Values|"
        "Our Vision|Our History|Our Approach|Our Process|Our Services|Our Work|Our Clients|Our Partners|Leadership Team|"
        "Management Team|Executive Team|Board Members|Advisory Board|Board of Directors|Meet Our Team|Meet The Founder|"
        "Meet the Founders|The Founder|The Founders|Founder Story|Company Overview|Company Profile|Company Culture|"
        "Corporate Responsibility|Case Studies|White Papers|Help Center|Knowledge Base|Terms Of Service|"
        "Terms and Conditions|Privacy Policy|Cookie Settings|Cookie Policy|Site Map|Quick Links|Useful Links|Follow Us|"
        "Get In Touch|Reach Out|Say Hello|Talk To Us|Work With Us|Join Our Team|Join Us Today|Open Positions|"
        "Current Openings|Life At Acme|Why Choose Us|How It Works|How We Work|Frequently Asked Questions|Latest News|"
        "Recent Posts|Related Articles|Featured Projects|Our Portfolio|Our Expertise|Industries We Serve|Core Values|"
        "Business Hours|Opening Hours|Office Locations|Find Us|Visit Us|Call Us|Email Us|Write To Us|Send Message|"
        "Subscribe Now|Stay Connected|Back To Top|Skip To Content|Main Menu|Search Site"),
    "buttons / CTAs": (
        "Learn More|Read More|View All|Get Started|Book Now|Book A Call|Book A Demo|Request Demo|Request A Quote|"
        "Get A Quote|Free Trial|Start Free Trial|Sign Up|Sign In|Log In|Log Out|Create Account|Add To Cart|Buy Now|"
        "Shop Now|Order Today|Call Now|Contact Sales|Talk To Sales|Schedule Consultation|Free Consultation|See Pricing|"
        "View Pricing|Download Now|Download Brochure|Watch Video|Play Video|Submit Form|Send Message|Apply Now|"
        "View Details|Show More|Load More|Next Page|Previous Page|Accept All|Decline All|Close Menu|Open Menu|Go Back|"
        "Try It Free|See How|Find Out More|Discover More|Explore Platform|Explore Solutions|Get Support|Get Help|"
        "Chat With Us|Live Chat|Order Status|Track Order|Ask Question"),
    "products / services": (
        "Cloud Backup|Managed Services|Managed IT|Cloud Migration|Cyber Security|Network Security|Data Analytics|"
        "Web Design|Web Development|Digital Marketing|Social Media|Search Engine Optimization|Mobile Apps|"
        "Custom Software|Business Intelligence|Machine Learning|Artificial Intelligence|Smart Home|Quick Start|Help Desk|"
        "Disaster Recovery|Data Recovery|Virtual Office|Remote Support|Voice Over IP|Video Conferencing|Cloud Hosting|"
        "Web Hosting|Domain Names|Email Marketing|Content Marketing|Brand Strategy|Product Design|User Experience|"
        "Quality Assurance|Project Management|Systems Integration|Penetration Testing|Compliance Audit|Risk Assessment|"
        "Staff Augmentation|Software Testing|Cloud Computing|Edge Computing|Quantum Computing|Blockchain Solutions|"
        "Internet Of Things|Digital Transformation|Enterprise Architecture|Technical Support|Customer Success|"
        "Supply Chain|Payroll Services|Tax Preparation|Real Estate|Home Inspection|Roof Repair|Pest Control|Dental Care|"
        "Pet Grooming|Auto Repair|Yoga Studio|Coffee Roasters|Craft Brewery|Food Truck"),
    "company-ish names": (
        "Jane Doe Photography|John Smith Consulting|Acme Widgets|Johnson Brothers Plumbing|Blue Ocean|Red Rock|"
        "Silver Lining|Golden Gate|North Star|Bright Future|Quantum Leap|Iron Mountain|Green Valley|Sun Valley|Lone Star|"
        "Big Sky|Blue Sky|Black Rock|White Oak|Cedar Ridge|Pine Hill|Maple Grove|Lake View|Ocean Breeze|Summit Peak|"
        "Eagle Rock|Falcon Ridge|Phoenix Rising|Tiger Team|Red Dragon|Alpha Omega|Delta Force|Omega Point|Apex Predator|"
        "Zenith Peak|Nova Labs|Vertex Works|Pixel Perfect|Code Ninja|Tech Savvy|Byte Size|Data Driven|Cloud Nine|"
        "Smart Move|Next Level|First Choice|Prime Time|Top Notch|Best Buy|Fast Track|Open Door|Clear Vision|True North|"
        "Fresh Start|New Horizon|Bold Move|Pure Genius|Rapid Growth"),
    "article titles / sentences": (
        "How To Grow|Why We Started|Five Ways To Save|Ten Tips For Success|What Is SEO|Top Trends 2025|Year In Review|"
        "Spring Sale Ends|Black Friday Deals|Holiday Hours|Weekly Newsletter|Monthly Update|Annual Report|"
        "Quarterly Results|Customer Stories|Success Stories|Client Testimonials|Happy Customers|Five Star Reviews|"
        "Award Winning|Best Practices|Industry Insights|Thought Leadership|Press Releases|In The News|As Seen On|"
        "Trusted By|Powered By|Designed By|Built With Love|Made In America|Proudly Serving|Since Nineteen Ninety|"
        "Family Owned|Locally Owned|Veteran Owned|Woman Owned|Minority Owned|Fully Insured|Licensed Bonded|"
        "Free Estimates|Call Today|Open Daily|Closed Sundays|Walk Ins Welcome|By Appointment|Gift Cards|Terms Apply|"
        "All Rights Reserved|Copyright Notice|Site Credits"),
    "ALL CAPS junk": (
        "OUR TEAM|CONTACT US|ABOUT US|MEET THE TEAM|OUR STORY|GET STARTED|LEARN MORE|READ MORE|BOOK NOW|CALL TODAY|"
        "CHIEF EXECUTIVE OFFICER|MANAGING DIRECTOR|SIGN UP NOW|PRIVACY POLICY|TERMS OF USE|COOKIE POLICY|FREE QUOTE|"
        "WEB DESIGN|CLOUD HOSTING|DATA CENTER|HELP DESK|NEW YORK|LOS ANGELES|SAN FRANCISCO|CUSTOMER SERVICE|"
        "HUMAN RESOURCES|BUSINESS DEVELOPMENT|ACME CORP|SMITH AND SONS|NORTH AMERICA|UNITED STATES|ALL RIGHTS RESERVED|"
        "FOLLOW US|OUR PARTNERS|MEET OUR TEAM|WHAT WE DO|WHO WE ARE|OUR VALUES|OUR CLIENTS"),
    "email / URL / odd text": (
        "info@acme.com|john.smith@acme.com|Email: info|www.acme.com|https://acme.com|john@acme.com Founder|Name Surname|"
        "First Last|Your Name|Full Name|Name Here|Lorem Ipsum|Dolor Sit|John Doe|Jane Doe|Jane Q Public|Test User|"
        "Sample Name|Placeholder Name|Firstname Lastname|Insert Name|Add Name|Tbd Tbd|Name Lastname|Foo Bar|Mickey Mouse|"
        "Donald Duck|Santa Claus|Click Here|Tap Here|Swipe Left|Scroll Down|Page 1 of 2|Step 1: Start|Q1 2025|"
        "Version 2.0|Phone: 555|Fax Number|Toll Free|Main Office|Head Office|Regional Office|West Coast|East Coast|"
        "New England|Gulf Coast|Bay Area|Silicon Valley|Wall Street|Main Street|Park Avenue|Elm Street|Oak Lane|"
        "Mill Road|Church Street|High Street|First Avenue|Second Street"),
    "role / label / acronym text": (
        "Chief Executive|Executive Director|Managing Partner|General Manager|Sales Manager|Office Manager|"
        "Project Manager|Account Manager|Product Owner|Business Owner|Home Owner|Property Owner|Brand Ambassador|"
        "Customer Support|Technical Lead|Team Lead|Senior Developer|Junior Developer|Software Engineer|Full Stack|"
        "Front End|Back End|Dev Ops|Site Reliability|Data Scientist|Data Engineer|Cloud Architect|Security Analyst|"
        "Help Desk Technician|Network Administrator|Systems Administrator|Database Administrator|Chief Of Staff|"
        "Vice President|Senior Vice President|Chief Technology Officer|Chief Operating Officer|Chief Financial Officer|"
        "Chief Marketing Officer|Co Founder|Co-Founder|Founder CEO|CEO Founder|President CEO|Owner Operator|"
        "Owner Director|Founder Principal|Principal Consultant|Lead Architect|AWS Partner|Microsoft Gold|Google Cloud|"
        "Amazon Web|Salesforce Admin|Oracle Certified|Cisco Certified|CompTIA Plus|ISO Certified|BBB Accredited|"
        "BBB National Programs|Better Business Bureau|Trustpilot Reviews|Google Reviews|Yelp Reviews|Facebook Page|"
        "LinkedIn Profile|Twitter Feed|Instagram Gallery|YouTube Channel|Owner Summary|Jobs Pipeline|CFO Questionnaire|"
        "CEO Questionnaire|Explore Platform|A Note|Note From CEO|Letter From Founder|Message From President|"
        "Welcome Message|Founder Note|Owner Assigned|Next Action|Action Items|Lead Capture|Lead Gen|Lead Pipeline|"
        "Sales Pipeline"),
}
JUNK = [s for group in JUNK_STRINGS.values() for s in group.split("|")]
# company-style names with nothing in them that tells them from a person's ("River Bend"): the one kind the check cannot catch
KNOWN_MISSES = {"River Bend"}

FIRSTS = ("Alan Beth Carl Dana Eric Fiona Glen Hana Ivan Jill Kyle Lena Mark Nina Owen Pam Quinn Ray Sara Todd Uma Vince Wendy "
          "Yusuf Zoe Aaron Bella Colin Diane Edgar Faith Grant Holly Isaac June Keith Laura Miles Nora Oscar Paula Rhett Stella "
          "Trent Vera Wade Xena Yvonne Zane").split()
LASTS = ("Abbott Barlow Carver Dalton Ellison Fairfax Garrity Hollis Ingram Jarvis Kessler Lowell Mercer Norwood Overton Pruitt "
         "Quigley Radcliffe Sutter Thorne Underhill Vance Whitlock Yardley Zimmer Ashby Brandt Calloway Draper Eastman Fowler "
         "Granger Hartwell Ivers Jessup Kirkland Langford Maddox Newcombe Oakley Prescott Rayburn Sawyer Tolliver Upton Vickers "
         "Wexler Yates Okafor Nguyen Petrov Tanaka Haddad Iyer Kowalski Moreno Lindqvist Bianchi").split()
# surnames that are also business or place words: every one must still be a person (the check measured 0 wrongly rejected)
WORD_SURNAMES = ("Page Bank Hall Stack Hunter Cook Lane Church Day Cross Field Wood Stone Black Gold Mills Law Hope Love Little "
                 "Long Short Pool Strong Young Mason Porter").split()
NAMES = [f"{FIRSTS[i % len(FIRSTS)]} {LASTS[(i * 7 + i // len(FIRSTS)) % len(LASTS)]}" for i in range(260)]
NAMES += [f"{g} {s}" for s in WORD_SURNAMES for g in ("Sarah", "Tom", "Greg", "Nina")]                       # 108
NAMES += [f"{FIRSTS[i]} {'ABCDEFGHJKLMNPRSTW'[i % 18]}. {LASTS[i + 3]}" for i in range(20)]                  # middle initials
NAMES += [f"{FIRSTS[i]} {LASTS[i + 10]} {('Jr', 'Sr', 'II', 'III')[i % 4]}" for i in range(8)]               # suffixes
NAMES += [f"{FIRSTS[i].upper()} {LASTS[i + 20].upper()}" for i in range(10)]                                  # ALL CAPS
NAMES += [f"Dr. {FIRSTS[i]} {LASTS[i + 30]}" for i in range(6)]                                               # honorific
NAMES += ["Sean O'Neil", "Maura O'Dell", "Dina D'Alba", "Fiona MacRae", "Rory McKee", "Anne-Marie Dupree", "Jean-Paul Ferrand",
          "Emma Smith-Jones", "Carlos Perez-Castro", "Hendrik van der Berg", "Ludwig von Altenburg", "Giulia di Marco",
          "Maria de la Cruz", "Ana Maria de la Torre", "Maria de los Angeles Perez", "Luis Fernando de la Cruz",
          "Lucia de las Heras", "Pedro y Pablo Ortega", "Marco della Valle", "Omar bin Rashid", "Layla Al-Hakim", "Ahmed al Sayed"]
assert len(NAMES) >= 400 and len(JUNK) >= 480


class NameCorpusTests(unittest.TestCase):
    """The name check on 480+ junk strings and 400+ names, measured together: what it lets in and what it wrongly refuses."""

    def test_junk_corpus_is_refused(self):
        taken = [s for s in JUNK if contacts.okname(s)]
        self.assertEqual(taken, [], f"{len(taken)} of {len(JUNK)} junk strings taken for a person")
        shouting = [s for s in JUNK if s.isupper() and contacts.okname(s, registry=True)]
        self.assertEqual(shouting, [])

    def test_the_known_miss_is_still_the_only_one(self):
        self.assertEqual([s for s in KNOWN_MISSES if contacts.okname(s)], ["River Bend"])

    def test_the_names_are_all_accepted(self):
        refused = [n for n in NAMES if not contacts.okname(n)]
        self.assertEqual(refused, [], f"{len(refused)} of {len(NAMES)} names refused")

    def test_a_surname_that_is_also_a_business_or_place_word_is_never_refused(self):
        refused = [f"{g} {s}" for s in WORD_SURNAMES for g in ("Sarah", "Tom", "Greg", "Nina", "Priya", "Wei") if not contacts.okname(f"{g} {s}")]
        self.assertEqual(refused, [])
        for s in WORD_SURNAMES:                                                       # also written in capitals by a state filing
            self.assertTrue(contacts.okname(f"SARAH {s.upper()}"), s)

    def test_the_names_keep_their_shape(self):
        self.assertEqual(contacts.okname("Dr. Alan Abbott"), "Alan Abbott")
        self.assertEqual(contacts.okname("MARK ELLISON"), "Mark Ellison")
        self.assertEqual(contacts.okname("Maria de los Angeles Perez"), "Maria de los Angeles Perez")
        self.assertEqual(contacts.okname("MARIA DE LOS ANGELES PEREZ"), "Maria de los Angeles Perez")
        self.assertEqual(contacts.okname("Sean O’Neil"), "Sean O'Neil")

    def test_the_junk_the_audit_listed(self):
        for bad in ("Jane Doe", "John Doe", "Jane Q Public", "Your Name", "Full Name", "Name Here", "First Last", "Firstname Lastname",
                    "Name Surname", "Lorem Ipsum", "Test User", "Sample Name", "Placeholder Name", "Foo Bar", "Mickey Mouse",
                    "Santa Claus", "Jane Doe Photography", "Corporate Responsibility", "Case Studies", "White Papers", "Knowledge Base",
                    "Quick Links", "Reach Out", "Say Hello", "How It Works", "Frequently Asked Questions", "Recent Posts",
                    "Business Hours", "Office Locations", "Stay Connected", "Request Demo", "Free Trial", "Create Account",
                    "Free Consultation", "See Pricing", "Download Brochure", "Watch Video", "Go Back", "Live Chat", "Track Order",
                    "Annual Report", "Success Stories", "Best Practices", "Family Owned", "Free Estimates", "Gift Cards", "Main Office",
                    "Head Office", "Oak Lane", "Church Street", "Web Design", "Managed IT", "Machine Learning", "Real Estate",
                    "Pest Control", "Dental Care", "Johnson Brothers Plumbing", "NEW YORK", "LOS ANGELES", "SAN FRANCISCO",
                    "UNITED STATES", "FREE QUOTE", "HELP DESK", "New York", "Los Angeles", "San Francisco", "Jobs Pipeline",
                    "Explore Platform", "CFO Questionnaire", "BBB National Programs"):
            self.assertIsNone(contacts.okname(bad), bad)
            self.assertIsNone(contacts.okname(bad, registry=True), bad)

    def test_a_city_and_state_or_an_all_capitals_name_with_a_suffix(self):
        for place in ("Austin Texas", "Houston Texas", "Portland Oregon", "Hartford Connecticut", "Texas Oklahoma"):
            self.assertIsNone(contacts.okname(place), place)
        for person in ("Jordan Washington", "Austin Reyes", "Dallas Hunter", "Charlotte Page", "Virginia Hall"):
            self.assertTrue(contacts.okname(person), person)
        self.assertEqual(contacts.okname("ROBERT GRANT Jr", registry=True), "Robert Grant Jr")
        self.assertEqual(contacts.okname("MARK ELLISON III"), "Mark Ellison III")
        self.assertIsNone(contacts.okname("BBB National Programs"))

    def test_a_street_is_not_a_person_but_a_person_may_be_named_lane(self):
        for street in ("Oak Lane", "Main Street", "Park Avenue", "Mill Road", "Church Street", "High Street", "First Avenue",
                       "Elm Street", "Second Street", "Wall Street"):
            self.assertIsNone(contacts.okname(street), street)
        for person in ("David Lane", "Rachel Lane", "Priya Lane", "Jon Street", "Margaret Court"):
            self.assertTrue(contacts.okname(person), person)

    def test_the_companys_own_name_is_not_a_person_on_a_web_page(self):
        self.assertIsNone(contacts.okname("Blue Heron", company="Blue Heron Software LLC"))
        self.assertIsNone(contacts.okname("Quinn Abbott", company="Quinn Abbott Consulting Inc"))
        self.assertEqual(contacts.okname("Quinn Abbott", company="Wexmere Labs LLC"), "Quinn Abbott")
        # a state filing may name a person after their own one-person company
        self.assertEqual(contacts.okname("Quinn Abbott", registry=True, company="Quinn Abbott LLC"), "Quinn Abbott")

    def test_business_words_that_are_surnames_are_accepted_after_a_given_name_only(self):
        for ok in ("Steve Jobs", "Frank Press", "Bill Cloud", "Ella Board", "Roger Center", "Kay Centre", "Kim Home", "Ron Service",
                   "Tina Register", "Dee Staff", "Max Welcome", "Nell Note"):
            self.assertTrue(contacts.okname(ok), ok)
        for bad in ("Jobs Pipeline", "Cloud Backup", "Press Release", "Board Members", "Staff Augmentation", "Service Desk",
                    "Welcome Message", "Home Inspection", "Center Stage Group", "Customer Service", "Latest News", "Read More",
                    "Learn More", "Show More", "Register Now"):
            self.assertIsNone(contacts.okname(bad), bad)


class NameFormTests(unittest.TestCase):
    """Real forms of a name the check used to refuse (D4, D5)."""

    def test_names_that_start_with_a_letter_outside_a_to_z(self):
        names = ["Łukasz Nowak", "Żaneta Kowalska", "Šimon Černý", "Čeněk Šimek", "Đorđe Petrović", "İbrahim Şahin", "Ūdris Ozols",
                 "Ēriks Bērziņš", "Ģirts Kalniņš", "Ścibor Kamiński", "Žofia Hrnčiar", "Ľubomír Hrnčiar", "Çağlar Yıldız", "Ömer Çelik",
                 "Nguyễn Văn Hòa", "Trần Thị Mai"]
        self.assertGreaterEqual(len(names), 14)
        for n in names:
            self.assertEqual(contacts.okname(n), n, n)
        self.assertEqual(contacts.okname("ŁUKASZ NOWAK"), "Łukasz Nowak")
        self.assertEqual(contacts.okname("ŠIMON ČERNÝ"), "Šimon Černý")
        self.assertEqual(contacts.okname("ŻANETA KOWALSKA"), "Żaneta Kowalska")
        self.assertIsNone(contacts.okname("Иван Петров"))                        # not Latin: still not read
        self.assertIsNone(contacts.okname("łukasz nowak"))                        # lower case on a web page is not a name

    def test_decomposed_letters_are_the_same_as_composed_ones(self):
        import unicodedata
        n = unicodedata.normalize("NFD", "Zoë Müller")
        self.assertEqual(contacts.okname(n), "Zoë Müller")

    def test_a_middle_initial_a(self):
        self.assertEqual(contacts.okname("John A. Smith"), "John A. Smith")
        self.assertEqual(contacts.okname("Robert A. Herrera"), "Robert A. Herrera")
        self.assertEqual(contacts.okname("Anil R Bhardwaj"), "Anil R Bhardwaj")
        self.assertIsNone(contacts.okname("John A Smith Q Jones Zed"))

    def test_initials_first(self):
        for n in ("J. R. Smith", "J.R. Smith", "A. J. Foyt", "T. J. Miller", "J. Michael Straczynski", "C. J. Whitlock"):
            self.assertEqual(contacts.okname(n), n, n)
        for bad in ("J. Smith", "J Smith", "Q Z", "J. R.", "A. B. C. D. Smith", "A. Note"):
            self.assertIsNone(contacts.okname(bad), bad)

    def test_a_single_letter_surname(self):
        for n in ("Henry X", "Jessica Q", "Brandon V", "Peter K"):
            self.assertEqual(contacts.okname(n), n, n)
        for bad in ("Plan B", "Option C", "Vitamin C", "Mary A", "Step B", "Type X Y"):
            self.assertIsNone(contacts.okname(bad), bad)

    def test_do_co_to_are_surnames(self):
        for n in ("Tuan Do", "Jose Co", "Mary Ann To", "Henry To"):
            self.assertEqual(contacts.okname(n), n, n)
        for bad in ("Do Not", "To Do", "How To", "Go To", "Talk To", "Back To", "Acme Co.", "What To Do"):
            self.assertIsNone(contacts.okname(bad), bad)

    def test_five_names_with_particles(self):
        for n in ("Maria de los Angeles Perez", "Luis Fernando de la Cruz", "Ana Maria de la Torre", "Lucia de las Heras",
                  "Juan de Dios Ramirez", "Pedro y Pablo Ortega", "Hendrik van der Berg"):
            self.assertEqual(contacts.okname(n), n, n)
        self.assertIsNone(contacts.okname("One Two Three Four Five"))
        self.assertIsNone(contacts.okname("Ann Bea Cy Dee Eve de la Cruz"))        # five names, not four
        self.assertIsNone(contacts.okname("a b c d e f g h"))

    def test_business_words_that_are_surnames(self):
        for n in ("Steve Jobs", "Frank Press", "Bill Cloud", "Ella Board", "Roger Center", "Kim Home", "Ron Service", "Tina Register",
                  "Dee Staff", "Max Welcome"):
            self.assertEqual(contacts.okname(n), n, n)


NB_HYPHEN = chr(0x2011)                      # the non-breaking hyphen some sites use in Co-Founder


class TitleCorpusTests(unittest.TestCase):
    """clean_title on 60+ realistic titles: ordinary case is never changed, all capitals are put in ordinary case (D2, D6)."""
    KEEP = ["CEO", "Founder & CEO", "Co-Founder", "Founder", "President", "Owner", "Managing Director", "Managing Partner",
            "Chief Technology Officer", "Chief Executive Officer", "Chief Operating Officer", "CISO", "CDO", "CRO", "CTO & Co-Founder",
            "Founder, MBA Candidate", "MBA Candidate", "Owner, CPA", "CPA", "Managing Member, Smith Group LLC", "Group LLC",
            "Principal, Acme Inc", "SEO Director", "SEO Manager", "AWS Solutions Architect", "SAP Consultant", "ERP Manager",
            "CRM Administrator", "PMO Director", "NOC Manager", "VP of Sales", "VP, Engineering", "SVP Operations", "EVP Strategy",
            "HR Director", "IT Director", "AI Lead", "QA Manager", "UX Designer", "UI Engineer", ".NET Developer", "iOS Developer",
            "iOS Engineering Lead", "eCommerce Manager", "Owner/Operator", "Founder | CEO", "CEO · Founder", "Co-Founder & CTO",
            "Chief Revenue Officer (CRO)", "Director of IT", "Executive Director", "General Manager", "Head of Product",
            "Founder and CEO", "Principal Consultant", "Partner, CPA, MBA", "Owner, LLC", "Senior AWS Engineer", "Cloud CISO"]
    SHOUTED = [("CHIEF EXECUTIVE OFFICER", "Chief Executive Officer"), ("CO-FOUNDER", "Co-Founder"), ("CEO & FOUNDER", "CEO & Founder"),
               ("VP OF SALES", "VP of Sales"), ("SEO DIRECTOR", "SEO Director"), ("OWNER, CPA", "Owner, CPA"),
               ("AWS SOLUTIONS ARCHITECT", "AWS Solutions Architect"), ("CISO", "CISO"), ("PRESIDENT & CEO", "President & CEO"),
               ("MANAGING DIRECTOR", "Managing Director"), ("FOUNDER AND CEO", "Founder and CEO"),
               ("SAP BASIS CONSULTANT", "SAP Basis Consultant"), ("ERP/CRM MANAGER", "ERP/CRM Manager"),
               ("OWNER, LLC", "Owner, LLC"), ("MBA CANDIDATE", "MBA Candidate"), ("THE FOUNDER", "Founder"),
               ("CHIEF EXECUTIVE OFFICER, COO", "Chief Executive Officer, COO")]

    def test_ordinary_case_is_never_changed(self):
        self.assertGreaterEqual(len(self.KEEP), 55)
        changed = [(t, contacts.clean_title(t)) for t in self.KEEP if contacts.clean_title(t) != t]
        self.assertEqual(changed, [])

    def test_all_capitals_become_ordinary_case_with_the_acronyms_kept(self):
        self.assertGreaterEqual(len(self.KEEP) + len(self.SHOUTED), 60)
        for raw, shown in self.SHOUTED:
            self.assertEqual(contacts.clean_title(raw), shown, raw)

    def test_clean_title_is_idempotent(self):
        for t in self.KEEP + [raw for raw, _ in self.SHOUTED] + ["Co" + NB_HYPHEN + "Founder", "Owner of", "(CEO & Director Operations)"]:
            once = contacts.clean_title(t)
            self.assertEqual(contacts.clean_title(once), once, t)

    def test_every_kind_of_dash_is_a_hyphen(self):
        for dash in [chr(c) for c in (*range(0x2010, 0x2016), 0x2212)]:
            self.assertEqual(contacts.clean_title(f"Co{dash}Founder"), "Co-Founder", hex(ord(dash)))
            self.assertEqual(contacts.clean_title(f"CO{dash}FOUNDER"), "Co-Founder", hex(ord(dash)))
        self.assertEqual(contacts.clean_title("Co" + NB_HYPHEN + "Founder & CEO"), "Co-Founder & CEO")
        self.assertTrue(contacts.LEAD.search(contacts.clean_title("Co" + NB_HYPHEN + "Founder")))

    def test_a_leading_dot_before_a_letter_stays(self):
        self.assertEqual(contacts.clean_title(".NET Developer"), ".NET Developer")
        self.assertEqual(contacts.clean_title(". Founder"), "Founder")
        self.assertEqual(contacts.clean_title("Founder."), "Founder")
        self.assertEqual(contacts.clean_title("..."), "")


class PhoneExtensionTests(unittest.TestCase):
    """A tel: link with an extension still gives the number (D8)."""
    NUMBERS = ["+1-512-210-0200", "(425) 210-0223", "206.210.0188", "1 415 210 4000", "212-210-0300"]
    FORMS = ["{n};ext=2210", "{n},2210", "{n}x2210", "{n} x 2210", "{n}#2210", "{n} ext. 2210", "{n} ext 2210", "{n};ext=2210,,5",
             "{n}p2210", "{n}extension2210", "{n}EXT2210", "{n}X2210"]

    def test_the_extension_is_cut_off(self):
        from itleads import util
        seen = 0
        for n in self.NUMBERS:
            want = util.usable_phone(n)
            self.assertTrue(want, n)
            for f in self.FORMS:
                href = "tel:" + f.format(n=n)
                got = contacts.phones_in(BeautifulSoup(f'<a href="{href}">call</a>', "lxml"), "")
                self.assertEqual(got, [want], href)
                seen += 1
        self.assertGreaterEqual(seen, 60)

    def test_an_encoded_link_and_a_plain_one_still_work(self):
        for href in ("tel:%2B15122100200", "tel:+15122100200", "tel:512-210-0200", "tel:+1%20512%20210%200200;ext=2210"):
            self.assertEqual(contacts.phones_in(BeautifulSoup(f'<a href="{href}">c</a>', "lxml"), ""), ["5122100200"], href)
        self.assertEqual(contacts.phones_in(BeautifulSoup('<a href="telecom.html">c</a>', "lxml"), ""), [])
        self.assertEqual(contacts.phones_in(BeautifulSoup('<a href="tel:5551234567x12">c</a>', "lxml"), ""), [])     # a 555 number is still no number

    def test_json_ld_telephone_with_an_extension(self):
        html = ('<html><head><title>Acme software</title><script type="application/ld+json">'
                '{"@type":"Organization","telephone":"+1 512-210-0200 ext. 2210"}</script></head><body>hi</body></html>')

        def load(url, timeout=8):
            return contacts.Page(url.rstrip("/"), html.encode()) if url.rstrip("/") == "https://acme.io" else None
        with mock.patch.object(contacts, "load", load):
            got = contacts.scrape("https://acme.io", "acme.io", "Acme LLC")
        self.assertEqual(got["phones"], ["5122100200"])

    def test_a_heading_that_is_only_the_companys_name_is_not_its_founder(self):
        html = ('<html><head><title>Wexmere Labs software</title></head><body><div>Wexmere Group</div><div>Founder</div>'
                '<div>Quinn Abbott</div><div>Co-Founder</div></body></html>')

        def load(url, timeout=8):
            return contacts.Page(url.rstrip("/"), html.encode()) if url.rstrip("/") == "https://wexmere.io" else None
        with mock.patch.object(contacts, "load", load):
            got = contacts.scrape("https://wexmere.io", "wexmere.io", "Wexmere Group LLC")
        self.assertEqual([p["name"] for p in got["people"]], ["Quinn Abbott"])


class MailboxWordTests(unittest.TestCase):
    """not_a_contact: one word of the mailbox name is enough (D10)."""

    def test_function_words_in_the_name(self):
        for local in ("investor.relations", "paul.press", "mark.media", "jon.jobs", "john.legal", "pam.hr", "relations", "press",
                      "media", "jobs", "press-team", "media_desk", "jobs+eu", "legal.team", "remove.me"):
            self.assertTrue(enrich.not_a_contact(local), local)
        for local in ("hello", "info", "sales", "jane", "jane.smith", "jsmith", "pressley", "mediator", "jobsite", "team",
                      "public.relation"):
            self.assertFalse(enrich.not_a_contact(local), local)


class MessageWordingTests(unittest.TestCase):
    def test_errors_the_admin_sees_name_the_product(self):
        import json as _json

        class Reply:
            def __init__(self, body):
                self.text, self.status_code = _json.dumps(body), 200

            def json(self):
                return _json.loads(self.text)

        for body, expect in (({"ok": False, "error": "Wrong token"}, "different secret than Hybrid Leads"),
                             ({"ok": True, "version": sheet.EXPECTED_VERSION + 1}, f"Hybrid Leads needs {sheet.EXPECTED_VERSION}")):
            with mock.patch("itleads.sheet.requests.post", return_value=Reply(body)):
                with self.assertRaises(sheet.BridgeError) as cm:
                    sheet.Bridge("https://script.example/exec", "t", retries=1).ping()
            self.assertIn(expect, str(cm.exception))
            self.assertNotIn("this tool", str(cm.exception))


class MalformedStoredResultTests(unittest.TestCase):
    """A stored result of the wrong kind is judged held, never raised on, and never stops the rest of a pass (D7)."""
    REC = StoredResultTests.REC
    CFG = StoredResultTests.CFG

    def e(self, **kw):
        return StoredResultTests.e(StoredResultTests, **kw)

    def test_clean_stored_on_values_of_the_wrong_kind(self):
        for junk in (None, [], "text", 7, ["a"]):
            self.assertEqual(enrich.clean_stored(junk), {}, junk)
            self.assertEqual(enrich.qualify(self.REC, junk, self.CFG), ("held", ["website"]), junk)
        for contact in (["Jane Hale"], "Jane Hale", 7, [{"name": "Jane Hale"}]):
            c = enrich.clean_stored(self.e(contact=contact), self.REC)
            self.assertEqual(c["contact"]["from"], "state registry", contact)           # the filing's officer takes the place
            self.assertEqual(enrich.clean_stored(self.e(contact=contact))["contact"], {}, contact)
        for empty in ([], "", 0):
            self.assertEqual(enrich.clean_stored(self.e(contact=empty), self.REC)["contact"], {}, empty)
        c = enrich.clean_stored(self.e(email=12345, phone=5122100200, phones=7))
        self.assertEqual((c["email"], c["email_from"]), ("12345", "company website"))      # a number is read as text, not raised on
        self.assertEqual((c["phone"], c["phones"]), ("5122100200", []))
        c = enrich.clean_stored(self.e(email=["a@b.io"], phone=["5122100200"], phones=[5555551234, 4152104000, None]))
        self.assertEqual((c["email"], c["phones"]), ("['a@b.io']", ["4152104000"]))
        self.assertEqual(enrich.clean_stored(self.e(it="strong"))["it"], "strong")
        self.assertEqual(enrich.qualify(self.REC, self.e(it="strong"), self.CFG), ("dropped", ["no sign of IT work on the website"]))
        self.assertEqual(enrich.qualify(self.REC, self.e(email=7), self.CFG)[0], "ready")
        self.assertFalse(enrich.has_usable_email(None))
        self.assertFalse(enrich.has_usable_phone("x"))
        self.assertEqual(enrich.clean_stored(self.e(), "not a record")["contact"]["name"], "Jane Hale")

    def test_a_malformed_row_does_not_stop_the_pass(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d) / "t.db")
            day = date(2026, 10, 6)
            for cid in ("ct:1", "ct:2", "ct:3", "ct:4"):
                s.add(dict(self.REC, id=cid), day)
                s.record_attempt(cid, self.e(domain=cid[3] + ".io"), "ready", day)
            s.mark_pushed("ct:4", "h", day)
            s.db.execute("UPDATE companies SET enrich='[1, 2]' WHERE id='ct:2'")
            s.db.execute("UPDATE companies SET enrich=? WHERE id='ct:3'", (json.dumps(self.e(contact=["x"], phone=5, email=9, domain="3.io")),))
            s.db.commit()
            lines = []
            st = pipeline.requalify_pass(s, self.CFG, day, lines.append)
            self.assertEqual(st["errors"], 0)
            self.assertEqual(st["demoted"], 1)                                     # the list that is no result at all: held, found again
            self.assertEqual(s.db.execute("SELECT state FROM companies WHERE id='ct:2'").fetchone()[0], "held")
            self.assertEqual(sorted(i["id"] for i in s.ready()), ["ct:1", "ct:3"])
            self.assertEqual(s.counts()["pushed"], 1)
            s.close()

    def test_a_company_that_cannot_be_judged_is_logged_by_id_and_the_rest_go_on(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d) / "t.db")
            day = date(2026, 10, 6)
            for cid in ("ct:1", "ct:2", "ct:3"):
                s.add(dict(self.REC, id=cid), day)
                s.record_attempt(cid, self.e(domain=cid[3] + ".io", phone="4155551234", phones=[]), "ready", day)
            real = enrich.qualify

            def flaky(rec, e, cfg):
                if rec["id"] == "ct:2":
                    raise AttributeError("boom")
                return real(rec, e, cfg)
            lines = []
            with mock.patch.object(enrich, "qualify", flaky):
                st = pipeline.requalify_pass(s, self.CFG, day, lines.append)
            self.assertEqual((st["errors"], st["demoted"]), (1, 2))
            self.assertTrue(any("ct:2" in ln and "AttributeError" in ln for ln in lines), lines)
            self.assertEqual([i["id"] for i in s.ready()], ["ct:2"])
            s.close()


class RequalifySentRowsTests(unittest.TestCase):
    """Companies already sent to the sheet are judged again by today's checks (D1, D9)."""
    REC = StoredResultTests.REC
    CFG = StoredResultTests.CFG
    DAY = date(2026, 10, 6)

    def e(self, **kw):
        return StoredResultTests.e(StoredResultTests, **kw)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def put(self, cid, enrich_, state="pushed", name="Acme LLC"):
        self.store.add(dict(self.REC, id=cid, name=name), self.DAY)
        self.store.record_attempt(cid, enrich_, "ready", self.DAY)
        if state == "pushed":
            self.store.mark_pushed(cid, "hash-" + cid, self.DAY)
        elif state in ("held", "dropped"):
            self.store.db.execute("UPDATE companies SET state=?, next_due=? WHERE id=?", (state, "2026-10-20", cid))
            self.store.db.commit()

    def row(self, cid):
        r = self.store.db.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
        return {"state": r["state"], "next_due": r["next_due"], "hash": r["pushed_hash"], "pushed_at": r["pushed_at"],
                "enrich": json.loads(r["enrich"])}

    def test_a_sent_company_that_still_qualifies_is_cleaned_and_stays_sent(self):
        bad = {"name": "Jobs Pipeline", "title": "Owner summary: 2 new", "email": "", "linkedin": "", "from": "company website"}
        self.put("ct:1", self.e(contact=bad))
        lines = []
        st = pipeline.requalify_pass(self.store, self.CFG, self.DAY, lines.append)
        self.assertEqual((st["cleaned"], st["unlisted"], st["demoted"], st["released"], st["errors"]), (1, 0, 0, 0, 0))
        r = self.row("ct:1")
        self.assertEqual((r["state"], r["hash"], r["pushed_at"]), ("pushed", "hash-ct:1", "2026-10-06"))       # nothing is sent again
        self.assertEqual(r["enrich"]["contact"]["name"], "Dana Rivers")
        self.assertEqual(self.store.ready(), [])                                                              # not queued for the sheet
        self.assertEqual([i["id"] for i in self.store.export_items()], ["ct:1"])                              # the downloads serve the clean value
        self.assertEqual(lines, [])

    def test_a_sent_company_that_no_longer_qualifies_goes_to_held_to_be_looked_up_today(self):
        self.put("ct:1", self.e(email="concerns@acme.io"), name="Wexmere Cloud LLC")
        self.put("ct:2", self.e(domain="b.io", phone="4155551234", phones=["4155551234"]), name="Bexmere Cloud LLC")
        self.put("ct:3", self.e(domain="c.io"), name="Cexmere Cloud LLC")
        lines = []
        st = pipeline.requalify_pass(self.store, self.CFG, self.DAY, lines.append)
        self.assertEqual((st["unlisted"], st["cleaned"], st["errors"]), (2, 0, 0))
        for cid, missing in (("ct:1", ["email"]), ("ct:2", ["phone"])):
            r = self.row(cid)
            self.assertEqual((r["state"], r["next_due"]), ("held", "2026-10-06"), cid)                      # today, not tomorrow
            self.assertEqual(r["enrich"]["missing"], missing)
            self.assertEqual(self.store.due(self.DAY)[0]["state"], "held")
        self.assertEqual(self.row("ct:3")["state"], "pushed")
        self.assertEqual({i["id"] for i in self.store.due(self.DAY)}, {"ct:1", "ct:2"})                      # the next run picks them up
        self.assertTrue(any("Wexmere Cloud LLC" in ln and "no longer meets the rules" in ln for ln in lines), lines)
        self.assertTrue(any("Bexmere Cloud LLC" in ln for ln in lines))
        self.assertEqual([i["id"] for i in self.store.export_items()], ["ct:3"])

    def test_a_sent_company_that_would_now_be_dropped_is_held_and_looked_up_again(self):
        self.put("ct:1", self.e(created="2001-01-01"))
        cfg = dict(self.CFG, skip_established=True)
        st = pipeline.requalify_pass(self.store, cfg, self.DAY)
        self.assertEqual(st["unlisted"], 1)
        r = self.row("ct:1")
        self.assertEqual((r["state"], r["next_due"]), ("held", "2026-10-06"))
        self.assertIn("established company", r["enrich"]["missing"][0])
        self.assertNotIn("dropped_because", r["enrich"])

    def test_a_second_pass_changes_nothing(self):
        self.put("ct:1", self.e(email="concerns@acme.io"))
        self.put("ct:2", self.e(domain="b.io", contact={"name": "Explore Platform", "title": "A Note", "email": "", "linkedin": "",
                                                        "from": "company website"}))
        self.put("ct:3", self.e(domain="c.io", contact={"name": "Randy Smith", "title": "Owner of", "email": "", "linkedin": "",
                                                        "from": "company website"}))
        self.put("ct:4", self.e(domain="d.io"), state="ready")
        self.put("ct:5", self.e(domain="e.io", phone="4155551234", phones=[]), state="ready")
        self.put("ct:6", self.e(domain="f.io", phone="4155551234", phones=[]), state="held")
        first = pipeline.requalify_pass(self.store, self.CFG, self.DAY)
        self.assertEqual((first["unlisted"], first["demoted"], first["cleaned"]), (1, 1, 2))
        snap = self.store.db.execute("SELECT * FROM companies ORDER BY id").fetchall()
        snap = [tuple(r) for r in snap]
        second = pipeline.requalify_pass(self.store, self.CFG, self.DAY)
        self.assertEqual(second, {"released": 0, "cleaned": 0, "demoted": 0, "unlisted": 0, "errors": 0})
        self.assertEqual([tuple(r) for r in self.store.db.execute("SELECT * FROM companies ORDER BY id").fetchall()], snap)

    def test_held_and_dropped_companies_are_judged_as_before(self):
        """No cleaning for them: a held company is released only when its stored result qualifies as it stands."""
        self.put("ct:1", self.e(email="concerns@acme.io", missing=["phone"]), state="held")
        self.put("ct:2", self.e(domain="b.io", it={"level": "none", "score": 0}, dropped_because=["x"]), state="dropped")
        self.put("ct:3", self.e(domain="c.io", missing=["phone"]), state="held")
        before = {c: self.row(c) for c in ("ct:1", "ct:2")}
        st = pipeline.requalify_pass(self.store, self.CFG, self.DAY)
        self.assertEqual(st["released"], 1)                                         # ct:3 only
        self.assertEqual({c: self.row(c) for c in ("ct:1", "ct:2")}, before)
        self.assertEqual(self.row("ct:3")["state"], "ready")

    def test_requalify_still_returns_how_many_were_released(self):
        self.put("ct:1", self.e(missing=["phone"]), state="held")
        self.put("ct:2", self.e(domain="b.io", email="concerns@b.io"))
        self.assertEqual(pipeline.requalify(self.store, self.CFG, self.DAY), 1)

    def test_demote_and_save_enrich_only_touch_listed_companies(self):
        self.put("ct:1", self.e(), state="held")
        self.assertFalse(self.store.demote("ct:1", "held", {"x": 1}, self.DAY))
        self.assertFalse(self.store.demote("ct:none", "held", {"x": 1}, self.DAY))
        self.store.save_enrich("ct:1", {"x": 1})
        self.assertNotIn("x", self.row("ct:1")["enrich"])
        self.put("ct:2", self.e(domain="b.io"), state="ready")
        self.assertTrue(self.store.demote("ct:2", "held", {"x": 2}, self.DAY))
        self.assertEqual(self.row("ct:2")["next_due"], "2026-10-07")                  # waiting ones: tomorrow
        self.put("ct:3", self.e(domain="c.io"))
        self.assertTrue(self.store.demote("ct:3", "dropped", {"x": 3}, self.DAY))
        self.assertEqual((self.row("ct:3")["state"], self.row("ct:3")["next_due"]), ("dropped", None))

    def _send_waiting(self, cfg, store_setup):
        from itleads import config

        class FakeBridge:
            def __init__(self, url, token):
                pass

            def ping(self):
                return {"ok": True}

            def upsert(self, rows, today):
                return {"inserted": len(rows), "updated": 0, "skipped": 0}

            def log(self, *a, **k):
                return {}
        saved = (config.ROOT, config.DATA, config.LOGS, config.CONFIG_PATH)
        root = Path(self._tmp.name) / "home"
        config.ROOT, config.DATA, config.LOGS, config.CONFIG_PATH = root, root / "data", root / "logs", root / "config.json"
        config.ensure_dirs()
        try:
            s = Store(config.DATA / "leads.db")
            store_setup(s)
            s.close()
            with mock.patch.object(sheet, "Bridge", FakeBridge):
                summary = pipeline.send_waiting(dict(cfg, apps_script_url="https://script.example/exec", token="t", sources={}),
                                                log=lambda *_: None)
            s = Store(config.DATA / "leads.db")
            counts = s.counts()
            s.close()
            return summary, counts
        finally:
            config.ROOT, config.DATA, config.LOGS, config.CONFIG_PATH = saved

    def test_the_summary_of_a_send_counts_what_the_pass_released_and_took_back(self):
        today = date.today()

        def setup(s):
            s.add(dict(self.REC, id="ct:1"), today)
            s.record_attempt("ct:1", self.e(missing=["phone"]), "held", today)                  # a rule was relaxed: released and sent
            s.add(dict(self.REC, id="ct:2"), today)
            s.record_attempt("ct:2", self.e(domain="b.io", email="concerns@b.io"), "ready", today)
            s.mark_pushed("ct:2", "h", today)                                                   # already sent, now a function mailbox
        summary, counts = self._send_waiting(dict(self.CFG), setup)
        self.assertEqual((summary["status"], summary["added"], summary["pushed"]), ("ok", 1, 1))
        self.assertEqual(summary["rechecked"]["unlisted"], 1)
        self.assertEqual((counts["pushed"], counts["held"]), (1, 1))


    def test_a_run_counts_logs_and_stores_what_the_pass_did(self):
        from itleads import config
        self.put("ct:1", self.e(email="concerns@acme.io"), name="Wexmere Cloud LLC")
        self.put("ct:2", self.e(domain="b.io", contact={"name": "Explore Platform", "title": "A Note", "email": "", "linkedin": "",
                                                        "from": "company website"}), name="Bexmere Cloud LLC")
        lines = []
        saved = (config.ROOT, config.DATA, config.LOGS, config.CONFIG_PATH)
        root = Path(self._tmp.name) / "home"
        config.ROOT, config.DATA, config.LOGS, config.CONFIG_PATH = root, root / "data", root / "logs", root / "config.json"
        config.ensure_dirs()
        try:
            cfg = dict(config.DEFAULTS, sources={})
            with mock.patch.object(pipeline, "online", return_value=True), mock.patch.object(pipeline.sources, "build", return_value=[]), \
                    mock.patch.object(pipeline, "fetch_all", return_value={}), \
                    mock.patch.object(pipeline, "enrich_pending", return_value={"checked": 0, "ready": 0, "held": 0, "dropped": 0, "errors": 0}):
                summary = pipeline._run(cfg, self.store, log=lines.append, quiet=True)
        finally:
            config.ROOT, config.DATA, config.LOGS, config.CONFIG_PATH = saved
        self.assertEqual({k: summary["rechecked"][k] for k in ("released", "cleaned", "demoted", "unlisted", "errors")},
                         {"released": 0, "cleaned": 1, "demoted": 0, "unlisted": 1, "errors": 0})
        self.assertEqual(self.store.last_run()["summary"]["rechecked"]["unlisted"], 1)           # kept in the run history
        self.assertTrue(any("Wexmere Cloud LLC" in ln for ln in lines), lines)                  # the company is named in the run log
        self.assertTrue(any("1 companies already sent" in ln for ln in lines), lines)
        self.assertEqual(summary["held"], 1)


class RepairCommandTests(unittest.TestCase):
    """./it-leads repair: the same pass, once, on purpose: a dry run unless --apply, with a backup first."""

    def setUp(self):
        from itleads import config
        self.config = config
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.saved = (config.ROOT, config.DATA, config.LOGS, config.CONFIG_PATH)
        config.ROOT, config.DATA, config.LOGS, config.CONFIG_PATH = root, root / "data", root / "logs", root / "config.json"
        config.ensure_dirs()
        day = date(2026, 10, 6)
        s = Store(config.DATA / "leads.db")
        e = StoredResultTests.e(StoredResultTests)
        for cid, over, name in (("ct:1", {"email": "concerns@acme.io"}, "Wexmere Cloud LLC"), ("ct:2", {"domain": "b.io"}, "Bexmere Cloud LLC"),
                                ("ct:3", {"domain": "c.io", "contact": {"name": "Explore Platform", "title": "A Note", "email": "",
                                                                         "linkedin": "", "from": "company website"}}, "Cexmere LLC")):
            s.add(dict(StoredResultTests.REC, id=cid, name=name), day)
            s.record_attempt(cid, dict(e, **over), "ready", day)
            s.mark_pushed(cid, "h", day)
        s.close()
        self.db = config.DATA / "leads.db"

    def tearDown(self):
        self.config.ROOT, self.config.DATA, self.config.LOGS, self.config.CONFIG_PATH = self.saved
        self._tmp.cleanup()

    def run_cli(self, *argv):
        import contextlib
        import io
        from itleads import cli
        out = io.StringIO()
        with mock.patch.object(self.config, "load", return_value=dict(StoredResultTests.CFG)), contextlib.redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def states(self):
        s = Store(self.db)
        try:
            return {r["id"]: (r["state"], r["enrich"]) for r in s.db.execute("SELECT * FROM companies ORDER BY id")}
        finally:
            s.close()

    def test_a_dry_run_changes_nothing_and_says_what_would_change(self):
        before = self.states()
        code, out = self.run_cli("repair")
        self.assertEqual(code, 0)
        self.assertEqual(self.states(), before)
        self.assertFalse((self.config.DATA / "leads-before-repair.db").exists())
        self.assertIn("Dry run, nothing was written", out)
        self.assertIn("Wexmere Cloud LLC", out)
        self.assertIn("1 companies already sent to the sheet no longer qualify", out)
        self.assertIn("1 companies kept, with their stored values cleaned", out)
        self.assertIn("repair --apply", out)

    def test_apply_backs_up_first_then_writes_and_a_second_apply_changes_nothing(self):
        before = self.states()
        code, out = self.run_cli("repair", "--apply")
        self.assertEqual(code, 0)
        backup = self.config.DATA / "leads-before-repair.db"
        self.assertTrue(backup.exists())
        self.assertEqual(oct(backup.stat().st_mode & 0o777), "0o600")
        old = Store(backup)
        self.assertEqual({r["id"]: (r["state"], r["enrich"]) for r in old.db.execute("SELECT * FROM companies ORDER BY id")}, before)
        old.close()
        after = self.states()
        self.assertEqual({c: v[0] for c, v in after.items()}, {"ct:1": "held", "ct:2": "pushed", "ct:3": "pushed"})
        self.assertEqual(json.loads(after["ct:3"][1])["contact"]["name"], "Dana Rivers")
        again_code, again = self.run_cli("repair", "--apply")
        self.assertEqual(again_code, 0)
        self.assertEqual(self.states(), after)
        self.assertIn("0 companies already sent to the sheet no longer qualify", again)
        self.assertTrue(backup.exists())                                            # the first backup is kept, the second is new
        self.assertEqual(len(list(self.config.DATA.glob("leads-before-repair*.db"))), 2)

    def test_no_database_is_not_an_error_with_a_traceback(self):
        self.db.unlink()
        for ext in ("-wal", "-shm"):
            Path(str(self.db) + ext).unlink() if Path(str(self.db) + ext).exists() else None
        code, out = self.run_cli("repair")
        self.assertEqual(code, 1)
        self.assertIn("nothing to repair", out)

    def test_it_waits_for_a_run_that_is_going(self):
        with pipeline.run_lock():
            code, out = self.run_cli("repair", "--apply")
        self.assertEqual(code, 0)
        self.assertIn("Skipped", out)
        self.assertFalse((self.config.DATA / "leads-before-repair.db").exists())


if __name__ == "__main__":
    unittest.main()
