"""Where things live: the user config, the library of books, a book's workspace.

Config follows the XDG convention (``~/.config/ai-epub-translator/config.toml``;
``%APPDATA%`` on Windows). The *library* — the directory holding one folder per
book — is, in order of precedence: the ``--books`` flag, ``$AI_EPUB_TRANSLATOR_BOOKS``,
``[paths] library`` in the config, else the XDG data dir. It is never guessed from
the current directory: the same command must mean the same library from anywhere.
Inside a book folder everything
the user edits or wants is visible (``book.toml``, ``glossary.toml``, the finished
EPUB); the machinery — the unpacked original and translation, logs, cache, progress
— sits in ``.work/``.
"""

from __future__ import annotations

import os
import sys

APP = "ai-epub-translator"
ENV_CONFIG = "AI_EPUB_TRANSLATOR_CONFIG"
ENV_BOOKS = "AI_EPUB_TRANSLATOR_BOOKS"
WORK = ".work"


def _home_subdir(env_var: str, posix_default: str, win_var: str) -> str:
    if sys.platform == "win32":
        base = os.environ.get(win_var) or os.path.expanduser("~")
    else:
        base = os.environ.get(env_var) or os.path.expanduser(posix_default)
    return os.path.join(base, APP)


def config_dir() -> str:
    return _home_subdir("XDG_CONFIG_HOME", "~/.config", "APPDATA")


def data_dir() -> str:
    return _home_subdir("XDG_DATA_HOME", "~/.local/share", "LOCALAPPDATA")


def user_config_path() -> str:
    return os.environ.get(ENV_CONFIG) or os.path.join(config_dir(), "config.toml")


def library_dir(flag: str = None, configured: str = None) -> str:
    """The directory holding the books, by the precedence in the module docstring."""
    for candidate in (flag, os.environ.get(ENV_BOOKS), configured):
        if candidate:
            return os.path.abspath(os.path.expanduser(candidate))
    return os.path.join(data_dir(), "books")


# --- a book's workspace ---------------------------------------------------------

def work(book_dir: str) -> str:
    return os.path.join(book_dir, WORK)


def original(book_dir: str) -> str:
    """The pristine unpacked EPUB — the baseline of every check."""
    return os.path.join(book_dir, WORK, "original")


def target(book_dir: str) -> str:
    """The unpacked EPUB being translated in place."""
    return os.path.join(book_dir, WORK, "target")


def logs(book_dir: str) -> str:
    return os.path.join(book_dir, WORK, "logs")


def cache_path(book_dir: str) -> str:
    return os.path.join(book_dir, WORK, "cache.sqlite")


def state_path(book_dir: str) -> str:
    return os.path.join(book_dir, WORK, "state.json")
