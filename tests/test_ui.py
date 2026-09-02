"""Unit tests for the progress display (ai_epub_translator/ui.py).

Fake streams expose ``isatty() -> True`` so the ANSI bar path is exercised
without a real terminal.
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_epub_translator.ui import _BEAT_S, Progress


class FakeStream:
    """Minimal writable stream that reports itself as a TTY."""

    def __init__(self):
        self.buf = ""

    def write(self, s: str) -> None:
        self.buf += s

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True


class FakeLog(FakeStream):
    """A writable stream that is NOT a TTY (a log file or a pipe)."""

    def isatty(self) -> bool:
        return False


class TestProgressModes(unittest.TestCase):

    def test_stream_writes_tokens(self):
        out = FakeStream()
        p = Progress("stream", total=100, out=out, bar=FakeStream())
        p.step("hello "); p.step("world"); p.finish()
        self.assertIn("hello world", out.buf)

    def test_percent_suppresses_tokens_and_draws_bar(self):
        out = FakeStream()
        p = Progress("percent", total=100, out=out, bar=FakeStream())
        p.step("hello world")            # would be printed in stream mode
        self.assertNotIn("hello world", out.buf)
        self.assertIn("%", out.buf)      # a bar was drawn

    def test_both_writes_token_and_bar(self):
        out, bar = FakeStream(), FakeStream()
        p = Progress("both", total=100, out=out, bar=bar)
        p.step("tok")
        self.assertIn("tok", out.buf)
        self.assertIn("%", bar.buf)

    def test_finish_shows_100(self):
        out = FakeStream()
        p = Progress("percent", total=100, out=out, bar=FakeStream())
        p.step("x" * 20)                 # 20%
        p.finish()
        self.assertIn("100%", out.buf)

    def test_pct_tracked_from_original_size(self):
        out = FakeStream()
        p = Progress("percent", total=200, out=out, bar=FakeStream())
        for _ in range(10):
            p.step("abcdefghij")         # 10 * 10 chars = 100 -> 50%
        self.assertEqual(p.pct, 50)

    def test_pct_capped_at_100(self):
        p = Progress("percent", total=10, out=FakeStream(), bar=FakeStream())
        p.step("x" * 500)                # far over total
        self.assertEqual(p.pct, 100)

    def test_unknown_mode_defaults_to_stream(self):
        p = Progress("nonsense", total=10)
        self.assertEqual(p.mode, "stream")

    def test_mode_constants_are_class_attributes(self):
        # Regression: Progress.STREAM/PERCENT/BOTH must exist as class attributes
        # (llm.chat and repair._default_chat build Progress(Progress.STREAM, …)).
        self.assertEqual(Progress.STREAM, "stream")
        self.assertEqual(Progress.PERCENT, "percent")
        self.assertEqual(Progress.BOTH, "both")
        for mode in (Progress.STREAM, Progress.PERCENT, Progress.BOTH):
            self.assertEqual(Progress(mode, total=10).mode, mode)


class TestNonTtyHeartbeat(unittest.TestCase):
    """On a log/pipe the in-place bar is invisible, so percent mode must emit a
    newline-terminated heartbeat line — otherwise a backgrounded run looks hung
    for the whole of a chapter (the bug that made a healthy run seem stuck).
    """

    def test_percent_emits_heartbeat_line_not_bar(self):
        log = FakeLog()
        p = Progress("percent", total=100, out=log, bar=FakeLog())
        p.step("x" * 30)                      # 30%
        self.assertIn("30%", log.buf)
        self.assertIn("\n", log.buf)          # a full line, not an in-place redraw
        self.assertNotIn("\r", log.buf)       # no carriage-return bar on a log

    def test_heartbeat_is_throttled(self):
        log = FakeLog()
        p = Progress("percent", total=100, out=log, bar=FakeLog())
        p.step("x" * 10)                      # first beat
        lines = log.buf.count("\n")
        p._last_draw = 0.0                    # clear the fast (0.15s) redraw throttle
        p.step("x" * 10)                      # still within _BEAT_S -> suppressed
        self.assertEqual(log.buf.count("\n"), lines)

    def test_heartbeat_resumes_after_interval(self):
        log = FakeLog()
        p = Progress("percent", total=100, out=log, bar=FakeLog())
        p.step("x" * 10)
        p._last_draw = 0.0
        p._last_beat = time.time() - (_BEAT_S + 1)   # a full interval has passed
        p.step("x" * 10)
        self.assertGreaterEqual(log.buf.count("\n"), 2)

    def test_finish_forces_final_line_even_when_throttled(self):
        log = FakeLog()
        p = Progress("percent", total=100, out=log, bar=FakeLog())
        p.step("x" * 10)
        p._last_beat = time.time()            # throttle active
        p.finish()
        self.assertIn("100%", log.buf)

    def test_stream_mode_still_writes_tokens_on_log(self):
        # STREAM mode already gives liveness on a log (tokens are printed); the
        # heartbeat only concerns the bar modes.
        log = FakeLog()
        p = Progress("stream", total=100, out=log, bar=FakeLog())
        p.step("ciao"); p.finish()
        self.assertIn("ciao", log.buf)


if __name__ == "__main__":
    unittest.main()
