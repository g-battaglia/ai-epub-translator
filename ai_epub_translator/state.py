"""Resume state for a book: file-level progress in ``.work/state.json``.

Holds the done/failed index. The translated *text* of in-progress or
failed files lives in the SQLite :mod:`cache` (``.cache.sqlite``), not here:
this file is the lightweight progress summary, that one is the recovery store.
"""

from __future__ import annotations

import json
import os

from .paths import state_path


def load(book_dir: str) -> dict:
    """Load the state file (or a fresh empty state).

    A partial/corrupt file must not brick the book: the SQLite cache still holds
    every translation, so a fresh index just re-verifies (it never re-translates —
    the cached units are reused). Hence the tolerant read.
    """
    p = state_path(book_dir)
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError, ValueError):
            pass
    return {"done": [], "failed": {}}


def save(book_dir: str, state: dict) -> None:
    """Persist the state atomically (deduped + sorted ``done`` list).

    Written to a temp file and ``os.replace``d in, so a crash mid-write can never
    leave a truncated index that the next run would fail to parse.
    """
    state["done"] = sorted(set(state.get("done", [])))
    p = state_path(book_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def mark_done(state: dict, rel: str) -> None:
    """Mark a file as done and clear any prior failure record."""
    if rel not in state["done"]:
        state["done"].append(rel)
    state.get("failed", {}).pop(rel, None)


def mark_failed(state: dict, rel: str, reason: str) -> None:
    state.setdefault("failed", {})[rel] = reason
