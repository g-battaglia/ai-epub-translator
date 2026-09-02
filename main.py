#!/usr/bin/env python3
"""Run the CLI from a checkout: ``python3 main.py <command>`` (or ``uv run main.py``).

The installed package exposes the same entry point as the ``ai-epub-translator``
command; this shim only puts the checkout on ``sys.path``.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ai_epub_translator.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
