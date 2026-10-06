"""The public start page and the docs that repeat it: pin the factual structure so the claims cannot drift back.

Nothing here touches the network or Google. The page is rendered by the real app (signed out), parsed, and each claim that
can be checked against the code (retry days, registries, column names, mailbox rules) is checked against it."""
import re
import sys
import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from itleads import config, sheet, store  # noqa: E402
from itleads.enrich import NOT_A_CONTACT, contacts, ESTABLISHED_YEARS, itclass  # noqa: E402
from itleads.sources import registries  # noqa: E402
from itleads import service  # noqa: E402
from itleads.web import auth, create_app, export  # noqa: E402

STATIC = ROOT / "itleads" / "web" / "static"
HEADLINE = "New IT company filings, checked every day."
CAPTION = "Rounded from September 2026 and the 45 days to 6 Oct 2026; the real numbers move with the registries."
# registry label -> (width class on the bar, "about N a month"); Texas (450) is the full bar
VOLUMES = {"Texas": ("w100", 450), "Connecticut": ("w56", 250), "San Francisco": ("w12", 55), "Seattle": ("w4", 20),
           "Los Angeles": ("w1", 5)}


def words(soup_or_tag) -> str:
    """Visible text of a page or a part of it, one space between pieces (visually hidden text included: a screen reader reads it)."""
    return re.sub(r"\s+", " ", soup_or_tag.get_text(" ")).strip()


class LandingCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls._saved = (config.ROOT, config.DATA, config.LOGS, config.CONFIG_PATH)
        config.ROOT, config.DATA, config.LOGS, config.CONFIG_PATH = root, root / "data", root / "logs", root / "config.json"
        config.ensure_dirs()
        app = create_app()
        app.testing = True
        cls.html = app.test_client().get("/").get_data(as_text=True)
        cls.soup = BeautifulSoup(cls.html, "lxml")
        cls.css = (STATIC / "landing.css").read_text()
        cls.main = cls.soup.select_one("main")
        cls.text = words(cls.soup.select_one("body"))

    @classmethod
    def tearDownClass(cls):
        config.ROOT, config.DATA, config.LOGS, config.CONFIG_PATH = cls._saved
        cls._tmp.cleanup()


class HeadlineAndStructure(LandingCase):
    def test_one_h1_with_the_decided_headline_and_the_title_repeats_it(self):
        h1 = self.soup.find_all("h1")
        self.assertEqual([words(h) for h in h1], [HEADLINE])
        self.assertEqual(words(self.soup.title), "Hybrid Leads · " + HEADLINE.rstrip("."))
        self.assertTrue(self.soup.select_one('link[href*="landing.css"]'), "landing.css is loaded after app.css")

    def test_headings_never_skip_a_level_and_the_care_note_is_an_h2(self):
        levels = [int(h.name[1]) for h in self.soup.find_all(re.compile(r"^h[1-6]$"))]
        self.assertEqual(levels[0], 1)
        for prev, cur in zip(levels, levels[1:]):
            self.assertLessEqual(cur, prev + 1, levels)
        self.assertEqual(self.soup.select_one("#care-h").name, "h2")
        self.assertEqual(self.soup.select_one("section.care")["aria-labelledby"], "care-h")

    def test_nav_labels_and_anchors(self):
        nav = [(a.get_text(strip=True), a["href"]) for a in self.soup.select("nav.l-nav a")]
        self.assertEqual(nav, [("How it works", "#how"), ("Proof", "#proof"), ("What you get", "#data"), ("Registries", "#sources")])
        ids = {t["id"] for t in self.soup.select("[id]")}
        for a in self.soup.select('a[href^="#"]'):
            self.assertIn(a["href"][1:], ids, a)
        self.assertEqual(self.soup.select_one("a.skip")["href"], "#main")

    def test_filing_is_defined_once(self):
        self.assertEqual(self.text.count("A filing is any new registration, permit, or license."), 1)

    def test_external_links_say_they_open_a_new_tab(self):
        links = self.soup.select('a[target="_blank"]')
        self.assertTrue(links)
        for a in links:
            self.assertIn("noopener", a["rel"])
            self.assertIn("(opens in a new tab)", words(a), a)
        for a in self.soup.select('a[href^="http"]'):
            self.assertEqual(a.get("target"), "_blank", a)

    def test_decorative_svgs_and_the_checking_state_are_hidden_from_screen_readers(self):
        for svg in self.soup.select("svg"):
            self.assertEqual(svg.get("aria-hidden"), "true", svg)
        for el in self.soup.select(".chip.wait, .sum-wait"):
            self.assertEqual(el.get("aria-hidden"), "true", el)
        self.assertTrue(self.soup.select_one("figure.board")["aria-label"])

    def test_no_inline_style_script_or_event_handler(self):                                   # the content policy is strict
        self.assertEqual(self.soup.select("[style]"), [])
        self.assertEqual(self.soup.select("style"), [])
        self.assertEqual([s for s in self.soup.select("script") if not s.get("src")], [])
        for el in self.soup.find_all(True):
            self.assertEqual([a for a in el.attrs if a.lower().startswith("on")], [], el.name)


