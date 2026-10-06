"""Local memory: every company seen, what we learned about it, and what already reached the sheet."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from .dbutil import add_column, enable_wal

# After attempt k the next look happens this many days after first sight (then we give up).
RETRY_DAYS = [0, 3, 10, 30]

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
  id         TEXT PRIMARY KEY,
  source     TEXT NOT NULL,
  raw        TEXT NOT NULL,
  enrich     TEXT,
  state      TEXT NOT NULL DEFAULT 'new',   -- new | held | ready | pushed | dropped
  first_seen TEXT NOT NULL,
  attempts   INTEGER NOT NULL DEFAULT 0,
  next_due   TEXT,
  domain     TEXT,
  pushed_hash TEXT,
  pushed_at  TEXT,
  ready_at   TEXT,
  strikes    INTEGER NOT NULL DEFAULT 0   -- lookups that ended in network trouble, not in an answer
);
CREATE INDEX IF NOT EXISTS idx_state ON companies(state, next_due);
CREATE INDEX IF NOT EXISTS idx_domain ON companies(domain);
CREATE TABLE IF NOT EXISTS source_state (
  key TEXT PRIMARY KEY,
  last_ok TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started TEXT, finished TEXT, summary TEXT
);
"""


def row_hash(row: dict) -> str:
    return hashlib.sha1(json.dumps(row, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=30000")
        enable_wal(self.db)                                    # the web server reads while a run writes
        self.db.executescript(SCHEMA)
        add_column(self.db, "companies", "ready_at", "TEXT")           # databases made by earlier versions
        add_column(self.db, "companies", "strikes", "INTEGER NOT NULL DEFAULT 0")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def close(self) -> None:
        self.db.close()

    # -- per-source progress: a source that failed keeps its old date, so its next window reaches back to it
    def source_last_ok(self, key: str):
        r = self.db.execute("SELECT last_ok FROM source_state WHERE key=?", (key,)).fetchone()
        return date.fromisoformat(r["last_ok"]) if r and r["last_ok"] else None

    def set_source_ok(self, key: str, day: date) -> None:
        self.db.execute("INSERT INTO source_state(key, last_ok) VALUES (?,?) "
                        "ON CONFLICT(key) DO UPDATE SET last_ok=excluded.last_ok", (key, day.isoformat()))
        self.db.commit()

    # -- intake
    def is_empty(self) -> bool:
        return self.db.execute("SELECT 1 FROM companies LIMIT 1").fetchone() is None

    def add(self, rec: dict, today: date) -> bool:
        """Insert a freshly fetched record. False if we already know it."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO companies(id, source, raw, state, first_seen, next_due) VALUES (?,?,?,?,?,?)",
            (rec["id"], rec["source"], json.dumps(rec, ensure_ascii=True), "new", today.isoformat(), today.isoformat()))
        self.db.commit()
        return cur.rowcount == 1

    # -- enrichment queue
    def due(self, today: date, limit: int = 100000) -> list:
        rows = self.db.execute(
            "SELECT * FROM companies WHERE state IN ('new','held','ready') AND next_due <= ? ORDER BY first_seen DESC LIMIT ?",
            (today.isoformat(), limit)).fetchall()
        return [self._row(r) for r in rows]

    def record_attempt(self, cid: str, enrich: dict, state: str, today: date) -> None:
        r = self.db.execute("SELECT first_seen, attempts FROM companies WHERE id=?", (cid,)).fetchone()
        attempts = r["attempts"] + 1
        first = date.fromisoformat(r["first_seen"])
        next_due = None
        if state in ("new", "held"):
            if attempts >= len(RETRY_DAYS):
                state = "dropped"
            else:
                next_due = max(first + timedelta(days=RETRY_DAYS[attempts]), today + timedelta(days=1)).isoformat()
        self.db.execute(
            "UPDATE companies SET enrich=?, state=?, attempts=?, next_due=?, domain=?, strikes=0, "
            "ready_at=CASE WHEN ?='ready' THEN COALESCE(ready_at, ?) ELSE ready_at END WHERE id=?",
            (json.dumps(enrich, ensure_ascii=True), state, attempts, next_due, enrich.get("domain") or None,
             state, today.isoformat(), cid))
        self.db.commit()

    def recheck_listed(self, today: date) -> int:
        """Look at every company already on the list again on the next run (their entry stays until the new look says
        otherwise). Used after the lookup rules improved."""
        cur = self.db.execute("UPDATE companies SET next_due=? WHERE state='ready'", (today.isoformat(),))
        self.db.commit()
        return cur.rowcount

    def add_strike(self, cid: str) -> int:
        """A lookup ended in network trouble. Returns how many in a row, so a company that always does is not retried forever."""
        self.db.execute("UPDATE companies SET strikes=strikes+1 WHERE id=?", (cid,))
        self.db.commit()
        return self.db.execute("SELECT strikes FROM companies WHERE id=?", (cid,)).fetchone()["strikes"]

    def checked_not_listed(self) -> list:
        """Companies that were looked up but did not qualify (held back, or dropped): re-judged when the rules change."""
        rows = self.db.execute("SELECT * FROM companies WHERE state IN ('held','dropped') AND enrich IS NOT NULL").fetchall()
        return [self._row(r) for r in rows]

    def demote(self, cid: str, state: str, enrich: dict, today: date) -> bool:
        """A company on the list (waiting to be sent, or already sent) no longer meets the rules: it goes back to held or
        dropped. One that was waiting is looked at again tomorrow; one that was already sent, today (the next run looks it up
        again with today's code). False when the company is not on the list (nothing changes)."""
        r = self.db.execute("SELECT state FROM companies WHERE id=?", (cid,)).fetchone()
        if r is None or r["state"] not in ("ready", "pushed"):
            return False
        nxt = None
        if state == "held":
            nxt = (today if r["state"] == "pushed" else today + timedelta(days=1)).isoformat()
        self.db.execute("UPDATE companies SET state=?, enrich=?, next_due=? WHERE id=? AND state IN ('ready','pushed')",
                        (state, json.dumps(enrich, ensure_ascii=True), nxt, cid))
        self.db.commit()
        return True

    def mark_ready(self, cid: str, enrich: dict, today: date) -> None:
        self.db.execute("UPDATE companies SET state='ready', enrich=?, next_due=NULL, ready_at=? WHERE id=?",
                        (json.dumps(enrich, ensure_ascii=True), today.isoformat(), cid))
        self.db.commit()

    def save_enrich(self, cid: str, enrich: dict) -> None:
        """Replace the stored lookup result of a company that is on the list, waiting to be sent or already sent (its dates
        and state stay)."""
        self.db.execute("UPDATE companies SET enrich=? WHERE id=? AND state IN ('ready','pushed')",
                        (json.dumps(enrich, ensure_ascii=True), cid))
        self.db.commit()

    # -- sheet sync
    def ready(self) -> list:
        rows = self.db.execute("SELECT * FROM companies WHERE state='ready'").fetchall()
        return [self._row(r) for r in rows]

    def pushed(self) -> list:
        """The companies already sent to the sheet."""
        rows = self.db.execute("SELECT * FROM companies WHERE state='pushed' AND enrich IS NOT NULL").fetchall()
        return [self._row(r) for r in rows]

    def mark_pushed(self, cid: str, h: str, today: date) -> None:
        self.db.execute("UPDATE companies SET state='pushed', pushed_hash=?, pushed_at=? WHERE id=?",
                        (h, today.isoformat(), cid))
        self.db.commit()

    def reset_pushed(self) -> int:
        """A different (empty) sheet was connected: everything that was sent to the old one is waiting again."""
        cur = self.db.execute("UPDATE companies SET state='ready', pushed_hash=NULL, pushed_at=NULL WHERE state='pushed'")
        self.db.commit()
        return cur.rowcount

    def set_state(self, cid: str, state: str) -> None:
        self.db.execute("UPDATE companies SET state=? WHERE id=?", (state, cid))
        self.db.commit()

    def domain_owner(self, domain: str, exclude: str):
        r = self.db.execute(
            "SELECT id FROM companies WHERE domain=? AND id<>? AND state IN ('ready','pushed') LIMIT 1",
            (domain, exclude)).fetchone()
        return r["id"] if r else None

    # -- reporting
    def counts(self) -> dict:
        out = {"new": 0, "held": 0, "ready": 0, "pushed": 0, "dropped": 0}
        for r in self.db.execute("SELECT state, COUNT(*) n FROM companies GROUP BY state"):
            out[r["state"]] = r["n"]
        return out

    def held_breakdown(self) -> dict:
        """Companies not in the sheet yet, by the reason."""
        out = {"not_looked_up": 0, "no_website": 0, "missing_contact": 0}
        for r in self.db.execute("SELECT state, enrich FROM companies WHERE state IN ('new','held')"):
            e = json.loads(r["enrich"]) if r["enrich"] else None
            if r["state"] == "new" and not e:
                out["not_looked_up"] += 1
            elif e and e.get("website"):
                out["missing_contact"] += 1
            else:
                out["no_website"] += 1
        return out

    def log_run(self, started: str, finished: str, summary: dict) -> None:
        self.db.execute("INSERT INTO runs(started, finished, summary) VALUES (?,?,?)",
                        (started, finished, json.dumps(summary)))
        self.db.commit()

    def last_run(self):
        r = self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return None if r is None else {"started": r["started"], "finished": r["finished"], "summary": json.loads(r["summary"])}

    def export_items(self) -> list:
        """Every company that qualified, newest filing first: what the sheet and the downloads contain."""
        rows = self.db.execute("SELECT * FROM companies WHERE state IN ('pushed','ready') AND enrich IS NOT NULL").fetchall()
        items = [self._row(r) for r in rows]
        items.sort(key=lambda it: (it["raw"].get("registered") or "", it["id"]), reverse=True)
        return items

    def export_version(self) -> tuple:
        """Changes whenever the list the downloads contain changes."""
        r = self.db.execute("SELECT COUNT(*), COALESCE(MAX(COALESCE(pushed_at, ready_at, first_seen)), ''), "
                            "COALESCE(SUM(LENGTH(enrich)), 0) FROM companies WHERE state IN ('pushed','ready')").fetchone()
        return (r[0], r[1], r[2])

    def recent_runs(self, n: int = 6) -> list:
        out = []
        for r in self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (n,)):
            out.append({"started": r["started"], "finished": r["finished"], "summary": json.loads(r["summary"])})
        return out

    def dashboard(self, today: date) -> dict:
        """Everything the dashboard shows, in one pass."""
        by_day = {}
        by_state: dict = {}
        by_source: dict = {}
        total = 0
        for r in self.db.execute("SELECT raw, ready_at, pushed_at, first_seen FROM companies "
                                 "WHERE state IN ('pushed','ready')"):
            total += 1
            raw = json.loads(r["raw"])
            day = r["ready_at"] or r["pushed_at"] or r["first_seen"]
            by_day[day] = by_day.get(day, 0) + 1
            by_state[raw.get("state") or "?"] = by_state.get(raw.get("state") or "?", 0) + 1
            by_source[raw.get("source")] = by_source.get(raw.get("source"), 0) + 1
        days = [(today - timedelta(days=11 - i)).isoformat() for i in range(12)]
        last7 = sum(by_day.get((today - timedelta(days=i)).isoformat(), 0) for i in range(7))
        latest_day = max(by_day) if by_day else ""
        counts = self.counts()
        return {"total": total, "pushed": counts["pushed"], "held": counts["held"], "last7": last7,
                "latest": by_day.get(latest_day, 0), "latest_day": latest_day,
                "by_day": [{"d": d, "n": by_day.get(d, 0)} for d in days],
                "by_state": sorted(by_state.items(), key=lambda kv: -kv[1])[:8],
                "by_source": sorted(by_source.items(), key=lambda kv: -kv[1])[:8],
                "waiting": self.held_breakdown()}

    def all_records(self) -> list:
        return [self._row(r) for r in self.db.execute("SELECT * FROM companies ORDER BY first_seen DESC")]

    @staticmethod
    def _row(r) -> dict:
        return {"id": r["id"], "source": r["source"], "raw": json.loads(r["raw"]),
                "enrich": json.loads(r["enrich"]) if r["enrich"] else None, "state": r["state"],
                "first_seen": r["first_seen"], "attempts": r["attempts"], "domain": r["domain"],
                "pushed_hash": r["pushed_hash"]}
