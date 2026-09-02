"""Unit tests for the tolerant XHTML lexer (ai_epub_translator/xhtml.py)."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_epub_translator import xhtml as X
from ai_epub_translator.xhtml import COMMENT, LT, OPEN, VOID_K, tokenize
from tests.samples import CHAPTER


def _rejoin(text: str) -> str:
    return "".join(tok.raw for tok in tokenize(text))


class TestTokenizer(unittest.TestCase):

    def test_roundtrip_byte_identical(self):
        """Concatenating token raws must reproduce the input exactly."""
        cases = [
            '<p class="x">Hello <em>world</em></p>',
            '<?xml version="1.0"?>\n<!DOCTYPE html>\n<html><body><p>&amp;&#160;</p></body></html>',
            '<p>a <br> b <img src="x" alt=""/></p>',
            '<!--c--><p>text</p>',
            '<a id="ch02ex01"/><p class="ex-caption"><em>Listing 2-1:</em></p>',
            '<p>unclosed <em>tag</p>',
        ]
        for c in cases:
            self.assertEqual(_rejoin(c), c, f"roundtrip failed for: {c!r}")

    def test_offsets_are_correct(self):
        text = '<p>x</p>'
        toks = tokenize(text)
        self.assertEqual(toks[0].start, 0)
        self.assertEqual(toks[-1].end, len(text))
        for tok in toks:
            self.assertEqual(text[tok.start:tok.end], tok.raw)

    def test_void_canonicalization(self):
        """``<br>`` and ``<br/>`` are both VOID events."""
        for tag in ("<br>", "<br/>", "<br />"):
            toks = tokenize(tag)
            self.assertEqual(len(toks), 1)
            self.assertEqual(toks[0].kind, VOID_K)
            self.assertEqual(toks[0].name, "br")

    def test_attributes_parsed_and_normalized(self):
        toks = tokenize('<a href="#a2" id="x" class=\'t5\'>t</a>')
        a = toks[0]
        self.assertEqual(a.kind, OPEN)
        self.assertEqual(a.name, "a")
        attrs = dict(a.attrs)
        self.assertEqual(attrs["href"], "#a2")
        self.assertEqual(attrs["id"], "x")
        self.assertEqual(attrs["class"], "t5")   # single-quote value normalized

    def test_attr_signature_drops_lang(self):
        a = tokenize('<p lang="en" xml:lang="en" id="z">x</p>')[0]
        sig = X.attr_signature(a)
        self.assertEqual(sig, (("id", "z"),))    # lang/xml:lang excluded, sorted

    def test_multiline_tag_and_doctype(self):
        text = ('<?xml version="1.0"?>\n'
                '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN"\n'
                '   "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
                '<html><body><p>ok</p></body></html>')
        toks = tokenize(text)
        kinds = [t.kind for t in toks]
        self.assertIn("decl", kinds)
        self.assertEqual(_rejoin(text), text)

    def test_stray_angle_does_not_crash(self):
        """A bare ``<`` (de-escaped ``a < b``) becomes an LT token, not a crash."""
        toks = tokenize("a < b and unclosed")
        self.assertIn(LT, [t.kind for t in toks])
        self.assertEqual(_rejoin("a < b and unclosed"), "a < b and unclosed")

    def test_comment_is_single_token(self):
        toks = tokenize("<!-- <p> not a tag --><p>x</p>")
        self.assertEqual(toks[0].kind, COMMENT)


class TestOnAChapterPage(unittest.TestCase):
    """Byte-identity on a whole page of awkward markup guards against regressions."""

    def test_chapter_roundtrip(self):
        self.assertEqual(_rejoin(CHAPTER), CHAPTER)

    def test_every_tag_is_seen(self):
        kinds = {tok.kind for tok in tokenize(CHAPTER)}
        self.assertIn(OPEN, kinds)
        self.assertIn(COMMENT, kinds)
        self.assertIn(VOID_K, kinds)


if __name__ == "__main__":
    unittest.main()