class Wording(LandingCase):
    def test_no_promised_time_and_no_morning(self):
        for bad in ("07:00", "every morning", "found every", "Open today's list"):
            self.assertNotIn(bad, self.html, bad)
        self.assertIn("once a day, at the time set in Settings", self.text)
        strip = [words(li) for li in self.soup.select(".strip-in li")]
        self.assertEqual(strip[1], "Daily automatic run, at a time you set")

    def test_the_word_verified_appears_only_as_the_column_name(self):
        for m in re.finditer(r"(?<![A-Za-z])verified", self.text, re.I):
            self.assertEqual(self.text[m.start():m.start() + 11], "Verified by", self.text[max(0, m.start() - 30):m.end() + 30])
        self.assertIn("Verified by", [h for h, _, _ in export.COLUMNS])
        self.assertIn("Fit", [h for h, _, _ in export.COLUMNS])

    def test_overstatements_stay_out(self):
        for bad in ("never taken", "is ignored", "No sign of IT work, no listing", "guessed domain is not good enough",
                    "The same columns appear", "mostly individuals", "Business licences"):
            self.assertNotIn(bad, self.text, bad)

    def test_one_term_per_concept(self):
        plain = re.sub(r"Hybrid Leads", "", words(self.main) + " " + words(self.soup.select_one("footer p")))
        for pat in (r"\bthe tool\b", r"\bthis app\b", r"\bprincipals?\b", r"\bre-?checked\b", r"\bleads?\b", r"\bsources\b"):
            self.assertIsNone(re.search(pat, plain, re.I), pat)

    def test_american_spelling_and_serial_commas(self):
        for pat in (r"licence", r"honou?r(?!ed)", r"honoured", r"organis", r"\btick", r"colour"):
            self.assertIsNone(re.search(pat, self.text, re.I), pat)
        for lst in ("Texas, Connecticut, Seattle, San Francisco, and Los Angeles", "Press, legal, billing, and hiring mailboxes",
                    "an email, and a phone", "state telemarketing laws, and the Do Not Call registry"):
            self.assertIn(lst, self.text)

    def test_hero_sample_site_is_one_the_app_would_try(self):
        sites = [s.get_text(strip=True) for s in self.soup.select(".co-site")]
        self.assertIn("quillsecurity.com", sites)
        self.assertNotIn("quillsec.com", sites)
        from itleads.enrich import website
        rec = {"name": "Quill Security LLC", "trade_name": "", "emails": []}
        self.assertIn(("quillsecurity.com", False), website.candidates(rec))             # a domain the app would really try

    def test_the_board_is_four_listed_and_one_held_back(self):
        rows = self.soup.select(".board-rows .co-row")
        self.assertEqual(len(rows), 5)
        self.assertEqual(len([r for r in rows if "held" in r.get("class", [])]), 1)
        self.assertEqual(words(self.soup.select_one(".sum-done")), "4 listed, 1 held back")


