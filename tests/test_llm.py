"""Unit tests for the LLM helpers (ai_epub_translator/llm.py) that need no network.

Covers the accuracy-check prompt/report parsing and large-text sampling; the
actual LLM call is not exercised here.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_epub_translator import llm

CFG = {"source_lang": "inglese", "target_lang": "italiano"}


class TestCheckReportParsing(unittest.TestCase):

    def test_parses_score_and_comment(self):
        score, comment = llm.parse_check_report("9/10 | fedele, lieve deriva lessicale")
        self.assertEqual(score, 9)
        self.assertEqual(comment, "fedele, lieve deriva lessicale")

    def test_tolerates_spacing_and_separators(self):
        score, comment = llm.parse_check_report("7 / 10 — problemi di terminologia")
        self.assertEqual(score, 7)
        self.assertEqual(comment, "problemi di terminologia")

    def test_clamps_score(self):
        score, _ = llm.parse_check_report("12/10 | eccessivo")
        self.assertEqual(score, 10)

    def test_no_score_returns_none(self):
        score, comment = llm.parse_check_report("la traduzione è buona")
        self.assertIsNone(score)
        self.assertEqual(comment, "la traduzione è buona")

    def test_picks_first_score_line(self):
        report = "Considerazione: 6/10 | ok\n(ignora questa riga 3/10)"
        score, _ = llm.parse_check_report(report)
        self.assertEqual(score, 6)


class TestCheckPrompt(unittest.TestCase):

    def test_prompt_contains_both_texts_and_langs(self):
        prompt = llm.build_check_prompt("Hello world", "Ciao mondo", CFG)
        self.assertIn("Hello world", prompt)
        self.assertIn("Ciao mondo", prompt)
        self.assertIn("inglese", prompt)
        self.assertIn("italiano", prompt)


class TestGlossaryOrdering(unittest.TestCase):
    """Near-twin terms must sit side by side — measured, not cosmetic.

    With the entries sorted alphabetically, exotérisme and ésotérisme ended up
    separated by unrelated lines and the model collapsed both onto one word
    (0/5 correct). Adjacent, the same model got 5/5.
    """

    GLO = {"exotérisme": "essoterismo", "ésotérisme": "esoterismo",
           "intellection": "intellezione", "intellectualité": "intellettualità"}

    def _lines(self, cfg):
        return [l for l in llm.glossary_section(cfg).splitlines() if "->" in l]

    def test_twins_are_adjacent(self):
        lines = self._lines({"glossary": self.GLO})
        idx = {l.split("->")[0].strip(): i for i, l in enumerate(lines)}
        self.assertEqual(abs(idx["exotérisme"] - idx["ésotérisme"]), 1)

    def test_warning_follows_a_twin_pair(self):
        section = llm.glossary_section({"glossary": self.GLO})
        self.assertIn("DIFFERENT terms", section)

    def test_no_warning_without_twins(self):
        section = llm.glossary_section({"glossary": {"chien": "cane",
                                                     "maison": "casa"}})
        self.assertNotIn("DIFFERENT terms", section)

    def test_every_term_appears_once(self):
        lines = self._lines({"glossary": self.GLO})
        self.assertEqual(len(lines), len(self.GLO))
        for src in self.GLO:
            self.assertEqual(sum(1 for l in lines if l.strip().startswith(src + " ")), 1)


class TestGlossaryNotes(unittest.TestCase):
    """Free-form notes carry the *why*, and reach the prompt."""

    def test_notes_reach_the_prompt(self):
        cfg = {"source_lang": "french", "target_lang": "italian",
               "dest_code": "it", "register": "tu", "glossary": {"a": "b"},
               "glossary_notes": "watch out for X"}
        p = llm.build_units_prompt([], cfg)
        self.assertIn("Notes on this book's terminology", p)
        self.assertIn("watch out for X", p)

    def test_notes_alone_are_enough(self):
        section = llm.glossary_section({"glossary": {}, "glossary_notes": "just a note"})
        self.assertIn("just a note", section)

    def test_no_notes_no_section(self):
        section = llm.glossary_section({"glossary": {"a": "b"}, "glossary_notes": ""})
        self.assertNotIn("Notes on this book", section)


class TestGlossaryExtractionParser(unittest.TestCase):
    """The extraction parser is strict: the model must not smuggle prose in."""

    def test_parses_well_formed_lines(self):
        reply = ("exotérisme | esoterismo | essoterismo | significato opposto\n"
                 "exotérique | esoterico | essoterico | idem")
        out = llm.parse_glossary_extraction(reply)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["source"], "exotérisme")
        self.assertEqual(out[0]["correct"], "essoterismo")
        self.assertEqual(out[0]["reason"], "significato opposto")

    def test_none_and_prose_are_dropped(self):
        self.assertEqual(llm.parse_glossary_extraction("NONE"), [])
        self.assertEqual(llm.parse_glossary_extraction(
            "La traduzione mi sembra buona, non ho rilievi."), [])

    def test_malformed_lines_are_dropped(self):
        reply = ("solo due | campi\n"
                 "exotérisme | esoterismo | essoterismo | ok\n"
                 "| | |\n")
        out = llm.parse_glossary_extraction(reply)
        self.assertEqual(len(out), 1)

    def test_sentence_like_terms_are_dropped(self):
        long_term = "x" * 60
        reply = f"{long_term} | a | b | c"
        self.assertEqual(llm.parse_glossary_extraction(reply), [])

    def test_identical_terms_are_dropped(self):
        self.assertEqual(
            llm.parse_glossary_extraction("terme | X | terme | nessun cambiamento"), [])

    def test_tolerates_list_markers(self):
        out = llm.parse_glossary_extraction("- exotérisme | esoterismo | essoterismo | x")
        self.assertEqual(len(out), 1)


class TestSampling(unittest.TestCase):

    def test_limit_fits_real_chapters_whole(self):
        """The judge must see the whole text: the biggest chapter in the corpus
        (~86 KB) is far below the limit, so sampling stays an extreme-case net."""
        self.assertGreaterEqual(llm.MAX_CHECK_CHARS, 100000)
        big_chapter = "x" * 86000
        self.assertEqual(llm._sample(big_chapter), big_chapter)   # untouched

    def test_prompt_carries_full_text_under_limit(self):
        orig, trad = "a" * 50000, "b" * 50000
        p = llm.build_check_prompt(orig, trad, CFG)
        self.assertIn(orig, p)
        self.assertIn(trad, p)
        self.assertNotIn("[…]", p)

    def test_short_text_unchanged(self):
        self.assertEqual(llm._sample("short", 100), "short")

    def test_long_text_is_trimmed_with_markers(self):
        text = "x" * 6000
        out = llm._sample(text, limit=300)
        self.assertLess(len(out), len(text))
        self.assertIn("…", out)          # head/mid/tail markers present


class TestBackoffRetry(unittest.TestCase):
    """Transient HTTP 5xx errors are retried with backoff (urllib monkeypatched)."""

    def _patch(self, monkey_llm, responses):
        """Make urlopen yield the given sequence (HTTPError or SSE line lists)."""
        calls = []

        class _Resp:
            def __init__(self, lines):
                self._lines = [s.encode() for s in lines]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                return iter(self._lines)

        def fake_urlopen(req, timeout=None):
            idx = len(calls)
            calls.append(1)
            item = responses[min(idx, len(responses) - 1)]
            if isinstance(item, int):    # HTTP status code
                import urllib.error
                raise urllib.error.HTTPError(req.full_url, item, "err", {}, None)
            return _Resp(item)

        monkey_llm.time.sleep = lambda s: None
        monkey_llm.urllib.request.urlopen = fake_urlopen
        return calls

    def test_retries_507_then_succeeds(self):
        import io as _io

        from ai_epub_translator.ui import Progress
        sse_ok = ['data: ' +
                  __import__("json").dumps({"choices": [{"delta": {"content": "ok"},
                                                         "finish_reason": "stop"}]}),
                  'data: [DONE]']
        calls = self._patch(llm, [507, sse_ok])
        prog = Progress(Progress.STREAM, total=10, out=_io.StringIO())
        res = llm.chat("p", "http://x/v1", "m", max_tokens=8, progress=prog,
                       retries=3, backoff_s=0)
        self.assertEqual(res["text"], "ok")
        self.assertEqual(res["attempts"], 2)
        self.assertEqual(res["finish_reason"], "stop")
        self.assertEqual(len(calls), 2)

    def test_exhausts_retries_and_raises(self):
        import io as _io

        from ai_epub_translator.ui import Progress
        self._patch(llm, [507, 507, 507])
        prog = Progress(Progress.STREAM, total=10, out=_io.StringIO())
        with self.assertRaises(RuntimeError) as cm:
            llm.chat("p", "http://x/v1", "m", max_tokens=8, progress=prog,
                     retries=3, backoff_s=0)
        self.assertIn("HTTP 507", str(cm.exception))


class TestStreamWatchdog(unittest.TestCase):
    """A server that keeps the socket open but stops sending must not hang.

    Regression: an MLX deadlock left one chunk unsent for hours. urlopen's
    timeout guards only connect/read, not a silent-but-open stream — so the
    watchdogs in chat() must abort it as a transient error.
    """

    def _run_with_silent_server(self, monkey):
        import io as _io
        import time as _time

        from ai_epub_translator.ui import Progress

        class _Stall:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"content":"x"}}]}'
                while True:                          # socket open, no more data
                    _time.sleep(0.01)
                    yield b''

        monkey.STREAM_STALL_S = 1
        monkey.STREAM_TOTAL_S = 5                   # total must NOT be the cause: the
                                                    # empty-bytes trickle must trip the
                                                    # stall timer (a real SSE event is
                                                    # the only thing that resets it)
        orig = monkey.urllib.request.urlopen
        monkey.urllib.request.urlopen = lambda req, timeout=None: _Stall()
        try:
            with self.assertRaises(RuntimeError) as cm:
                monkey.chat("p", "http://x/v1", "m", max_tokens=8, retries=1,
                            progress=Progress(Progress.STREAM, total=10,
                                              out=_io.StringIO()))
            return str(cm.exception)
        finally:
            monkey.urllib.request.urlopen = orig

    def test_silent_stream_aborts(self):
        msg = self._run_with_silent_server(llm)
        self.assertIn("TimeoutError", msg)
        self.assertIn("stalled", msg)              # the stall path, not the total one

    def test_blocked_read_aborted_by_watchdog(self):
        """The real deadlock: the read blocks mid-line and never returns to the
        loop, so the in-loop check can never run. Only the watchdog thread closing
        the socket can break it — as closing a real socket interrupts a blocked
        recv(). Here close() unblocks the fake the same way.
        """
        import io as _io
        import threading as _threading

        from ai_epub_translator.ui import Progress

        class _Blocked:
            def __init__(self): self._closed = _threading.Event()
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def close(self): self._closed.set()
            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"content":"x"}}]}'
                self._closed.wait(10)                   # blocks like a stuck read
                raise OSError("stream closed")          # what a closed socket does

        orig_stall, orig_total = llm.STREAM_STALL_S, llm.STREAM_TOTAL_S
        orig_open = llm.urllib.request.urlopen
        llm.STREAM_STALL_S = 1
        llm.STREAM_TOTAL_S = 60                          # total must NOT be the cause
        llm.urllib.request.urlopen = lambda req, timeout=None: _Blocked()
        try:
            with self.assertRaises(RuntimeError) as cm:
                llm.chat("p", "http://x/v1", "m", max_tokens=8, retries=1,
                         progress=Progress(Progress.STREAM, total=10,
                                           out=_io.StringIO()))
            self.assertIn("TimeoutError", str(cm.exception))
            self.assertIn("stalled", str(cm.exception))  # the stall path, not total
        finally:
            llm.STREAM_STALL_S, llm.STREAM_TOTAL_S = orig_stall, orig_total
            llm.urllib.request.urlopen = orig_open

    def test_total_timeout_raises_stream_too_slow_not_retried(self):
        """Steady generation that out-runs STREAM_TOTAL_S is StreamTooSlow, not a
        retried transient error. The piece is too large for the model's throughput
        at the deadline, so retrying from zero cannot converge; distinct from a
        stall (which stays a retried TimeoutError). The unwrapped StreamTooSlow
        itself is the proof it was not caught by the transient-retry path.
        """
        import io as _io
        import time as _time

        from ai_epub_translator.ui import Progress

        class _Steady:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def __iter__(self):                           # data flows steadily...
                end = _time.time() + 5
                while _time.time() < end:
                    yield b'data: {"choices":[{"delta":{"content":"x"}}]}'
                    _time.sleep(0.05)

        orig_stall, orig_total = llm.STREAM_STALL_S, llm.STREAM_TOTAL_S
        orig_open = llm.urllib.request.urlopen
        llm.STREAM_STALL_S = 60                           # ...so the stall timer never
        llm.STREAM_TOTAL_S = 1                            # fires; the total timer does
        llm.urllib.request.urlopen = lambda req, timeout=None: _Steady()
        try:
            with self.assertRaises(llm.StreamTooSlow) as cm:
                llm.chat("p", "http://x/v1", "m", max_tokens=8, retries=3,
                         progress=Progress(Progress.STREAM, total=10,
                                           out=_io.StringIO()))
            self.assertIn("stream exceeded", str(cm.exception))
        finally:
            llm.STREAM_STALL_S, llm.STREAM_TOTAL_S = orig_stall, orig_total
            llm.urllib.request.urlopen = orig_open


class TestUnitPrompts(unittest.TestCase):
    """The unit prompts carry the rules a small model needs, and the glossary."""

    def setUp(self):
        from ai_epub_translator import units as U
        self.sk = U.segment('<html lang="en"><body><p>Hello <em>world</em></p>'
                            '<p>Second one here.</p></body></html>')
        self.cfg = {"source_lang": "english", "target_lang": "italian",
                    "register": "tu", "glossary": {"world": "mondo"}}

    def test_batch_prompt_lists_every_segment_and_the_rules(self):
        p = llm.build_units_prompt(self.sk.translatable, self.cfg)
        self.assertIn('<seg id="1">Hello <g1>world</g1></seg>', p)
        self.assertIn('<seg id="2">Second one here.</seg>', p)
        self.assertIn("NEVER summarize", p)
        self.assertIn("placeholders", p)
        self.assertIn("world -> mondo", p)
        self.assertIn('Address the reader as "tu"', p)

    def test_fix_prompt_states_the_reason_and_the_focus(self):
        u = self.sk.translatable[0]
        p = llm.build_unit_fix_prompt(u, "placeholder <g1> </g1> missing",
                                      self.cfg, focus="Keep <g1> around mondo.")
        self.assertIn("rejected: placeholder <g1> </g1> missing", p)
        self.assertIn("Keep <g1> around mondo.", p)
        self.assertIn('<seg id="1">Hello <g1>world</g1></seg>', p)
        self.assertIn("world -> mondo", p)

    def test_polish_prompt_carries_the_note_and_both_texts(self):
        u = self.sk.translatable[0]
        p = llm.build_polish_prompt([(u, "Ciao <g1>mondo</g1>")], "troppo formale", self.cfg)
        self.assertIn("troppo formale", p)
        self.assertIn('<seg id="1">\nORIGINAL: Hello <g1>world</g1>\nCURRENT: Ciao <g1>mondo</g1>\n</seg>', p)
        self.assertIn("reply exactly: NONE", p)


class TestUnitsReplyParsing(unittest.TestCase):

    def test_segments_are_read_in_any_wrapping(self):
        reply = ("Ecco:\n```xml\n<seg id=\"3\">tre <g1>a</g1></seg>\n"
                 "<seg id=4>quattro\n&lt;seg id=\"5\"&gt;cinque&lt;/seg&gt;\n```")
        self.assertEqual(llm.parse_units_reply(reply),
                         {3: "tre <g1>a</g1>", 4: "quattro", 5: "cinque"})

    def test_a_renumbered_reply_is_mapped_by_position(self):
        reply = '<seg id="57">a</seg><seg id="58">b</seg><seg id="59">c</seg>'
        self.assertEqual(llm.parse_units_reply(reply, expected=3), {1: "a", 2: "b", 3: "c"})
        # a wrong count is not remapped: the missing ones must be asked again
        self.assertEqual(llm.parse_units_reply('<seg id="9">a</seg>', expected=3), {9: "a"})

    def test_batches_respect_the_char_budget_and_keep_order(self):
        from ai_epub_translator import units as U
        body = "".join(f"<p>{'x' * 100} {i}</p>" for i in range(10))
        sk = U.segment(f"<html><body>{body}</body></html>")
        batches = llm._batches(sk.translatable, 350)
        self.assertEqual([len(b) for b in batches], [3, 3, 3, 1])
        self.assertEqual([u.idx for b in batches for u in b], list(range(10)))


def _seg_chat(answers, calls=None):
    """A fake chat answering from ``answers`` (a list of replies, in order)."""
    it = iter(answers)

    def chat(prompt, max_tokens, temperature=0.15, progress=None):
        if calls is not None:
            calls.append((prompt, temperature))
        return {"text": next(it), "prompt_tokens": 1, "completion_tokens": 1}
    return chat


class TestTranslateUnits(unittest.TestCase):

    def setUp(self):
        from ai_epub_translator import units as U
        self.sk = U.segment('<html lang="en"><body><p>Hello <em>world</em></p>'
                            '<p>Second one here.</p></body></html>')
        self.cfg = {"source_lang": "english", "target_lang": "italian",
                    "unit_retries": 2, "batch_chars": 16000}

    def _run(self, chat, **kw):
        kw.setdefault("log", lambda *a: None)
        return llm.translate_units(self.sk.translatable, self.cfg, chat, **kw)

    def test_valid_answers_are_spliced(self):
        chat = _seg_chat(['<seg id="1">Ciao <g1>mondo</g1></seg><seg id="2">Secondo.</seg>'])
        res = self._run(chat)
        self.assertEqual(res, {0: ("Ciao <em>mondo</em>", ""), 1: ("Secondo.", "")})

    def test_a_rejected_unit_is_retried_alone_and_warmer(self):
        calls = []
        chat = _seg_chat(['<seg id="1">Ciao mondo</seg><seg id="2">Secondo.</seg>',
                          '<seg id="1">Ciao <g1>mondo</g1></seg>'], calls)
        seen = []
        res = self._run(chat, on_unit=lambda u, inner, why: seen.append((u.idx, inner)))
        self.assertEqual(res[0], ("Ciao <em>mondo</em>", ""))
        self.assertEqual([t for _, t in calls], [0.15, 0.4])
        self.assertIn("rejected: placeholder <g1> </g1> missing", calls[1][0])
        self.assertEqual(seen, [(1, "Secondo."), (0, "Ciao <em>mondo</em>")])

    def test_a_missing_segment_is_asked_again(self):
        chat = _seg_chat(['<seg id="2">Secondo.</seg>',
                          'Ciao <g1>mondo</g1>'])            # bare answer accepted
        res = self._run(chat)
        self.assertEqual(res[0], ("Ciao <em>mondo</em>", ""))

    def test_a_stubborn_unit_fails_by_name_never_silently(self):
        chat = _seg_chat(['<seg id="1">Ciao mondo</seg><seg id="2">Secondo.</seg>',
                          '<seg id="1">Ciao mondo</seg>', '<seg id="1">Ciao mondo</seg>'])
        res = self._run(chat)
        self.assertEqual(res[0], (None, "placeholder <g1> </g1> missing"))

    def test_prior_attempts_bound_the_retries_across_runs(self):
        calls = []
        chat = _seg_chat(['<seg id="1">Ciao mondo</seg><seg id="2">Secondo.</seg>',
                          '<seg id="1">Ciao mondo</seg>'], calls)
        res = self._run(chat, history={0: (2, "placeholder <g1> </g1> missing")})
        self.assertIsNone(res[0][0])
        self.assertEqual(len(calls), 2)                 # the batch, then ONE retry left

    def test_a_unit_past_its_budget_is_not_asked_at_all(self):
        calls = []
        chat = _seg_chat(['<seg id="1">Secondo.</seg>'], calls)
        res = self._run(chat, history={0: (3, "placeholder <g1> </g1> missing")})
        self.assertEqual(res[0], (None, "gave up after 3 attempts (placeholder <g1> </g1> missing)"))
        self.assertEqual(res[1], ("Secondo.", ""))
        self.assertNotIn("Hello", calls[0][0])          # only unit 1 in the batch

    def test_a_too_slow_batch_is_split_not_failed(self):
        calls = []

        def chat(prompt, max_tokens, temperature=0.15, progress=None):
            calls.append(prompt)
            if len(calls) == 1:
                raise llm.StreamTooSlow("stream exceeded 1200s")
            if "Hello" in prompt:
                return {"text": '<seg id="1">Ciao <g1>mondo</g1></seg>'}
            return {"text": '<seg id="1">Secondo.</seg>'}
        res = self._run(chat)
        self.assertEqual(len(calls), 3)
        self.assertEqual(res[1], ("Secondo.", ""))

    def test_an_unreachable_server_propagates(self):
        def chat(prompt, max_tokens, **kw):
            raise RuntimeError("connection refused")
        with self.assertRaises(RuntimeError):
            self._run(chat)


if __name__ == "__main__":
    unittest.main()
