"""Unit tests for the quality-gate rewrites and helpers (ai_epub_translator/repair.py)."""

from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_epub_translator import repair as R  # noqa: E402


def down(prompt: str, max_tokens: int) -> dict:
    """Mock simulating an unreachable LLM server."""
    raise RuntimeError("LLM unreachable")


class TestNamedEntities(unittest.TestCase):

    def test_html_named_entities_become_xml_valid(self):
        out = R._fix_named_entities("a&mdash;b &copy; &amp; &lt; &unknownx;")
        self.assertEqual(out, "a&#8212;b &#169; &amp; &lt; &amp;unknownx;")


class TestRewriteLang(unittest.TestCase):
    """The language attribute must end up on <html> even when absent upstream."""

    def test_rewrites_existing_values(self):
        out = R.rewrite_lang('<html lang="fr" xml:lang="fr">', "it")
        self.assertEqual(out, '<html lang="it" xml:lang="it">')

    def test_inserts_when_missing(self):
        # jacket.xhtml has no lang at all: a pure substitution would leave the
        # file permanently failing the `lang` check.
        out = R.rewrite_lang('<html xmlns="http://www.w3.org/1999/xhtml">', "it")
        self.assertIn('lang="it"', out)
        self.assertIn('xmlns="http://www.w3.org/1999/xhtml"', out)   # kept

    def test_inserts_only_on_html_tag(self):
        doc = '<?xml version="1.0"?>\n<html xmlns="y">\n<body><p>x</p></body></html>'
        out = R.rewrite_lang(doc, "it")
        self.assertIn('<html xmlns="y" lang="it" xml:lang="it">', out)
        self.assertIn("<body><p>x</p></body>", out)                  # body untouched

    def test_partial_lang_is_completed(self):
        out = R.rewrite_lang('<html xmlns="x" lang="en">', "it")
        self.assertIn('lang="it"', out)
        self.assertNotIn('lang="en"', out)


class TestLineEndings(unittest.TestCase):

    def test_follows_the_reference(self):
        self.assertEqual(R.match_line_ending("a\nb", "x\r\ny"), "a\r\nb")
        self.assertEqual(R.match_line_ending("a\r\nb", "x\ny"), "a\nb")


class TestQuotedCorrection(unittest.TestCase):
    """A note that states its own correction is applied literally, no model.

    Every chapter of the Rudhyar book carries the running title translated with
    a typo in its <title> — "ASPECTI" for "ASPETTI" — and the judge said so in
    exactly those words, 27 times. The unit walk skips a short <title>, so the
    note "did not map to any block" and the typo survived every pass.
    """

    def test_a_stated_typo_is_replaced_with_case_kept(self):
        trad = "<html><head><title>ASPECTI ASTROLOGICI</title></head></html>"
        note = 'Errore di ortografia: "ASPECTI" invece di "ASPETTI".'
        out, n = R.apply_quoted_correction(trad, note)
        self.assertEqual(n, 1)
        self.assertIn("<title>ASPETTI ASTROLOGICI</title>", out)

    def test_polish_counts_a_literal_fix_without_the_model(self):
        # no unit is long enough: the literal fix alone must register as a change
        trad = ('<html><head><title>ASPECTI ASTROLOGICI</title></head><body>'
                '<p>Capitolo primo, testo breve.</p></body></html>')
        orig = ('<html><head><title>ASTROLOGICAL ASPECTS</title></head><body>'
                '<p>Chapter one, short text.</p></body></html>')
        note = 'Errore: "ASPECTI" invece di "ASPETTI".'

        def boom(prompt, max_tokens):
            raise AssertionError("the model must not be called")
        out = R.polish_file(orig, trad, note, {"dest_code": "it"},
                            chat_fn=boom)
        self.assertIn("ASPETTI", out["text"])
        self.assertEqual(out["changed"], 1)

    def test_a_rewording_is_not_a_typo(self):
        out, n = R.apply_quoted_correction("<p>il cane corre</p>",
                                           '"cane" invece di "gatto"')
        self.assertEqual(n, 0)
        self.assertIn("cane", out)

    def test_a_common_word_is_left_alone(self):
        # "Luce" is real Italian: replacing every occurrence would wreck the
        # chapter, so a frequent wrong form is refused
        trad = "<p>" + "La Luce e la luce. " * 4 + "</p>"
        out, n = R.apply_quoted_correction(trad, '"Luce" invece di "Luci"')
        self.assertEqual(n, 0)
        self.assertEqual(out, trad)

    def test_rare_word_is_replaced(self):
        trad = "<p>Le <i>Luce</i> della città.</p>"
        out, n = R.apply_quoted_correction(trad, '"Luce" invece di "Luci"')
        self.assertEqual(n, 1)
        self.assertIn("Luci", out)


def _currents(prompt: str) -> list:
    """[(id, current translation)] from a batch polish prompt."""
    return [(int(i), cur) for i, cur in
            re.findall(r'<seg id="(\d+)">\nORIGINAL: .*?\nCURRENT: (.*?)\n</seg>', prompt, re.S)]