class ClaimsMatchTheCode(LandingCase):
    def test_retry_days(self):
        self.assertEqual(store.RETRY_DAYS, [0, 3, 10, 30])
        self.assertIn("checked again 3, 10, and 30 days after it first appears, and dropped", self.text)
        self.assertIn("Checked again in 3 days", self.text)

    def test_the_five_registries(self):
        labels = {c.label for c in registries.ALL.values()}
        self.assertEqual(labels, set(VOLUMES))
        names = [li.select_one(".src-name b").get_text(strip=True) for li in self.soup.select(".src li")]
        self.assertEqual(sorted(names), sorted(labels))
        self.assertEqual(words(self.soup.select_one("#sources h2")), "Five public registries.")
        self.assertEqual(words(self.soup.select(".strip-in li")[0]), "%d public business registries" % len(registries.ALL))

    def test_four_kinds_of_proof_and_three_ways_to_get_the_list(self):
        self.assertEqual(len(self.soup.select(".proof")), 4)
        strip = [words(li) for li in self.soup.select(".strip-in li")]
        self.assertTrue(strip[2].startswith("4 kinds of proof, checked for every website"), strip[2])
        self.assertTrue(strip[3].startswith("3 ways to get the list: Google Sheet, Excel, CSV"), strip[3])

    def test_the_two_weaker_proof_routes_are_disclosed(self):
        intro = words(self.soup.select_one("#proof .intro"))
        for part in ("A weaker match is accepted in a few cases", "between ten months before and two months after the filing",
                     "exact name as the domain", "also need the site to name the filing's city", "Verified by column shows which check passed"):
            self.assertIn(part, intro)
        proof = words(self.soup.select_one("#proof"))
        self.assertIn("A name of one word also needs the filing's city on the same page.", proof)           # website.py: city_hit or 2+ name words
        self.assertIn("On the terms or contact page, any name also needs the filing's city or ZIP code.", proof)       # website._fine_print
        self.assertIn("A website must carry the company's name, on the page or in its domain,", proof)      # website.check_domain: in_head or in_body or in_domain
        self.assertIn("in the page title, headings, or description", proof)                                 # contacts._head_text reads the meta description too
        self.assertNotIn("page title or headings", proof)

    def test_the_it_work_callout_matches_the_classifier_thresholds(self):
        callout = words(self.soup.select_one(".callout"))
        self.assertIn("shows almost nothing about software, IT services, or cloud work", callout)
        self.assertIn("A site with almost no readable text is kept and marked Unverified.", callout)
        self.assertNotIn("shows nothing", callout)
        self.assertNotIn("no readable text", callout.replace("almost no readable text", ""))
        # the code behind it: one strong word in the page title keeps a site; a text-less page is "unknown", not dropped
        self.assertEqual(itclass.classify("Cloud software company", "x" * 500)["level"], "strong")
        self.assertEqual(itclass.classify("", "")["level"], "unknown")
        self.assertEqual(itclass.classify("Welcome", "word " * 120)["level"], "none")

    def test_mailbox_and_phone_skip_rules_exist_in_the_code(self):
        for local in ("press", "legal", "billing", "careers", "jobs", "hr"):
            self.assertTrue(NOT_A_CONTACT.match(local), local)
        for label in ("press contact", "media contact", "complaints"):
            self.assertTrue(contacts.OTHER_PEOPLES.search(label), label)
        self.assertIn("Press, legal, billing, and hiring mailboxes", self.text)
        self.assertIn("labeled as a press, media, or complaints contact", self.text)

    def test_fit_labels_and_the_three_year_threshold(self):
        dd = words(next(d for d in self.soup.select(".spec dl > div") if words(d.dt) == "Fit").dd)
        for label in sheet.FIT_LABEL.values():
            self.assertIn(label, dd)
        self.assertEqual(ESTABLISHED_YEARS, 3)
        self.assertIn("3 or more years older", dd)

    def test_the_columns_the_page_describes(self):
        headers = [h for h, _, _ in export.COLUMNS]
        for h in ("Company", "Website", "Email", "Phone", "Address", "Registered", "Contact", "LinkedIn", "Fit", "Verified by"):
            self.assertIn(h, headers)
        dts = [words(d) for d in self.soup.select(".spec dt")]
        self.assertEqual(dts, ["Company", "Website", "Email", "Phone", "Address", "Contact", "Fit", "Delivery"])
        # "the Google Sheet and the preview show the first six": the sheet script groups everything after VISIBLE, the preview has six
        self.assertRegex((ROOT / "itleads" / "appscript" / "Code.gs.tpl").read_text(), r"var VISIBLE = 6;")
        heads = re.search(r"const HEADS = \[([^\]]*)\]", (STATIC / "app.js").read_text()).group(1)
        self.assertEqual(len(re.findall(r'"[^"]+"', heads)), 6)
        self.assertEqual(headers[:6], [m for m in re.findall(r'"([^"]+)"', heads)])
        self.assertIn("show the first six", words(self.soup.select_one("#data .intro")))


