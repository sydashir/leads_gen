"""Stylesheet facts that a browser measurement showed matter (no browser here: the numbers below came from real Chrome
sweeps on a throwaway server, and these tests keep the rules that produced them).

Covers: long unbroken text wrapping inside the dashboard, the sign-in mark box, the pinned company column in the phone
preview, the landing headline staying on two lines, the landing line lengths, and the landing header that stops
following you on small screens. Python 3.9 and 3.13."""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STATIC = ROOT / "itleads" / "web" / "static"


def read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def parse(css):
    """[(context, selectors, declarations)] for every style rule. context is the tuple of enclosing @media conditions
    (the text after @media, whitespace removed); keyframes and font-face blocks are skipped."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules = []
    stack = []                                   # ("media", prelude) | ("skip", "") | ("rule", [selectors])
    buf = ""
    body = ""
    for ch in css:
        if ch == "{":
            prelude = buf.strip()
            buf = ""
            if stack and stack[-1][0] in ("skip", "rule"):
                stack.append(("skip", ""))
            elif prelude.startswith("@media") or prelude.startswith("@supports"):
                stack.append(("media", re.sub(r"\s+", "", re.sub(r"^@\w+", "", prelude))))
            elif prelude.startswith("@"):
                stack.append(("skip", ""))
            else:
                stack.append(("rule", [s.strip() for s in prelude.split(",")]))
                body = ""
        elif ch == "}":
            kind, value = stack.pop()
            if kind == "rule":
                decls = {}
                for part in body.split(";"):
                    if ":" in part:
                        k, v = part.split(":", 1)
                        decls[k.strip()] = v.strip()
                context = tuple(v for k, v in stack if k == "media")
                rules.append((context, value, decls))
            buf = ""
            body = ""
        else:
            if stack and stack[-1][0] == "rule":
                body += ch
            else:
                buf += ch
    return rules


def declarations(rules, selector, context=None):
    """All declarations that apply to exactly this selector (it may share a rule with others), later rules winning.
    context=None means the top level only; a string means inside that @media prelude (whitespace removed)."""
    wanted = () if context is None else (re.sub(r"\s+", "", context),)
    merged = {}
    for ctx, selectors, decls in rules:
        if ctx == wanted and selector in selectors:
            merged.update(decls)
    return merged


class AppCssFixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css = read("app.css")
        cls.rules = parse(cls.css)

    def test_the_parser_reads_the_media_blocks_it_is_used_for(self):
        self.assertEqual(declarations(self.rules, ".btn.small")["min-height"], "32px")
        self.assertEqual(declarations(self.rules, ".actions", "(max-width:640px)")["display"], "grid")
        self.assertEqual(declarations(self.rules, ".actions")["display"], "flex")       # the phone rule is not the base rule

    def test_a_long_unbroken_string_wraps_instead_of_pushing_the_page_sideways(self):
        """A 71-character word in the dashboard sub line or in a registry/state label made the page 914 px wide on a 320 px
        phone and 1877 px wide on a 1440 px desktop. With these rules the page keeps its width at 320, 390, 800 and 1440."""
        sub = declarations(self.rules, ".page-head .sub")
        self.assertEqual(sub.get("overflow-wrap"), "anywhere")
        label = declarations(self.rules, ".list span:first-child")
        self.assertEqual(label.get("min-width"), "0")                           # a flex item may shrink below its longest word
        self.assertEqual(label.get("overflow-wrap"), "anywhere")
        self.assertNotIn("overflow-wrap", declarations(self.rules, ".list span:last-child"))   # the number stays whole

    def test_the_sign_in_mark_box_is_as_tall_as_the_drawing(self):
        """The 740x560 width/height attributes gave a 560 px tall box around a drawing 482 px tall (39 px of empty band
        above and below). Auto height with the drawing's own ratio makes the box the drawing."""
        mark = declarations(self.rules, ".auth-mark")
        self.assertEqual(mark.get("height"), "auto")
        view_box = re.search(r'viewBox="0 0 (\d+) (\d+)"', read("mark.svg"))
        self.assertTrue(view_box)
        ratio = re.sub(r"\s+", "", mark.get("aspect-ratio", ""))
        self.assertEqual(ratio, "%s/%s" % (view_box.group(1), view_box.group(2)), "the ratio must be the drawing's viewBox")
        # the mark was centred 44.8 px above the middle of the panel; the same offset holds at every width now
        self.assertIn("calc(-50% - 45px)", mark.get("transform", ""))

    def test_the_company_column_stays_pinned_and_readable_in_the_phone_preview(self):
        phone = "(max-width:640px)"
        both = declarations(self.rules, ".pv-table th:first-child", phone)
        cell = declarations(self.rules, ".pv-table td:first-child", phone)
        self.assertEqual(both.get("position"), "sticky")
        self.assertEqual(both.get("left"), "0")
        # a collapsed table border does not travel with a sticky cell, so the line on its right edge is an inset shadow
        self.assertRegex(both.get("box-shadow", ""), r"^inset -1px 0 0 var\(--line\)$")
        # one fixed width, so the scroll padding below can match it exactly
        width = both.get("width")
        self.assertEqual(width, "8.5rem")
        self.assertEqual(both.get("min-width"), width)
        self.assertEqual(both.get("max-width"), width)
        # opaque: the other columns pass underneath without showing through
        root = declarations(self.rules, ":root")
        for token in ("--panel", "--raise"):
            self.assertRegex(root[token], r"^#[0-9a-f]{6}$", token + " must be opaque")
        self.assertEqual(cell.get("background"), "var(--panel)")
        self.assertEqual(declarations(self.rules, ".pv-table th").get("background"), "var(--raise)")
        self.assertGreater(int(declarations(self.rules, ".pv-table th:first-child", phone).get("z-index", "0")),
                           int(cell.get("z-index", "0")))                        # the header corner sits above the body cells
        # the other columns settle just right of the pinned one, so a column's header and its first characters stay in view
        box = declarations(self.rules, ".pv-table", phone)
        self.assertEqual(box.get("scroll-padding-left"), width)
        self.assertEqual(box.get("scroll-snap-type"), "x proximity")             # never "mandatory": a column wider than the screen must stay reachable
        self.assertEqual(declarations(self.rules, ".pv-table th", phone).get("scroll-snap-align"), "start")
        self.assertEqual(both.get("scroll-snap-align"), "none")                  # the pinned column never snaps

    def test_the_pinned_column_is_phone_only(self):
        for ctx, selectors, decls in self.rules:
            if ".pv-table th:first-child" in selectors:
                self.assertEqual(ctx, ("(max-width:640px)",), "the pin and the snapping belong to phones only")

    def test_the_rules_message_names_dropped_companies_too(self):
        """Saving the rules also releases companies that had already been dropped (requalify), so the message says both."""
        js = read("app.js")
        self.assertIn('"companies that were") + " held back or dropped now "', js)
        self.assertNotIn(" held back now ", js)


class LandingCssFixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css = read("landing.css")
        cls.rules = parse(cls.css)

    def test_the_headline_stays_on_two_lines_from_981_to_1920_px(self):
        """At 1365 px the headline (4.9vw, 66.9 px) no longer fitted its 715 px column by about 1 px and jumped from two
        lines to three. A 1 px sweep of 981-1920 with this cap shows two lines at every width (fits up to 66.8 px)."""
        size = declarations(self.rules, ".hero h1").get("font-size", "")
        m = re.fullmatch(r"clamp\((\d+)px,([\d.]+)vw,(\d+)px\)", size)
        self.assertTrue(m, size)
        self.assertLessEqual(int(m.group(3)), 66)
        self.assertGreaterEqual(int(m.group(3)), 56)                              # still a big headline
        # the column it must fit: 1320 wrap, 32 px side padding, 64 px gap, 1.2fr of 2fr
        column = (1320 - 64 - 64) * 1.2 / 2
        self.assertAlmostEqual(column, 715.2, places=1)

    def test_lines_stay_near_seventy_to_eighty_characters(self):
        self.assertEqual(declarations(self.rules, ".callout p").get("max-width"), "56ch")
        self.assertEqual(declarations(self.rules, ".src-note").get("max-width"), "62ch")
        self.assertEqual(declarations(self.rules, ".paper .intro").get("max-width"), "34em")

    def test_the_header_stops_following_you_on_phones_and_short_windows_only(self):
        self.assertEqual(declarations(self.rules, ".l-bar").get("position"), "sticky")            # desktop keeps it
        small = "(max-width:640px),(max-height:520px)"
        self.assertEqual(declarations(self.rules, ".l-bar", small).get("position"), "static")
        # anchors still land below the 77 px header on a desktop: each section brings its own padding (80 px or more)
        bar = int(declarations(self.rules, ".l-bar-in").get("height", "0").replace("px", "")) + 1
        sec = declarations(self.rules, ".sec")
        padding_min = int(re.search(r"clamp\((\d+)px", sec["padding-block"]).group(1))
        margin = int(sec["scroll-margin-top"].replace("px", ""))
        self.assertGreater(padding_min + margin, bar)
        self.assertEqual(declarations(self.rules, "html").get("scroll-padding-top"), "0")

    def test_the_small_screen_rules_come_after_the_base_rule(self):
        first_base = min(i for i, (ctx, sel, _) in enumerate(self.rules) if ctx == () and ".l-bar" in sel)
        small = [i for i, (ctx, sel, _) in enumerate(self.rules)
                 if ctx == ("(max-width:640px),(max-height:520px)",) and ".l-bar" in sel]
        self.assertTrue(small)
        self.assertGreater(min(small), first_base)


if __name__ == "__main__":
    unittest.main()
