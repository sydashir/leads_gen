"""Small SQLite helpers shared by the lead database and the accounts database."""
from __future__ import annotations

import sqlite3
import time


def enable_wal(db: sqlite3.Connection) -> None:
    """Switch to write-ahead logging. Another connection opening the same file at that instant can make SQLite answer
    'database is locked' straight away (the busy timeout does not apply), so try again for a few seconds."""
    for _ in range(50):
        try:
            db.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e):
                raise
            time.sleep(0.2)
    db.execute("PRAGMA journal_mode=WAL")


def add_column(db: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Add a column to a database made by an older version; two programs doing it at once is not an error."""
    have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
    if column in have:
        return
    try:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        db.commit()
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e):
            raise