class VolumesAndBars(LandingCase):
    def test_each_registry_has_its_volume_and_bar_class(self):
        seen = {}
        for li in self.soup.select(".src li"):
            name = li.select_one(".src-name b").get_text(strip=True)
            bar = li.select_one(".src-bar i")["class"]
            num = re.fullmatch(r"about (\d+) a month", li.select_one(".src-num").get_text(strip=True))
            self.assertTrue(num, name)
            seen[name] = (bar[0], int(num.group(1)))
        self.assertEqual(seen, VOLUMES)

    def test_bar_widths_are_in_proportion_and_defined_in_the_css_once(self):
        defined = dict(re.findall(r"\.src-bar \.(w\d+)\{width:(\d+)%\}", self.css))
        used = {cls for cls, _ in VOLUMES.values()}
        self.assertEqual(set(defined), used, "every bar class is defined, and none is left over")
        top = max(n for _, n in VOLUMES.values())
        for cls, n in VOLUMES.values():
            self.assertEqual(int(defined[cls]), int(cls[1:]), cls)                          # the class name is the width
            self.assertAlmostEqual(int(defined[cls]), 100 * n / top, delta=1, msg=cls)       # and it follows the volume
        for stale in ("w46", "w15", "w6", "w52", "w11"):
            self.assertNotIn(stale, self.css)
            self.assertNotIn(stale, self.html)

    def test_what_each_registry_gives(self):
        gives = {li.select_one(".src-name b").get_text(strip=True): words(li.select_one(".src-gives")) for li in self.soup.select(".src li")}
        self.assertEqual(gives["Connecticut"], "Industry code, address, email, and officers")
        self.assertEqual(gives["San Francisco"], "Industry code and address; about 4 in 10 are individuals")
        self.assertEqual(gives["Seattle"], "Industry code and address. Its phone number is only used to check the website.")
        self.assertEqual(gives["Texas"], "Industry code and address")
        self.assertEqual(gives["Los Angeles"], "Industry code and address")
        note = words(self.soup.select_one(".src-note"))
        self.assertIn(CAPTION, note)
        self.assertNotIn("measured over", note)

    def test_the_guide_table_repeats_the_same_numbers(self):
        guide = (ROOT / "docs" / "guide.md").read_text()
        rows = {}
        for line in guide.splitlines():
            m = re.match(r"\|\s*(Texas|Connecticut|San Francisco|Seattle|Los Angeles)\b[^|]*\|[^|]*\|\s*about (\d+)\s*\|", line)
            if m:
                rows[m.group(1)] = int(m.group(2))
        self.assertEqual(rows, {k: n for k, (_, n) in VOLUMES.items()})
        self.assertIn("6 Oct 2026", guide)


