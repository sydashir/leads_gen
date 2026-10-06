"""The "Open Google Sheet" button on the dashboard, and Run now on the read-only copy."""
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "deploy" / "vercel"))
from itleads import config  # noqa: E402
from tests.test_web import WebCase  # noqa: E402

SHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"


def connect_google():
    config.update(lambda c: c.update(apps_script_url="https://script.google.com/macros/s/INVENTED/exec", sheet_id=SHEET_ID, token="t"))


class OpenSheetButtonTests(WebCase):
    def dash(self, app=None):
        return self.user_client(app=app).get("/").get_data(as_text=True)

    def test_with_google_connected_the_dashboard_has_a_button_that_opens_the_sheet(self):
        connect_google()
        html = self.dash()
        m = re.search(r'<a class="btn" id="open-sheet" href="([^"]+)" target="_blank" rel="noopener noreferrer">(.*?)</a>', html, re.S)
        self.assertTrue(m, "the button is a plain outline link, Run now stays the main button")
        self.assertEqual(m.group(1), SHEET_URL)
        self.assertIn("Open Google Sheet", m.group(2))
        self.assertIn("(opens in a new tab)", m.group(2))
        self.assertRegex(html, r'id="run"[^>]*aria-disabled="false">')
        self.assertNotRegex(html, r'id="run"[^>]*\bhidden\b')

    def test_without_google_there_is_no_button(self):
        self.assertNotIn('id="open-sheet"', self.dash())

    def test_on_the_read_only_copy_the_sheet_button_is_the_main_button_and_run_now_is_gone(self):
        env = {"ITLEADS_READ_ONLY": "1", "ITLEADS_SHEET_URL": SHEET_URL, "ITLEADS_SNAPSHOT_AT": "6 Oct 2026, 21:58 PKT"}
        with mock.patch.dict("os.environ", env):
            html = self.dash(self.make_app(self.fake_runner))
        self.assertRegex(html, r'<a class="btn primary" id="open-sheet" href="' + re.escape(SHEET_URL) + '"')
        self.assertRegex(html, r'id="run"[^>]*\bhidden\b')                          # kept in the page for the script, never shown
        self.assertNotRegex(re.search(r'<button class="btn primary" id="run"[^>]*>', html).group(0), r"\sdisabled[\s>=]")   # not a dead button
        self.assertIn("Run now is switched off", html)
        self.assertIn("The Google Sheet is updated after every run", html)

    def test_only_a_real_sheet_address_is_accepted_from_the_environment(self):
        for bad in ("javascript:alert(1)", "http://docs.google.com/spreadsheets/d/" + SHEET_ID + "/edit",
                    "https://evil.test/spreadsheets/d/" + SHEET_ID + "/edit", "https://docs.google.com.evil.test/spreadsheets/d/" + SHEET_ID + "/edit",
                    "https://docs.google.com/spreadsheets/d/" + SHEET_ID + "/edit?x=1", "https://docs.google.com/spreadsheets/d/x/edit", ""):
            with mock.patch.dict("os.environ", {"ITLEADS_READ_ONLY": "1", "ITLEADS_SHEET_URL": bad}):
                self.assertNotIn('id="open-sheet"', self.dash(self.make_app(self.fake_runner)), bad)
                self.tearDown_users()

    def test_the_environment_address_is_ignored_when_this_is_not_a_read_only_copy(self):
        with mock.patch.dict("os.environ", {"ITLEADS_SHEET_URL": SHEET_URL}):
            self.assertNotIn('id="open-sheet"', self.dash())

    def tearDown_users(self):
        from itleads.web import appdb
        for u in appdb.list_users():
            appdb.delete_user(u["id"])


class VercelSheetAddressTests(unittest.TestCase):
    def test_the_bundle_carries_the_sheet_address_and_nothing_else_from_the_settings(self):
        import json
        import tempfile
        import build_bundle
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text(json.dumps({"apps_script_url": "https://script.google.com/macros/s/INVENTED/exec", "sheet_id": SHEET_ID, "token": "secret-token-123"}))
            self.assertEqual(build_bundle.sheet_url(cfg), SHEET_URL)
            cfg.write_text(json.dumps({"apps_script_url": "", "sheet_id": SHEET_ID}))
            self.assertEqual(build_bundle.sheet_url(cfg), "")                       # not connected: no button
            cfg.write_text(json.dumps({"apps_script_url": "x", "sheet_id": "../../etc"}))
            self.assertEqual(build_bundle.sheet_url(cfg), "")
            self.assertEqual(build_bundle.sheet_url(Path(tmp) / "missing.json"), "")


if __name__ == "__main__":
    unittest.main()
