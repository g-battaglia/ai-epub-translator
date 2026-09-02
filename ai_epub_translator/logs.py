"""Structured per-book logging: human-readable (translate.log) + JSONL (files.jsonl).

Each translation attempt or repair step appends one JSONL record and one
human-readable line. ``status`` aggregates the JSONL history for reporting.
"""

from __future__ import annotations

import datetime
import json
import os

from . import paths


def logs_dir(book_dir: str) -> str:
    """Return (creating) the logs directory of a book workspace."""
    d = paths.logs(book_dir)
    os.makedirs(d, exist_ok=True)
    return d


def _ts() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def text(book_dir: str, slug: str, msg: str) -> None:
    """Append one readable line to ``logs/translate.log``."""
    with open(os.path.join(logs_dir(book_dir), "translate.log"), "a",
              encoding="utf-8") as f:
        f.write(f"[{_ts()}] {slug} {msg}\n")


def jsonl(book_dir: str, record: dict) -> None:
    """Append one JSON record to ``logs/files.jsonl`` (one per attempt/fix)."""
    record = {"ts": _ts(), **record}
    with open(os.path.join(logs_dir(book_dir), "files.jsonl"), "a",
              encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl(book_dir: str) -> list:
    """Read all JSONL records (empty list if the file does not exist yet)."""
    p = os.path.join(logs_dir(book_dir), "files.jsonl")
    if not os.path.isfile(p):
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for ln in f:
            if not ln.strip():
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue        # a half-written last line must not break status
    return out