class MotionAndLayout(LandingCase):
    @staticmethod
    def _seconds(token):
        token = token.strip()
        return float(token[:-2]) / 1000 if token.endswith("ms") else float(token[:-1])

    def test_the_hero_animation_ends_by_4_9_seconds(self):
        """Worst case over every rule: delay + duration. A rule that only sets a delay borrows the longest settle animation."""
        css = re.sub(r"/\*.*?\*/", "", self.css, flags=re.S)
        time_token = re.compile(r"(?<![\w.-])(\d*\.?\d+m?s)\b")
        rules = []
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            decl = {k.strip(): v.strip() for k, v in (d.split(":", 1) for d in body.split(";") if ":" in d)}
            short = decl.get("animation", "")
            if "none" in short.split():
                continue
            times = [self._seconds(t) for t in time_token.findall(short)]
            duration = self._seconds(decl["animation-duration"]) if "animation-duration" in decl else (times[0] if times else None)
            delay = self._seconds(decl["animation-delay"]) if "animation-delay" in decl else (times[1] if len(times) > 1 else None)
            if duration is not None or delay is not None:
                rules.append((sel.strip(), duration, delay, bool(short) and "waiting" not in short))
        settle = max(d for _, d, _, is_settle in rules if d is not None and is_settle)       # the settle/rise shorthands
        worst = max((delay or 0.0) + (duration if duration is not None else settle) for _, duration, delay, _ in rules if delay is not None)
        self.assertLessEqual(worst, 4.9 + 1e-9, "every animation must be finished by 4.9 s (WCAG 2.2.2)")
        self.assertGreater(worst, 4.0)                                           # the choreography is still there

    def test_reduced_motion_shows_the_finished_board(self):
        m = re.search(r"@media \(prefers-reduced-motion:reduce\)\{(.*?)\n\}", self.css, re.S)
        self.assertTrue(m)
        block = m.group(1)
        self.assertIn("animation:none", block)
        self.assertRegex(block, r"\.chip\.wait,\.board-sum \.sum-wait\{display:none\}")

    def test_layout_fixes_stay(self):
        self.assertRegex(self.css, r"\.sec\{[^}]*scroll-margin-top:24px")
        self.assertRegex(self.css, r"\.callout p\{[^}]*max-width:\d+ch")          # a bounded line length (the CSS owner sets the exact measure)
        self.assertRegex(self.css, r"\.src-note\{[^}]*max-width:\d+ch")
        self.assertRegex(self.css, r"@media \(max-width:980px\)\{[^@]*\.board\{max-width:560px\}")
        self.assertRegex(self.css, r"@media \(max-width:480px\)\{\s*\.cta \.btn\{flex:1 1 100%\}")
        self.assertRegex(self.css, r"@media \(max-width:345px\)")
        self.assertRegex(self.css, r"@media \(max-height:500px\)")
        proofs_980 = re.search(r"@media \(max-width:980px\)\{(.*?)\n\}", self.css, re.S).group(1)
        self.assertNotIn("grid-template-columns:minmax(0,1fr);margin-top:36px", proofs_980)       # two columns until 640
        self.assertRegex(re.search(r"@media \(max-width:640px\)\{(.*?)\n\}", self.css, re.S).group(1), r"\.proofs\{grid-template-columns:minmax\(0,1fr\)\}")


