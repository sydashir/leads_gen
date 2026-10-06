"""Accounts, login throttling and a few app facts, in their own small SQLite file."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime

from .. import config
from ..dbutil import enable_wal

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  email      TEXT NOT NULL UNIQUE,
  name       TEXT NOT NULL DEFAULT '',
  pw_hash    TEXT NOT NULL,
  is_admin   INTEGER NOT NULL DEFAULT 0,
  created    TEXT NOT NULL,
  last_login TEXT,
  session_v  INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS attempts (k TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_attempts ON attempts(k, ts);
CREATE INDEX IF NOT EXISTS idx_attempts_ts ON attempts(ts);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


@contextmanager
def connect():
    config.ensure_dirs()
    path = config.DATA / "app.db"
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    enable_wal(db)
    db.executescript(SCHEMA)
    try:
        os.chmod(path, 0o600)                           # accounts live here: owner only
    except OSError:
        pass
    try:
        yield db
        db.commit()
    finally:
        db.close()


def _user(r):
    return None if r is None else {k: r[k] for k in r.keys()}


def user_by_email(email: str):
    with connect() as db:
        return _user(db.execute("SELECT * FROM users WHERE email=?", (email.lower(),)).fetchone())


def user_by_id(uid: int):
    with connect() as db:
        return _user(db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())


def count_users() -> int:
    with connect() as db:
        return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def list_users() -> list:
    with connect() as db:
        return [_user(r) for r in db.execute("SELECT id, email, name, is_admin, created, last_login FROM users ORDER BY id")]


def create_user(email: str, name: str, pw_hash: str, admin: bool | None = None) -> dict:
    """The first account becomes the admin unless told otherwise. Raises sqlite3.IntegrityError if the email is taken."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        first = db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        is_admin = first if admin is None else admin
        cur = db.execute("INSERT INTO users(email, name, pw_hash, is_admin, created) VALUES (?,?,?,?,?)",
                         (email.lower(), name, pw_hash, 1 if is_admin else 0, datetime.now().isoformat(timespec="seconds")))
        return _user(db.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone())


def make_admin(uid: int) -> None:
    with connect() as db:
        db.execute("UPDATE users SET is_admin=1 WHERE id=?", (uid,))


def touch_login(uid: int) -> None:
    with connect() as db:
        db.execute("UPDATE users SET last_login=? WHERE id=?", (datetime.now().isoformat(timespec="seconds"), uid))


def set_password(uid: int, pw_hash: str) -> None:
    with connect() as db:
        db.execute("UPDATE users SET pw_hash=?, session_v=session_v+1 WHERE id=?", (pw_hash, uid))


def delete_user(uid: int) -> None:
    with connect() as db:
        db.execute("DELETE FROM users WHERE id=?", (uid,))


def bump_session(uid: int) -> None:
    """Every cookie issued so far for this account stops working (everyone on the account is signed out). Log out does
    not use this: it only clears the browser's own cookie. Changing a password does it (set_password)."""
    with connect() as db:
        db.execute("UPDATE users SET session_v=session_v+1 WHERE id=?", (uid,))


# ---- throttle: failed sign-ins in a rolling window
def record_failure(key: str) -> None:
    with connect() as db:
        db.execute("INSERT INTO attempts(k, ts) VALUES (?,?)", (key, time.time()))
        db.execute("DELETE FROM attempts WHERE ts < ?", (time.time() - 86400,))


def failures(key: str, window: int = 900) -> int:
    with connect() as db:
        return db.execute("SELECT COUNT(*) FROM attempts WHERE k=? AND ts>?", (key, time.time() - window)).fetchone()[0]


def hit(key: str, limit: int, window: int = 900) -> bool:
    """Count one attempt and say whether it is allowed, as a single step (parallel requests cannot slip past)."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        n = db.execute("SELECT COUNT(*) FROM attempts WHERE k=? AND ts>?", (key, time.time() - window)).fetchone()[0]
        if n >= limit:
            return False
        db.execute("INSERT INTO attempts(k, ts) VALUES (?,?)", (key, time.time()))
        db.execute("DELETE FROM attempts WHERE ts < ?", (time.time() - 86400,))
        return True


def email_key(email: str) -> str:
    """A short fixed-size stand-in for an email in the throttle table (the raw text can be as long as a request)."""
    return hashlib.sha256((email or "").strip().lower()[:254].encode("utf-8", "ignore")).hexdigest()[:16]


def reserve(limits: list, window: int = 900):
    """Count one attempt against every (key, limit) in a single step. Returns the row ids to give back if the attempt
    turns out to be fine, or None if any limit is already reached (parallel requests cannot slip past)."""
    now = time.time()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for key, limit in limits:
            if db.execute("SELECT COUNT(*) FROM attempts WHERE k=? AND ts>?", (key, now - window)).fetchone()[0] >= limit:
                return None
        ids = [db.execute("INSERT INTO attempts(k, ts) VALUES (?,?)", (key, now)).lastrowid for key, _ in limits]
        db.execute("DELETE FROM attempts WHERE ts < ?", (now - 86400,))
        return ids


def release(row_ids: list) -> None:
    with connect() as db:
        db.executemany("DELETE FROM attempts WHERE rowid=?", [(i,) for i in row_ids])


def rehash_password(uid: int, pw_hash: str) -> None:
    """Store the same password under the current hash method. Other sessions stay signed in."""
    with connect() as db:
        db.execute("UPDATE users SET pw_hash=? WHERE id=?", (pw_hash, uid))


def clear_lockout(email: str) -> None:
    """Forget the failed sign-ins that count against this account (used when its password is reset)."""
    h = email_key(email)
    with connect() as db:
        db.execute("DELETE FROM attempts WHERE k=? OR k LIKE ?", (f"acct:{h}", f"u:%|{h}"))


def clear_all_lockouts() -> None:
    """Forget every failed sign-in (this tool has one shared login, so anybody locked out is locked out of it)."""
    with connect() as db:
        db.execute("DELETE FROM attempts")


def clear_failures(key: str) -> None:
    with connect() as db:
        db.execute("DELETE FROM attempts WHERE k=?", (key,))


# ---- small facts
def meta_get(k: str, default: str = "") -> str:
    with connect() as db:
        r = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default


def meta_set(k: str, v: str) -> None:
    with connect() as db:
        db.execute("INSERT INTO meta(k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
