"""SQLite recovery cache: every translated unit is durable the instant it validates.

Translation is the expensive step. Each unit's outcome is written the moment it
is known, so a crash, a Ctrl-C or a server stall costs at most one batch: the
next run asks only for the units still missing. One row per file keeps the
reassembled text and the status the progress commands show.

Stored per book at ``<book>/.work/cache.sqlite`` via the stdlib ``sqlite3``
module. WAL journaling makes each commit crash-safe.

Lifecycle::

    put_unit()   one row per unit, status 'ok' | 'fail'   (the durability point)
    put_file()   the reassembled file, status 'translated' | 'fail'
    done()       rows removed                               (the text lives in target/)
"""

from __future__ import annotations

import datetime
import hashlib
import os
import sqlite3

from .paths import cache_path


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


class Cache:
    """The per-book store: a row per file, a row per unit."""

    def __init__(self, book_dir: str):
        self.path = cache_path(book_dir)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        # The whole-file cache of the markup-reproducing era is unusable now
        # (text with tags): a file that was pending simply goes back to "to
        # translate", which is what it was anyway.
        self.conn.execute("DROP TABLE IF EXISTS translations")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS files ("
            " rel TEXT PRIMARY KEY, orig_hash TEXT, status TEXT, text TEXT,"
            " prompt_tokens INTEGER, completion_tokens INTEGER, updated_at TEXT)")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS units ("
            " rel TEXT, idx INTEGER, orig_hash TEXT, text TEXT, status TEXT,"
            " attempts INTEGER DEFAULT 0, reason TEXT, updated_at TEXT,"
            " PRIMARY KEY (rel, idx))")
        self.conn.commit()

    def __enter__(self) -> Cache:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @staticmethod
    def hash_text(text: str) -> str:
        """SHA1 of the original text — a changed source invalidates the rows."""
        return hashlib.sha1(text.encode("utf-8")).hexdigest()

    # --- files --------------------------------------------------------------

    def get(self, rel: str):
        row = self.conn.execute("SELECT * FROM files WHERE rel=?", (rel,)).fetchone()
        return dict(row) if row else None

    def put_file(self, rel: str, orig_hash: str, text: str, status: str,
                 prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?)",
            (rel, orig_hash, status, text, prompt_tokens, completion_tokens, _now()))
        self.conn.commit()

    def set_status(self, rel: str, status: str) -> None:
        self.conn.execute("UPDATE files SET status=?, updated_at=? WHERE rel=?",
                          (status, _now(), rel))
        self.conn.commit()

    def done(self, rel: str) -> None:
        """Mark complete by pruning the rows (the final text lives in target/)."""
        self.drop(rel)

    def drop(self, rel: str) -> None:
        """Forget a file entirely, its units included."""
        self.conn.execute("DELETE FROM files WHERE rel=?", (rel,))
        self.conn.execute("DELETE FROM units WHERE rel=?", (rel,))
        self.conn.commit()

    def pending(self) -> list:
        """Rel paths of files that failed and have work in the cache."""
        cur = self.conn.execute("SELECT rel FROM files WHERE status='fail'")
        return [r["rel"] for r in cur.fetchall()]

    def files(self) -> list:
        """A compact summary of every cached file (for ``status``)."""
        cur = self.conn.execute(
            "SELECT rel, status, completion_tokens FROM files")
        return [dict(r) for r in cur.fetchall()]

    # --- units --------------------------------------------------------------

    def units(self, rel: str, orig_hash: str) -> dict:
        """``{idx: row}`` of the cached units of ``rel`` for this source text."""
        cur = self.conn.execute(
            "SELECT * FROM units WHERE rel=? AND orig_hash=?", (rel, orig_hash))
        return {r["idx"]: dict(r) for r in cur.fetchall()}

    def put_unit(self, rel: str, idx: int, orig_hash: str, text,
                 status: str, reason: str = "") -> None:
        """Persist one unit's outcome; ``attempts`` counts every call made."""
        self.conn.execute(
            "INSERT INTO units (rel, idx, orig_hash, text, status, attempts,"
            " reason, updated_at) VALUES (?,?,?,?,?,1,?,?) "
            "ON CONFLICT(rel, idx) DO UPDATE SET orig_hash=excluded.orig_hash,"
            " text=excluded.text, status=excluded.status,"
            " attempts=CASE WHEN units.orig_hash=excluded.orig_hash"
            "               THEN COALESCE(units.attempts, 0) + 1 ELSE 1 END,"
            " reason=excluded.reason, updated_at=excluded.updated_at",
            (rel, idx, orig_hash, text, status, reason, _now()))
        self.conn.commit()

    def unit_status(self, rel: str) -> tuple:
        """``(ok, failed)`` counts of the cached units of ``rel``."""
        ok = failed = 0
        for r in self.conn.execute(
                "SELECT status, COUNT(*) n FROM units WHERE rel=? GROUP BY status",
                (rel,)):
            if r["status"] == "ok":
                ok = r["n"]
            else:
                failed += r["n"]
        return ok, failed

    def failed_units(self, rel: str) -> list:
        """``[(idx, attempts, reason)]`` of the units of ``rel`` still failing."""
        cur = self.conn.execute(
            "SELECT idx, attempts, reason FROM units WHERE rel=? AND status!='ok'"
            " ORDER BY idx", (rel,))
        return [(r["idx"], r["attempts"] or 0, r["reason"] or "") for r in cur]

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:                             # noqa: BLE001 — best-effort
            pass