class Docs(unittest.TestCase):
    def test_readme_is_short_and_makes_no_promise_of_a_time(self):
        readme = (ROOT / "README.md").read_text()
        self.assertLessEqual(len(readme.splitlines()), 45)
        for needle in ("07:00", "every morning"):
            self.assertNotIn(needle, readme)
        self.assertIn("once a day, at the time set in Settings", readme)
        self.assertIn("not legal advice", readme)

    def test_docs_use_american_spelling_and_the_decided_terms(self):
        for name in ("README.md", "docs/guide.md"):
            text = (ROOT / name).read_text()
            for pat in (r"licence", r"honour", r"organis", r"\buntick", r"\btick(ed)?\b", r"principals", r"re-checked", r"every morning"):
                self.assertIsNone(re.search(pat, text, re.I), (name, pat))

    def test_the_guide_states_no_time_of_day(self):
        for name in ("README.md", "docs/guide.md"):
            self.assertIsNone(re.search(r"\b\d{1,2}:\d{2}\b", (ROOT / name).read_text()), name)      # the schedule is configurable

    def test_docs_say_hybrid_leads_not_the_tool_or_this_app(self):
        guide = (ROOT / "docs" / "guide.md").read_text()
        for pat in (r"\bthe tool\b", r"\bthis app\b"):
            self.assertIsNone(re.search(pat, guide, re.I), pat)
        self.assertIn("To update Hybrid Leads, replace the files", guide)

    def test_the_guide_caption_and_the_arrival_pattern(self):
        guide = (ROOT / "docs" / "guide.md").read_text()
        self.assertIn(CAPTION.replace(". ", ". "), re.sub(r"\s+", " ", guide))
        self.assertNotIn("Measured over the 45 days", guide)
        flat = re.sub(r"\s+", " ", guide)
        self.assertIn("Texas loads in weekly batches and the Los Angeles feed refreshes monthly, so those two arrive in bursts", flat)
        for bad in ("Saturday", "the 15th", "loads weekly"):
            self.assertNotIn(bad, guide, bad)

    def test_first_run_duration_is_not_promised_as_an_upper_bound(self):
        guide = re.sub(r"\s+", " ", (ROOT / "docs" / "guide.md").read_text())
        self.assertIn("usually takes under 10 minutes", guide)
        self.assertIn("up to an hour is possible", guide)
        for bad in ("half an hour", "a run takes minutes", "15 to 30 minutes"):
            self.assertNotIn(bad, guide, bad)

    def test_the_guide_matches_the_code_on_columns_environment_lockout_and_sheet(self):
        guide = re.sub(r"\s+", " ", (ROOT / "docs" / "guide.md").read_text())
        tpl = (ROOT / "itleads" / "appscript" / "Code.gs.tpl").read_text()
        sheet_cols = len(re.findall(r"\['[a-z_]+', '[^']+', \d+\]", tpl.split("var VISIBLE")[0]))
        self.assertEqual((len(export.COLUMNS), sheet_cols), (14, 16))
        self.assertIn("the same company columns as the sheet (14); the sheet also keeps Added and ID columns (16 in all)", guide)
        self.assertNotIn("same columns as the sheet.", guide)
        # install-service carries only these four variables, so the guide must not say it copies the login ones
        self.assertEqual(set(service.CARRIED_ENV), {"ITLEADS_HOST", "ITLEADS_PORT", "ITLEADS_TRUSTED_PROXY", "ITLEADS_SECURE_COOKIES"})
        self.assertIn("`install-service` copies only `ITLEADS_HOST`, `ITLEADS_PORT`, `ITLEADS_TRUSTED_PROXY` and `ITLEADS_SECURE_COOKIES`", guide)
        self.assertNotIn("and `install-service` copies them", guide)
        # ITLEADS_ALLOWED_HOSTS is read by the app, and listed in the guide
        self.assertIn("ITLEADS_ALLOWED_HOSTS", (ROOT / "itleads" / "web" / "__init__.py").read_text())
        self.assertIn("`ITLEADS_ALLOWED_HOSTS`", guide)
        # the lockout window is 15 minutes, and reset-password for the team email only lifts the lockout
        self.assertEqual(auth.WINDOW, 15 * 60)
        self.assertIn("lifts by itself within 15 minutes", guide)
        self.assertNotIn("wait a few minutes", guide)
        self.assertIn("For the team email that is all it does", guide)
        # the Dashboard tab is cleared and rewritten; the Companies tab is never cleared
        self.assertIn("sh.clear();", tpl.split("function formatDashboard_")[1].split("\n}")[0])
        self.assertNotIn("Nothing is ever deleted", guide)
        self.assertIn("never deletes a company row from your sheet's Companies tab; only the Dashboard tab is cleared and rewritten", guide)


if __name__ == "__main__":
    unittest.main()
