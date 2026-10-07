"""End to end: Python client <-> simulated Apps Script web app (strict about the real Google API)."""
import json
import os
import signal
import subprocess
import sys
import unittest
from pathlib import Path

import requests
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from itleads import sheet  # noqa: E402

TOKEN = "unit-test-token"


def lead(i, name, site, email, phone, reg, state="TX", source="tx"):
    raw = {"id": f"{source}:{i}", "source": source, "name": name, "trade_name": "", "state": state, "city": "Austin",
           "address": f"{i} Main St", "zip": "78701", "registered": reg, "industry": "Software Publishers",
           "emails": [], "people": []}
    enr = {"website": f"https://{site}", "email": email, "email_from": "company website", "emails": [],
           "phone": phone, "phone_from": "company website", "why": ["full legal name on the site"],
           "created": "2026-09-01", "contact": {"name": "Ada Lovelace", "title": "Founder", "email": "", "linkedin": ""},
           "linkedin": "", "linkedin_company": "https://www.linkedin.com/company/acme", "it": {"level": "strong"}}
    return {"raw": raw, "enrich": enr}


class BridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = dict(os.environ, FAKE_TOKEN=TOKEN, PORT="0")
        cls.proc = subprocess.Popen(["node", str(ROOT / "tests" / "fake_google.js")], stdout=subprocess.PIPE,
                                    text=True, env=env)
        line = cls.proc.stdout.readline().strip()
        assert line.startswith("PORT "), line
        cls.base = f"http://127.0.0.1:{line.split()[1]}"
        cls.bridge = sheet.Bridge(cls.base + "/macros/s/FAKE/exec", TOKEN, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.proc.send_signal(signal.SIGTERM)
        cls.proc.stdout.close()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()

    def debug(self):
        return requests.get(self.base + "/debug").json()

    def sheet_named(self, name):
        book = self.debug()["books"][0]
        return next(s for s in book["sheets"] if s["name"] == name)

    def test_1_flow(self):
        p = self.bridge.ping()
        self.assertTrue(p["ok"])
        self.assertFalse(p["configured"])

        out = self.bridge.init("Asia/Karachi", ["boss@example.com"])
        self.assertTrue(out["url"].startswith("https://docs.google.com/spreadsheets/d/"))
        book = self.debug()["books"][0]
        self.assertEqual([s["name"] for s in book["sheets"]], ["Dashboard", "Companies", "_runs"])
        self.assertEqual(book["tz"], "Asia/Karachi")
        self.assertEqual(book["editors"], ["boss@example.com"])
        co = self.sheet_named("Companies")
        heads = [c["v"] for c in co["rows"][0] if c["v"]]
        self.assertEqual(heads[:6], ["COMPANY", "WEBSITE", "EMAIL", "PHONE", "ADDRESS", "REGISTERED"])
        self.assertEqual(co["frozen"], [1, 1])
        self.assertTrue(co["filter"])
        self.assertTrue(co["group"] and co["group"]["collapsed"])      # detail columns tucked away
        self.assertTrue(self.sheet_named("_runs")["hidden"])
        dash = self.sheet_named("Dashboard")
        self.assertEqual(dash["rows"][1][1]["v"], "New IT company filings")
        self.assertTrue(any(str(c["v"]).startswith("=COUNTIF") for r in dash["rows"] for c in r))

        # ping now sees the book
        self.assertTrue(self.bridge.ping()["configured"])

        # init is idempotent: no second group, no duplicate tabs
        self.bridge.init("Asia/Karachi", [])
        co = self.sheet_named("Companies")
        self.assertEqual(co["group"]["depth"], 1)
        self.assertEqual(len(self.debug()["books"][0]["sheets"]), 3)

        rows = [sheet.sheet_row(lead(1, "Alpha LLC", "alpha.com", "info@alpha.com", "2062100101", "2026-10-01")),
                sheet.sheet_row(lead(2, "Bravo Inc", "bravo.io", "hi@bravo.io", "4152100102", "2026-10-03")),
                sheet.sheet_row(lead(3, "Charlie Co", "charlie.ai", "team@charlie.ai", "7132100103", "2026-10-02"))]
        r = self.bridge.upsert(rows, "2026-10-05")
        self.assertEqual((r["inserted"], r["updated"]), (3, 0))
        co = self.sheet_named("Companies")
        body = [[c["v"] for c in row] for row in co["rows"][1:4]]
        self.assertEqual([b[0] for b in body], ["Bravo Inc", "Charlie Co", "Alpha LLC"])     # newest filing first
        top = co["rows"][1]
        self.assertEqual(top[1]["v"], "bravo.io")
        self.assertEqual(top[1]["link"], "https://bravo.io")
        self.assertEqual(top[2]["link"], "mailto:hi@bravo.io")
        self.assertEqual(top[3]["v"], "(415) 210-0102")
        self.assertIn("Website:", top[self.colx("Verified by")]["v"])
        self.assertEqual(top[4]["v"], "2 Main St, Austin, TX 78701".replace("2 Main", "2 Main"))
        self.assertEqual(top[5]["v"], "2026-10-03")
        self.assertEqual(top[self.colx("Added")]["v"], "2026-10-05")                                          # Added

        # same leads again plus one new: nothing duplicates
        rows.append(sheet.sheet_row(lead(4, "Delta LLC", "delta.dev", "d@delta.dev", "2142100104", "2026-10-04")))
        r = self.bridge.upsert(rows, "2026-10-06")
        self.assertEqual((r["inserted"], r["updated"]), (1, 3))
        co = self.sheet_named("Companies")
        names = [row[0]["v"] for row in co["rows"][1:] if row[0]["v"]]
        self.assertEqual(len(names), 4)
        self.assertEqual(len(set(names)), 4)
        self.assertEqual(names[0], "Delta LLC")                                              # added on the newest day

        self.bridge.log({"started": "6 Oct 2026, 07:00", "seconds": 42, "added": 1, "held": 9, "updated": 0,
                         "sources": "TX 1", "status": "ok", "errors": ""}, 9, 5, "tomorrow 07:00")
        dash = self.sheet_named("Dashboard")
        self.assertIn("next run tomorrow 07:00", dash["rows"][2][1]["v"])
        self.assertTrue(str(dash["rows"][5][10]["v"]).startswith("=MAX(0,COUNTIFS("))             # the fourth tile counts companies with email and phone
        self.assertNotIn(9, [c["v"] for c in dash["rows"][5]])                               # the held-back count is not shown any more
        self.assertTrue(any(c["v"] == "Texas" for row in dash["rows"] for c in row))          # by-source table

    def colx(self, header):
        """0-based position of a column, read from the sheet's own header row."""
        heads = [c["v"] for c in self.sheet_named("Companies")["rows"][0]]
        return heads.index(header.upper())

    def used_columns(self):
        return len([c for c in self.sheet_named("Companies")["rows"][0] if c["v"]])

    def row_of(self, company):
        co = self.sheet_named("Companies")
        for i, row in enumerate(co["rows"]):
            if row[0]["v"] == company:
                return i + 1, row
        raise AssertionError(company + " not in the sheet")

    def test_4_user_columns_and_edits_survive(self):
        r, row = self.row_of("Delta LLC")
        mine = self.used_columns()                                              # the first column that is not ours
        requests.get(self.base + "/debug/set", params={"sheet": "Companies", "row": r, "col": mine + 1, "value": "Called Monday"})
        requests.get(self.base + "/debug/set", params={"sheet": "Companies", "row": r, "col": 1, "value": "Delta LLC"})
        new = [sheet.sheet_row(lead(10, "Echo LLC", "echo.dev", "e@echo.dev", "2142100110", "2026-10-07")),
               sheet.sheet_row(lead(11, "Foxtrot LLC", "foxtrot.io", "f@foxtrot.io", "2142100111", "2026-10-06"))]
        self.bridge.upsert(new, "2026-10-07")
        self.bridge.init("Asia/Karachi", ["boss@example.com"])                 # re-running setup
        self.bridge.init("Asia/Karachi", [], refresh=True)                      # even a full refresh
        r2, row2 = self.row_of("Delta LLC")
        self.assertEqual(row2[mine]["v"], "Called Monday")                        # the note is still beside its company
        self.assertEqual(r2, r + 2)                                             # two rows went in above it, nothing moved apart
        names = [row[0]["v"] for row in self.sheet_named("Companies")["rows"][1:] if row[0]["v"]]
        self.assertEqual(names[:2], ["Echo LLC", "Foxtrot LLC"])               # newest filing first, at the top
        self.assertNotIn("Sheet.deleteColumns", self.debug()["calls"])           # no column was ever deleted
        self.assertEqual(self.debug()["books"][0]["editors"], ["boss@example.com"])   # not added twice

    def test_5_bad_rows_do_not_sink_the_batch(self):
        good = sheet.sheet_row(lead(20, "Golf LLC", "golf.io", "g@golf.io", "2142100120", "2026-10-08"))
        bad_date = dict(sheet.sheet_row(lead(21, "Hotel LLC", "hotel.io", "h@hotel.io", "2142100121", "2026-10-08")),
                        registered="N/A")
        no_name = dict(good, id="tx:22", company="")
        dup = dict(good)
        formula = dict(sheet.sheet_row(lead(23, "=1+1 Consulting LLC", "calc.io", "c@calc.io", "2142100123", "2026-10-08")))
        out = self.bridge.upsert([good, bad_date, no_name, dup, formula], "2026-10-08")
        self.assertEqual(out["inserted"], 3)
        self.assertEqual(out["skipped"], 2)
        _, row = self.row_of("Hotel LLC")
        self.assertEqual(row[5]["v"], "")                                       # the bad date became a blank cell
        r, row = self.row_of("=1+1 Consulting LLC")
        self.assertEqual(row[0]["fmt"], "@")                                    # stored as text, never as a formula

    def test_5b_text_in_the_run_log_cannot_become_a_formula(self):
        self.bridge.log({"started": "=1+1", "seconds": "7", "added": "3", "held": 1, "updated": 0, "sources": "+SUM(1)",
                         "status": "ok", "errors": '@cmd|"x"'}, 4, 5, "tomorrow 07:00")
        rows = [r for r in self.sheet_named("_runs")["rows"] if r and r[0]["v"]]
        last = [c["v"] for c in rows[-1]]
        self.assertEqual(last[0], "'=1+1")
        self.assertEqual(last[5], "'+SUM(1)")
        self.assertEqual(last[7], "'@cmd|\"x\"")
        self.assertEqual(last[1:5], [7, 3, 1, 0])                                # numbers stay numbers

    def test_5c_an_account_that_forbids_link_sharing_does_not_break_the_connection(self):
        requests.get(self.base + "/debug/block_link?on=1")
        try:
            out = self.bridge.init("Asia/Karachi", [], link=True)
            self.assertTrue(out["ok"])
            self.assertFalse(out["shared"])
            self.assertIn("Access denied", out["share_error"])
            again = self.bridge.share(True)
            self.assertEqual((again["shared"], bool(again["share_error"])), (False, True))
            self.assertEqual(self.bridge.share(False)["share_error"], "")             # making it private is always allowed
        finally:
            requests.get(self.base + "/debug/block_link?on=0")
        ok = self.bridge.share(True)
        self.assertEqual((ok["shared"], ok["share_error"]), (True, ""))

    def test_6_every_response_carries_the_script_version(self):
        for action in ("ping",):
            self.assertEqual(self.bridge.call(action)["version"], sheet.EXPECTED_VERSION)

    def test_7_companies_waiting_for_google_are_sent_once_it_is_connected(self):
        import tempfile
        from datetime import date
        from itleads import config, pipeline
        from itleads.store import Store
        saved = (config.ROOT, config.DATA, config.LOGS, config.CONFIG_PATH)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            config.ROOT, config.DATA, config.LOGS, config.CONFIG_PATH = root, root / "data", root / "logs", root / "config.json"
            config.ensure_dirs()
            try:
                waiting = lead(31, "Hotel Two LLC", "hotel2.io", "h@hotel2.io", "2142100131", "2026-10-09")
                store = Store(config.DATA / "leads.db")
                store.add(waiting["raw"], date(2026, 10, 9))
                store.record_attempt(waiting["raw"]["id"], waiting["enrich"], "ready", date(2026, 10, 9))
                store.close()
                cfg = dict(config.load(), apps_script_url=self.base + "/macros/s/FAKE/exec", token=TOKEN)
                lines = []
                summary = pipeline.send_waiting(cfg, log=lines.append, trigger="connect")
                self.assertEqual((summary["status"], summary["pushed"]), ("ok", 1))
                names = [row[0]["v"] for row in self.sheet_named("Companies")["rows"][1:] if row[0]["v"]]
                self.assertIn("Hotel Two LLC", names)
                logged = [[c["v"] for c in r[:8]] for r in self.sheet_named("_runs")["rows"] if r and r[0]["v"]][-1]
                self.assertEqual(logged[2], 1)                                                  # the sheet's log says 1 new
                store = Store(config.DATA / "leads.db")
                self.assertEqual(store.counts()["pushed"], 1)
                self.assertEqual(store.last_run()["summary"]["trigger"], "connect")
                store.close()
                again = pipeline.send_waiting(cfg, log=lines.append, trigger="connect")        # nothing left to send
                self.assertEqual(again["pushed"], 0)
                store = Store(config.DATA / "leads.db")
                self.assertEqual(len(store.recent_runs(5)), 1)                                 # and no empty entry for it
                store.close()
            finally:
                config.ROOT, config.DATA, config.LOGS, config.CONFIG_PATH = saved

    def test_8_an_answer_without_the_result_is_asked_for_again_and_never_counted(self):
        class Reply:
            def __init__(self, body):
                self.text, self.status_code = json.dumps(body), 200

            def json(self):
                return json.loads(self.text)

        good = {"ok": True, "inserted": 3, "updated": 0, "skipped": 0, "total": 3, "version": 5}
        odd = {"ok": True, "service": "it-leads", "version": 5}                        # what Google sent once instead of the result
        answers, bridge = [Reply(odd), Reply(good)], sheet.Bridge("https://script.google.com/macros/s/X/exec", "t", retries=3)
        with mock.patch.object(sheet.requests, "post", side_effect=lambda *a, **k: answers.pop(0)) as post, \
                mock.patch.object(sheet.time, "sleep"):
            res = bridge.upsert([], "2026-10-06")
        self.assertEqual((res["inserted"], post.call_count), (3, 2))
        with mock.patch.object(sheet.requests, "post", return_value=Reply(odd)), mock.patch.object(sheet.time, "sleep"):
            with self.assertRaises(sheet.BridgeError) as cm:                           # a clear error, not a KeyError
                bridge.upsert([], "2026-10-06")
        self.assertIn("not with the result", str(cm.exception))

    def test_9_the_dashboard_formulas_cannot_drift_and_there_is_no_held_back_tile(self):
        """New rows go in at the top of Companies; a range such as $O$2:$O would be pushed down by every insert until the
        tiles read 0 (seen on the owner's sheet). Every reference to the list must be a whole column."""
        self.bridge.init("Asia/Karachi", [])
        self.bridge.upsert([sheet.sheet_row(lead(40, "India LLC", "india.io", "i@india.io", "2142100140", "2026-10-11"))], "2026-10-11")
        self.bridge.log({"started": "11 Oct 2026, 17:00", "seconds": 5, "added": 1, "held": 9, "updated": 0, "sources": "TX 1",
                         "status": "ok", "errors": ""}, 9, 5, "tomorrow 17:00")
        for refresh in (False, True):
            if refresh:
                self.assertTrue(self.bridge.dashboard(5, "tomorrow 17:00")["ok"])          # the repair action rebuilds the same thing
            dash = self.sheet_named("Dashboard")
            cells = [c["v"] for row in dash["rows"] for c in row if isinstance(c.get("v"), str)]
            formulas = [v for v in cells if v.startswith("=")]
            self.assertGreaterEqual(len(formulas), 20)
            for f in formulas:
                self.assertNotRegex(f, r"Companies!\$?[A-Z]+\$?\d", f)                      # whole columns only
            text = " ".join(cells).upper()
            self.assertNotIn("HELD", text)
            for label in ("LATEST ADDITIONS", "LAST 7 DAYS", "TOTAL LISTED", "EMAIL AND PHONE", "BY REGISTRY"):
                self.assertIn(label, text)
            self.assertIn("registries", dash["rows"][2][1]["v"])
        recent = [c["v"] for c in dash["rows"][24] if c.get("v") not in ("", None)]
        self.assertIn("1 added", recent)

    def test_2_wrong_token(self):
        bad = sheet.Bridge(self.base + "/macros/s/FAKE/exec", "nope", timeout=30)
        with self.assertRaises(sheet.BridgeError) as cm:
            bad.ping()
        self.assertIn("Open Settings", str(cm.exception))

    def test_3_html_answer_is_explained(self):
        bad = sheet.Bridge(self.base + "/does/not/exist", TOKEN, timeout=30)
        with mock.patch("time.sleep"), self.assertRaises(sheet.BridgeError) as cm:
            bad.ping()
        self.assertIn("Anyone", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
