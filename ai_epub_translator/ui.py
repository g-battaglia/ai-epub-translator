"""Terminal progress display for the streaming translation.

Three modes, selectable per run:

``stream``
    print every token as it arrives (the default).
``percent``
    show a live progress bar only (tokens suppressed).
``both``
    print tokens AND show the live progress bar.

The percentage is estimated from the streamed text length relative to the
original text length — a translation is roughly the same length as its source —
so it is intentionally approximate.

No external dependencies. On a TTY the bar is updated in place with a carriage
return and an ANSI clear-to-end-of-line. When the target is NOT a TTY (a log or a
pipe — how a backgrounded run is meant to be watched), an in-place bar would be
invisible and leave the log silent for the whole of a chapter's generation, so a
newline-terminated heartbeat line is emitted every ``_BEAT_S`` seconds instead:
``tail -f`` on the log then shows the run is alive even while one large chapter
streams.
"""

from __future__ import annotations

import os
import sys
import time

if sys.platform == "win32":                       # enable ANSI escapes (VT mode)
    os.system("")
# The output carries arrows, ticks and the books' own characters; a console that
# cannot encode them (cp1252 on Windows) must not crash the run over a glyph.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except (ValueError, OSError):                 # a closed or odd stream
            pass

_CLEAR = "\r\033[K"           # carriage return + clear to end of line
_BEAT_S = 15.0                # heartbeat cadence when writing to a log (non-TTY)


class Progress:
    """Drives token display during a streaming LLM call.

    Call :meth:`step` for each token chunk and :meth:`finish` once at the end.
    The mode constants are exposed both on the class (``Progress.STREAM``) and at
    module level (``STREAM``) for convenience.
    """

    STREAM = "stream"
    PERCENT = "percent"
    BOTH = "both"
    MODES = (STREAM, PERCENT, BOTH)

    def __init__(self, mode: str = STREAM, total: int = 0, *,
                 out=None, bar=None, width: int = 24):
        self.mode = mode if mode in Progress.MODES else Progress.STREAM
        self.total = max(1, int(total or 0))
        self.out = out if out is not None else sys.stdout
        self.bar = bar if bar is not None else sys.stderr
        self.width = width
        self.done = 0
        self.pct = 0
        self._last_draw = 0.0
        self._last_beat = 0.0

    def step(self, token: str) -> None:
        """Account for one streamed token chunk and refresh the display."""
        if not token:
            return
        self.done += len(token)
        self.pct = min(100, int(self.done * 100 / self.total))
        if self.mode == Progress.STREAM:
            self._emit(token)
        elif self.mode == Progress.PERCENT:
            self._redraw(self.out)
        else:                                       # BOTH
            self._emit(token)
            self._redraw(self.bar)

    def reset(self, total: int = None) -> None:
        """Zero the counters (used to retry a failed streaming call cleanly)."""
        self.done = 0
        self.pct = 0
        self._last_draw = 0.0
        self._last_beat = 0.0
        if total is not None:
            self.total = max(1, int(total))

    def tick(self, n: int = 1) -> None:
        """Advance by a unit count (for block-level progress, not token text)."""
        self.done += n
        self.pct = min(100, int(self.done * 100 / self.total))
        if self.mode == Progress.PERCENT:
            self._redraw(self.out)
        elif self.mode == Progress.BOTH:
            self._redraw(self.bar)

    def finish(self) -> None:
        """Finalize the display (100% bar / trailing newline)."""
        if self.mode == Progress.STREAM:
            self.out.write("\n")
            self.out.flush()
            return
        target = self.out if self.mode == Progress.PERCENT else self.bar
        if getattr(target, "isatty", lambda: False)():
            self._draw(target, 100)
            target.write("\n")
        else:
            self._draw(target, 100, force=True)     # final heartbeat (own newline)
        target.flush()
        if self.mode == Progress.BOTH:              # terminate the token stream
            self.out.write("\n")
            self.out.flush()

    # -- internals -----------------------------------------------------------

    def _emit(self, token: str) -> None:
        self.out.write(token)
        self.out.flush()

    def _redraw(self, stream) -> None:
        """Throttled in-place redraw (at most ~7 times per second)."""
        now = time.time()
        if now - self._last_draw < 0.15:
            return
        self._last_draw = now
        self._draw(stream, self.pct)

    def _draw(self, stream, pct: int, force: bool = False) -> None:
        if getattr(stream, "isatty", lambda: False)():
            filled = self.width * pct // 100
            bar = "█" * filled + "░" * (self.width - filled)
            stream.write(f"{_CLEAR}[{bar}] {pct:3d}%")
            stream.flush()
            return
        # Not a TTY (a log/pipe): an in-place bar is invisible, so a backgrounded
        # run would go silent for a whole chapter. Emit a throttled heartbeat line
        # instead, so tailing the log shows the run is alive. ``force`` bypasses the
        # throttle for the final 100% line.
        now = time.time()
        if not force and now - self._last_beat < _BEAT_S:
            return
        self._last_beat = now
        stream.write(f"  … {pct:3d}%\n")
        stream.flush()


# module-level aliases
STREAM = Progress.STREAM
PERCENT = Progress.PERCENT
BOTH = Progress.BOTH
MODES = Progress.MODES
