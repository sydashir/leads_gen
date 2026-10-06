"""The signed-in pages, the sign-in page and the error page: structure and wording that can be checked without a browser.

Landmarks, one h1 per page, form labels and aria links, the content policy (no inline styles or scripts), the words we agreed
on (American spelling, one term per idea), and the stylesheet facts the accessibility and responsive reviews depend on
(forced-colors block, rem text sizes, the chart text ladder, touch sizes). Everything here uses invented data. No network,
no browser, no Google."""
import re
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from itleads import config  # noqa: E402
from itleads.store import Store  # noqa: E402
from itleads.web import appdb  # noqa: E402
from tests import test_web  # noqa: E402

WEB = ROOT / "itleads" / "web"
TEMPLATES = WEB / "templates"
STATIC = WEB / "static"
MINE = ("shell.html", "dashboard.html", "_stats.html", "settings.html", "login.html", "error.html")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def words(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ")).strip()


def text_of_id(soup, ident: str) -> str:
    node = soup.find(id=ident)
    assert node is not None, "no element with id " + ident
    return words(node)


def accessible_name(soup, field) -> str:
    """What a screen reader says for a form control: aria-labelledby, then aria-label, then its <label>."""
    if field.get("aria-labelledby"):
        return " ".join(text_of_id(soup, i) for i in field["aria-labelledby"].split())
    if field.get("aria-label"):
        return field["aria-label"].strip()
    if field.get("id"):
        label = soup.find("label", attrs={"for": field["id"]})
        if label:
            return words(label)
    parent = field.find_parent("label")
    return words(parent) if parent else ""


def hygiene(testcase, soup, where):
    """Rules every page must keep: unique ids, working label and aria links, no inline style or handler, sized images."""
    ids = [n["id"] for n in soup.find_all(id=True)]
    testcase.assertEqual(sorted(set(i for i in ids if ids.count(i) > 1)), [], where + ": duplicate ids")
    for label in soup.find_all("label", attrs={"for": True}):
        testcase.assertIn(label["for"], ids, where + ": <label for=%s> points at nothing" % label["for"])
    for node in soup.find_all(attrs={"aria-labelledby": True}) + soup.find_all(attrs={"aria-describedby": True}):
        for key in ("aria-labelledby", "aria-describedby"):
            for ref in (node.get(key) or "").split():
                testcase.assertIn(ref, ids, where + ": %s=%s points at nothing" % (key, ref))
    testcase.assertEqual(soup.find_all(attrs={"style": True}), [], where + ": inline style")
    testcase.assertEqual(soup.find("style"), None, where + ": <style> element")
    for node in soup.find_all(True):
        testcase.assertEqual([a for a in node.attrs if a.lower().startswith("on")], [], where + ": event handler on <%s>" % node.name)
    for script in soup.find_all("script"):
        testcase.assertTrue(script.get("src"), where + ": inline script")
    for img in soup.find_all("img"):
        testcase.assertIsNotNone(img.get("alt"), where + ": img without alt")
        testcase.assertTrue(img.get("width") and img.get("height"), where + ": img without width and height")
    testcase.assertEqual(soup.html.get("lang"), "en")
    testcase.assertEqual(len(soup.find_all("h1")), 1, where + ": needs exactly one h1")
    levels = [int(h.name[1]) for h in soup.find_all(re.compile(r"^h[1-6]$"))]
    for before, after in zip(levels, levels[1:]):
        testcase.assertLessEqual(after, before + 1, where + ": heading levels skip (%s)" % levels)
    for ext in soup.find_all("a", target="_blank"):
        testcase.assertIn("noopener", ext.get("rel", []), where + ": target=_blank without noopener")
        testcase.assertIn("opens in a new tab", words(ext), where + ": external link %r does not say it opens a new tab" % words(ext))


# ------------------------------------------------------------------ files
class TemplateAndScriptFacts(unittest.TestCase):
    def test_no_inline_style_script_or_handler_in_the_templates(self):
        for name in MINE:
            text = read(TEMPLATES / name)
            self.assertNotRegex(text, r"\sstyle\s*=", name)
            self.assertNotRegex(text, r"<style", name)
            self.assertNotRegex(text, r"<script(?![^>]*\ssrc=)", name)
            self.assertNotRegex(text, r"\son[a-z]+\s*=", name)

    def test_american_spelling_and_the_agreed_words(self):
        banned = [r"\blicence", r"\bhonour", r"\borganis", r"\bcolour", r"\bcentre\b", r"\bbehaviour",
                  r"\buntick", r"\btick(ed|s)?\b", r"\bre-?checked\b", r"\bprincipals?\b", r"\bevery morning\b", r"\bfound every morning\b",
                  r"\b07:00\b", r"\bLog out\b", r"\bFill it in\b", r"\bInternal use only\b", r"\bSources checked\b", r"\bBy source\b", r"\bqualif(y|ies|ied)\b"]
        files = [TEMPLATES / n for n in MINE] + [STATIC / "app.js"]
        for path in files:
            text = read(path)
            for pattern in banned:
                self.assertNotRegex(text, pattern, "%s still has %s" % (path.name, pattern))

    def test_the_words_the_pages_use(self):
        stats, settings, login = read(TEMPLATES / "_stats.html"), read(TEMPLATES / "settings.html"), read(TEMPLATES / "login.html")
        self.assertIn("New filings found", stats)
        self.assertIn("By registry", stats)
        self.assertIn(">Registries<", settings)
        self.assertIn("Save changes", settings)
        self.assertIn("Team login", login)
        self.assertIn("Fill in the form", login)
        self.assertIn("Sign out", read(TEMPLATES / "shell.html"))
        self.assertIn("New IT company filings, checked every day.", login)
        self.assertIn("<h1>New IT company filings</h1>", read(TEMPLATES / "dashboard.html"))

    def test_app_js_says_added_formats_numbers_and_dates_the_same_everywhere(self):
        js = read(STATIC / "app.js")
        self.assertNotIn('" new"', js)
        self.assertIn('" added"', js)
        self.assertNotIn("en-GB", js)
        self.assertIn('toLocaleString("en-US")', js)
        self.assertIn('const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]', js)
        self.assertNotIn("getMonth", js)                         # no browser-language month names
        self.assertIn("The run stopped before it finished", js)
        self.assertIn("send this to your admin", js)
        self.assertIn("Sheet rows", js)                          # the scrolling table has a name
        self.assertIn("tabIndex = 0", js)                        # and the keyboard can reach it
        self.assertIn('"role", "region"', js)

    def test_the_stylesheet_keeps_the_accessibility_and_responsive_fixes(self):
        css = read(STATIC / "app.css")
        self.assertIn("@media (forced-colors:active)", css)
        self.assertRegex(css, r"\.bar i\{background:Highlight;forced-color-adjust:none\}")
        self.assertRegex(css, r"\.nav a\[aria-current\]::after\{background:Highlight;forced-color-adjust:none\}")
        self.assertRegex(css, r"\.pill,\.chip\{border:1px solid CanvasText\}")
        self.assertIn("scroll-padding-top:84px", css)
        self.assertIn("border:1px solid rgba(255,255,255,.46)", css)                # text fields: 4.5:1 on black
        self.assertIn("border:1px solid rgba(255,255,255,.45)", css)                # outline buttons
        self.assertRegex(css, r"\.check input:focus-visible\{outline:2px solid var\(--lime\)")
        self.assertRegex(css, r"input:not\(\[type=checkbox\]\):not\(\[type=radio\]\):focus,textarea:focus\{outline:2px solid transparent")
        self.assertRegex(css, r"\.menu \.pop a:hover small\{color:var\(--mute\)\}")
        self.assertRegex(css, r"\.btn\[aria-disabled=true\]\{opacity:\.72")
        self.assertRegex(css, r"\.nav a:focus-visible\{outline-offset:-4px\}")
        self.assertIn("@media (pointer:coarse)", css)
        self.assertRegex(css, r"@media \(max-height:500px\)\{\s*dialog\.preview\{width:100vw")
        self.assertIn("font-size:93.75%", css)                                      # the browser's own size setting is honored
        self.assertIn('"Inter Fallback"', css)
        self.assertRegex(css, r"size-adjust:\d+(\.\d+)?%;ascent-override:\d+(\.\d+)?%;descent-override:\d+(\.\d+)?%;line-gap-override:0%")
        self.assertRegex(css, r'--font:"Inter","Inter Fallback",')
        self.assertNotIn(".num{", css)                                              # the unused rule is gone
        self.assertRegex(css, r"\.table thead\{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect\(0 0 0 0\)")
        self.assertNotRegex(css, r"\.table thead\{display:none\}")                   # column headers stay in the accessibility tree
        self.assertIn(".pv-table:focus-visible{outline-offset:-3px", css)
        self.assertNotRegex(css, r"max-width:360px")                                  # the preview no longer cuts cells at 360 px

    def test_text_sizes_are_rem_except_the_chart_and_touch_minimum(self):
        css = read(STATIC / "app.css")
        for number, line in enumerate(css.splitlines(), 1):
            if "font-size:" not in line and not re.search(r"[{;]font:", line):
                continue
            if ".trend text" in line or "max(16px" in line or "select{font-size:16px}" in line:
                continue                                                            # the SVG chart is sized in drawing units; iOS needs 16 px
            self.assertNotRegex(line, r"font(-size)?:\s*[\d.]+px", "app.css:%d sizes text in px: %s" % (number, line.strip()))

    def test_the_chart_text_ladder_leaves_no_width_uncovered(self):
        """From a 320 px phone to 560 px the drawing is scaled down, so its units grow as the screen narrows: every width
        in between has one size, and the sizes only shrink as the screen widens."""
        css = read(STATIC / "app.css")
        bands = []
        for lo, hi, size in re.findall(r"@media \(min-width:(\d+)px\) and \(max-width:(\d+)px\)\{\.trend text\{font-size:([\d.]+)px\}\}", css):
            bands.append((int(lo), int(hi), float(size)))
        for hi, size in re.findall(r"@media \(max-width:(\d+)px\)\{\.trend text\{font-size:([\d.]+)px\}\}", css):
            bands.append((0, int(hi), float(size)))
        ladder = sorted(b for b in bands if b[1] <= 560)
        self.assertEqual(ladder[0][0], 0)
        for (_, hi, _s), (lo, _h, _t) in zip(ladder, ladder[1:]):
            self.assertEqual(lo, hi + 1, "gap or overlap in the chart text ladder at %s" % hi)
        self.assertEqual(ladder[-1][1], 560)
        sizes = [b[2] for b in ladder]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertGreaterEqual(sizes[0], 24)                                       # 320 px phones


# ------------------------------------------------------------------ rendered pages
class PageCase(test_web.WebCase):
    def log_run(self, status="ok", hours_ago=2, **extra):
        store = Store(config.DATA / "leads.db")
        start = datetime.now() - timedelta(hours=hours_ago)
        store.log_run(start.isoformat(), (start + timedelta(minutes=2)).isoformat(),
                      dict({"status": status, "errors": [], "trigger": "schedule"}, **extra))
        store.close()

    def member_client(self):
        if appdb.count_users() == 0:
            self.make_user()                                                        # the first account is the admin
        self.make_user("member@example.com")
        client = self.app.test_client()
        self.assertEqual(self.login(client, "member@example.com").status_code, 302)
        return client

    def seed_held(self, n):
        """n companies that were looked at and are being held back (no website found yet)."""
        s = Store(config.DATA / "leads.db")
        for i in range(n):
            rec = {"id": "tx:Held %d LLC" % i, "source": "tx", "name": "Held %d LLC" % i, "trade_name": "", "state": "TX", "city": "Austin",
                   "address": "1 Main St", "zip": "78701", "registered": "2026-10-02", "industry": "Software", "individual": False,
                   "emails": [], "people": []}
            s.add(rec, date.today())
            s.record_attempt(rec["id"], {"website": "", "domain": "", "it": {"level": "none"}}, "held", date.today())
        s.close()

    def show_login_hint(self):
        config.update(lambda c: c["app"].__setitem__("login", {"email": "crew@agency.test", "password": "Another-Pass-1", "show": True}))


class SignInPageTests(PageCase):
    def page(self, **kw):
        return soup_of(self.app.test_client().get("/login").get_data(as_text=True))

    def test_the_sign_in_page_has_landmarks_and_one_h1_the_task(self):
        soup = self.page()
        hygiene(self, soup, "login")
        self.assertEqual(words(soup.h1), "Sign in")
        self.assertIsNotNone(soup.find("main"))
        self.assertIsNotNone(soup.body.find("header"))                              # the banner with the logo
        self.assertEqual(soup.find_all("h2"), [])
        tagline = soup.find("p", class_="tagline")
        self.assertEqual(words(tagline), "New IT company filings, checked every day.")
        self.assertNotIn("every morning", words(soup))

    def test_the_fields_have_labels_and_the_email_has_focus_at_first(self):
        soup = self.page()
        email, password = soup.find(id="email"), soup.find(id="password")
        self.assertEqual((accessible_name(soup, email), accessible_name(soup, password)), ("Email", "Password"))
        self.assertTrue(email.has_attr("autofocus"))
        self.assertFalse(password.has_attr("autofocus"))
        self.assertIsNone(soup.find(attrs={"role": "alert"}))
        self.assertIsNone(soup.find(attrs={"aria-invalid": True}))

    def test_a_failed_sign_in_points_both_fields_at_the_message_and_moves_focus_to_the_password(self):
        self.user_client()
        c = self.app.test_client()
        r = self.login(c, "admin@example.com", "wrong password")
        soup = soup_of(r.get_data(as_text=True))
        hygiene(self, soup, "login after a failed sign-in")
        alert = soup.find(attrs={"role": "alert"})
        self.assertEqual(alert["id"], "login-error")
        self.assertIn("do not match", words(alert))
        for field in (soup.find(id="email"), soup.find(id="password")):
            self.assertEqual(field.get("aria-invalid"), "true")
            self.assertEqual(field.get("aria-describedby"), "login-error")
        self.assertEqual(soup.find(id="email").get("value"), "admin@example.com")      # what was typed is kept
        self.assertTrue(soup.find(id="password").has_attr("autofocus"))
        self.assertFalse(soup.find(id="email").has_attr("autofocus"))

    def test_the_team_login_box_says_what_it_is(self):
        self.show_login_hint()
        soup = self.page()
        box = soup.find(id="internal")
        self.assertEqual(words(box.find("b")), "Team login")
        self.assertEqual(words(soup.find(id="use-login")), "Fill in the form")
        self.assertEqual(text_of_id(soup, "hint-email"), "crew@agency.test")
        self.assertNotIn("Internal use only", words(soup))
        hygiene(self, soup, "login with the team login box")


class ErrorPageTests(PageCase):
    def test_the_404_page_has_landmarks_and_a_way_back_that_fits_who_is_looking(self):
        soup = soup_of(self.app.test_client().get("/nope").get_data(as_text=True))
        hygiene(self, soup, "404 signed out")
        self.assertIsNotNone(soup.find("main"))
        self.assertIsNotNone(soup.body.find("header"))
        self.assertEqual(words(soup.h1), "Page not found")
        self.assertEqual(soup.find("a", class_="logo")["aria-label"], "Hybrid Leads start page")
        self.assertEqual(words(soup.find("main").find("a", class_="btn")), "Back to the start page")
        c = self.user_client()
        soup = soup_of(c.get("/nope").get_data(as_text=True))
        hygiene(self, soup, "404 signed in")
        self.assertEqual(soup.find("a", class_="logo")["aria-label"], "Hybrid Leads dashboard")
        self.assertEqual(words(soup.find("main").find("a", class_="btn")), "Back to the dashboard")


class DashboardTests(PageCase):
    def test_the_shell_has_a_skip_link_landmarks_and_the_sign_out_button(self):
        c = self.user_client()
        self.seed()
        self.log_run(added=1, pushed=0, held=2, seconds=60, sources={"ct": 1})
        soup = soup_of(c.get("/").get_data(as_text=True))
        hygiene(self, soup, "dashboard")
        skip = soup.find("a", class_="skip")
        self.assertEqual(skip["href"], "#main")
        self.assertEqual(soup.body.find(True, recursive=True), skip)                # the very first thing in the page
        main = soup.find("main")
        self.assertEqual((main["id"], main.get("tabindex")), ("main", "-1"))
        self.assertIsNotNone(soup.find("header", class_="top"))
        self.assertIsNotNone(soup.find("nav", attrs={"aria-label": "Main"}))
        self.assertEqual(words(soup.h1), "New IT company filings")
        out = soup.find("form", action="/logout").find("button")
        self.assertEqual(words(out), "Sign out")
        self.assertNotIn("Log out", words(soup))

    def test_icons_are_hidden_from_screen_readers_and_the_chart_is_named(self):
        c = self.user_client()
        self.seed()
        soup = soup_of(c.get("/").get_data(as_text=True))
        for button in soup.find("div", class_="actions").find_all(["button", "summary"]):
            svg = button.find("svg")
            self.assertEqual((svg.get("aria-hidden"), svg.get("focusable")), ("true", "false"), words(button))
        chart = soup.find("svg", attrs={"role": "img"})
        self.assertIn("Companies added per day", chart["aria-label"])

    def test_the_preview_dialog_announces_itself_and_says_links_open_in_a_new_tab(self):
        soup = soup_of(self.user_client().get("/").get_data(as_text=True))
        live = soup.find(id="pv-live")
        self.assertEqual((live.get("role"), live.get("aria-live")), ("status", "polite"))
        self.assertTrue(live.find_parent("dialog"))
        self.assertEqual(soup.find("dialog").get("aria-labelledby"), "pv-title")
        self.assertIn("opens in a new tab", words(soup.find(id="pv-open")))
        self.assertTrue(soup.find(id="pv-close").has_attr("autofocus"))

    def test_the_google_banner_says_what_works_without_google(self):
        admin = self.user_client()
        text = words(soup_of(admin.get("/").get_data(as_text=True)).find("div", class_="notice"))
        self.assertEqual(text, "The Google Sheet is not connected yet. Connect it to get the sheet and its daily update. "
                               "The preview and downloads already work. Connect Google")
        member = self.member_client()
        soup = soup_of(member.get("/").get_data(as_text=True))
        banner = soup.find("div", class_="notice")
        self.assertEqual(words(banner), "The Google Sheet is not connected yet. Ask an admin to connect it. The preview and downloads already work.")
        self.assertIsNone(banner.find("a"))
        self.assertIsNone(soup.find("a", href="/settings"))                          # the member has no Settings link either

    def test_the_run_history_has_real_column_headers_and_new_wording(self):
        c = self.user_client()
        self.seed()
        self.log_run(added=3, pushed=2, held=1, seconds=61, sources={"ct": 3, "tx": 0})
        soup = soup_of(c.get("/").get_data(as_text=True))
        heads = soup.select("table.table thead th")
        self.assertEqual([words(h) for h in heads], ["When", "Result", "New filings found", "Status"])
        self.assertTrue(all(h.get("scope") == "col" for h in heads))
        self.assertIn("By registry", [words(h) for h in soup.select(".card h2")])
        self.assertNotIn("By source", words(soup))
        result = soup.select("table.table tbody tr td")[1]
        self.assertNotIn("nowrap", result.get("class", []))                          # a long result may wrap instead of pushing the status out

    def test_the_empty_states(self):
        admin = self.user_client()
        none = words(soup_of(admin.get("/").get_data(as_text=True)).find("div", class_="empty"))
        self.assertIn("usually takes under 10 minutes; up to an hour is possible", none)        # measured: 449 s first run, 39 min worst case
        self.assertNotIn("half an hour", none)
        self.assertNotIn("check out", none)
        self.log_run(added=0, pushed=0, held=0, seconds=60)
        quiet = words(soup_of(admin.get("/").get_data(as_text=True)).find("div", class_="empty"))
        self.assertNotIn("held back", quiet)                                          # nothing is held, so nothing is said about it
        self.seed_held(12)
        held = words(soup_of(admin.get("/").get_data(as_text=True)).find("div", class_="empty"))
        self.assertIn("none has a matched website together with the contact details required in Settings", held)
        self.assertNotIn("an email and a phone", held)                              # Settings can turn Phone off or accept either
        self.assertIn("12 are held back and checked again after 3, 10, and 30 days", held)
        self.assertIn("To see more, relax what is required in Settings.", held)
        self.assertNotIn("verified", held)
        member = self.member_client()
        held = words(soup_of(member.get("/").get_data(as_text=True)).find("div", class_="empty"))
        self.assertIn("an admin can relax what is required in Settings", held)


class SettingsPageTests(PageCase):
    def setUp(self):
        super().setUp()
        self.html = self.user_client().get("/settings").get_data(as_text=True)
        self.soup = soup_of(self.html)

    def test_structure_and_hygiene(self):
        hygiene(self, self.soup, "settings")
        self.assertEqual(words(self.soup.h1), "Settings")
        self.assertEqual(self.soup.find("main")["id"], "main")
        self.assertEqual([words(h) for h in self.soup.find_all("h2")], ["Google Sheet", "Daily run", "What gets listed"])
        self.assertIsNotNone(self.soup.find("a", class_="skip"))

    def test_every_checkbox_has_a_short_name_and_its_help_is_a_description(self):
        boxes = self.soup.select("label.check input[type=checkbox]")
        self.assertEqual(len(boxes), 12)                                            # 1 sharing + 6 rules + 5 registries
        helped = 0
        for box in boxes:
            name = accessible_name(self.soup, box)
            self.assertTrue(name)
            self.assertLessEqual(len(name), 60, name)                                 # was a whole paragraph (up to 300 characters)
            if box.get("aria-describedby"):
                helped += 1
                self.assertGreater(len(text_of_id(self.soup, box["aria-describedby"])), 10)
        self.assertEqual(helped, 6)
        for stale in ("tick", "untick"):
            self.assertNotIn(stale, words(self.soup).lower().split())

    def test_checked_stays_right_after_the_id(self):
        """Other tests (and the script that reads the page back) look for id="r-it" checked."""
        self.assertRegex(self.html, r'<input type="checkbox" id="r-it" checked ')
        self.assertRegex(self.html, r'<input type="checkbox" data-req="phone" checked ')
        self.assertRegex(self.html, r'<input type="checkbox" id="g-link" checked ')

    def test_the_time_field_has_a_visible_label_not_only_an_aria_label(self):
        field = self.soup.find(id="s-time")
        self.assertIsNone(field.get("aria-label"))
        label = self.soup.find("label", attrs={"for": "s-time"})
        self.assertEqual(words(label), "Time of day")
        self.assertEqual(accessible_name(self.soup, field), "Time of day")

    def test_wording(self):
        text = words(self.soup)
        self.assertIn("A one-time setup of about two minutes.", text)
        self.assertIn("Needed to show the sheet inside Hybrid Leads. With it off, or on a work or school Google account that blocks link sharing, "
                      "the preview is a plain table instead.", text)
        self.assertIn("Save changes", text)
        self.assertIn("everything turned on here", text)
        self.assertEqual(words(self.soup.find(id="reg-label")), "Registries")
        self.assertEqual(self.soup.find(id="reg-label").find_parent(attrs={"role": "group"})["aria-labelledby"], "reg-label")
        steps = self.soup.select("ol.setup li")
        self.assertEqual(len(steps), 5)
        self.assertIn("Google hasn't verified this app", words(steps[3]))
        note = steps[3].find(class_="step-note")                                       # the long explanation is a note, not part of the step
        self.assertIn("The warning is normal for a script you own.", words(note))
        self.assertNotIn("The warning is normal", words(steps[3]).replace(words(note), ""))
        self.assertNotIn("Sources", words(self.soup.find(id="rules")))
        self.assertNotIn("&rsquo;", self.html)                                          # one kind of apostrophe everywhere


if __name__ == "__main__":
    unittest.main()
