"""Test isolation: never read the developer's real config or library."""

import os
import tempfile

os.environ.setdefault("AI_EPUB_TRANSLATOR_CONFIG", os.devnull)
os.environ.setdefault("AI_EPUB_TRANSLATOR_BOOKS", tempfile.mkdtemp(prefix="aiet-books-"))