class TestPolish(unittest.TestCase):
    """polish rewrites units to fix a reviewer's issue, on protected text."""

    DOC = ('<html lang="it"><body>'
           '<p class="a">Il testo dice tu al lettore in questa <em>frase</em> piuttosto lunga.</p>'
           '<p class="b">E anche qui continua a dare del tu al lettore, sempre così.</p>'
           '</body></html>')
    ORIG = ('<html lang="fr"><body>'
            '<p class="a">Le texte dit vous au lecteur dans cette <em>phrase</em> assez longue.</p>'
            '<p class="b">Et ici aussi il continue de vouvoyer le lecteur, toujours ainsi.</p>'
            '</body></html>')

    def test_changed_units_are_spliced_from_one_call(self):
        calls = []

        def fix(prompt, max_tokens):
            calls.append(prompt)
            return {"text": "".join(
                f'<seg id="{i}">{cur.replace("tu ", "noi ").replace("del tu", "del noi")}</seg>'
                for i, cur in _currents(prompt))}
        out = R.polish_file(self.ORIG, self.DOC, "usa 'noi' non 'tu'",
                            {"dest_code": "it"}, chat_fn=fix)
        self.assertEqual(len(calls), 1)                        # both units, one call
        self.assertEqual(out["changed"], 2)
        self.assertIn("noi", out["text"])
        self.assertIn("<em>frase</em>", out["text"])           # markup restored
        self.assertNotIn(">Il testo dice tu", out["text"])   # rewritten

    def test_unchanged_when_model_returns_none_or_the_same(self):
        for reply in ("NONE", lambda p: "".join(f'<seg id="{i}">{c}</seg>'
                                                 for i, c in _currents(p))):
            out = R.polish_file(self.ORIG, self.DOC, "any issue", {"dest_code": "it"},
                                chat_fn=lambda p, max_tokens, r=reply: {
                                    "text": r if isinstance(r, str) else r(p)})
            self.assertEqual(out["changed"], 0)
            self.assertEqual(out["text"], self.DOC)

    def test_placeholder_breaking_answer_is_rejected(self):
        def breaks(prompt, max_tokens):
            return {"text": "".join(f'<seg id="{i}">testo senza i segnaposto, riscritto</seg>'
                                    for i, _c in _currents(prompt))}
        out = R.polish_file(self.ORIG, self.DOC, "x",
                            {"dest_code": "it"}, chat_fn=breaks)
        self.assertEqual(out["changed"], 1)        # only unit b (no placeholder)
        self.assertIn("<em>frase</em>", out["text"])

    def test_misaligned_files_do_nothing(self):
        trad = '<html lang="it"><body><p>solo un blocco</p></body></html>'
        out = R.polish_file(self.ORIG, trad, "x", {"dest_code": "it"},
                            chat_fn=lambda p, max_tokens: {"text": ""})
        self.assertEqual(out["changed"], 0)

    def test_server_down_aborts(self):
        out = R.polish_file(self.ORIG, self.DOC, "x", {"dest_code": "it"},
                            chat_fn=down)
        self.assertTrue(out.get("aborted"))


class TestGlossaryEvidence(unittest.TestCase):
    """The retry focus names what the model wrote instead of the rendering."""

    def test_twin_glossary_confusion_is_named(self):
        # esoterism/exoterism: the model wrote the twin's rendering. Say so.
        glossary = {"esoterism": "esoterismo", "exoterism": "essoterismo"}
        note = R._wrong_rendering(
            "esoterism", "esoterismo",
            "In Islamic esoterism the seven lands appear.",
            "Nell'essoterismo islamico le sette terre appaiono.", glossary)
        self.assertIn("essoterismo", note)
        self.assertIn("exoterism", note)
        self.assertIn("DIFFERENT", note)

    def test_no_confusion_claimed_when_both_terms_are_in_the_original(self):
        glossary = {"esoterism": "esoterismo", "exoterism": "essoterismo"}
        note = R._wrong_rendering(
            "esoterism", "esoterismo",
            "The exoterism and the esoterism of a tradition.",
            "L'essoterismo e il ... di una tradizione.", glossary)
        self.assertEqual(note, "")

    def test_near_miss_word_is_reported_without_a_twin_entry(self):
        glossary = {"initiation": "iniziazione"}
        note = R._wrong_rendering("initiation", "iniziazione",
                                  "The initiation is real.",
                                  "La inizazione e reale.", glossary)
        self.assertIn("inizazione", note)

    def test_evidence_travels_into_the_retry_focus(self):
        from ai_epub_translator import llm
        from ai_epub_translator import units as U
        glossary = {"esoterism": "esoterismo", "exoterism": "essoterismo"}
        cfg = {"glossary": glossary, "source_lang": "english"}
        sk = U.segment("<p>In Islamic esoterism the lands appear.</p>")
        (u,) = sk.translatable
        inner, why, focus = llm.accept_unit(
            u, "Nell'essoterismo islamico le terre appaiono.", cfg)
        self.assertIsNone(inner)
        self.assertIn("'esoterism'", why)
        self.assertIn("essoterismo", focus)
        self.assertIn("DIFFERENT", focus)


if __name__ == "__main__":
    unittest.main()
