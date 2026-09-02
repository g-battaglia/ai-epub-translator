"""Unit tests for the structural diff (ai_epub_translator/structdiff.py)."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_epub_translator import structdiff as S
from tests.samples import CHAPTER

ORIG = ('<?xml version="1.0"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" lang="en" xml:lang="en">'
        '<head><title>Hello</title></head><body>'
        '<p class="p1">First <em>sentence</em> with <a href="#x">link</a>.</p>'
        '<p class="p2">Second <span class="t5">phrase</span>.</p>'
        '</body></html>')


def _translate_with(orig: str, mutate) -> str:
    """Apply a mutation simulating the model's translation of ``orig``."""
    base = (orig.replace("Hello", "Ciao")
            .replace("First", "Primo")
            .replace("sentence", "frase")
            .replace("with", "con")
            .replace("link", "collegamento")
            .replace("Second", "Seconda")
            .replace("phrase", "frase")
            .replace('lang="en" xml:lang="en"', 'lang="it" xml:lang="it"'))
    return mutate(base)


def diff_files(orig, trad, cfg):
    """The plain-dict view of an analysis the tests were written against."""
    a = S.analyze(orig, trad, cfg)
    return {"passed": a.passed,
            "mismatches": [{"kind": d.kind, "detail": d.detail,
                            "block_trad": list(d.block_trad)} for d in a.defects]}


def _kinds(report: dict) -> list:
    return sorted(m["kind"] for m in report["mismatches"])


class TestCleanTranslation(unittest.TestCase):
    """A translation that only changes prose + lang passes with no defects."""

    def test_clean_passes(self):
        report = diff_files(ORIG, _translate_with(ORIG, lambda s: s), {})
        self.assertTrue(report["passed"], report["mismatches"])

    def test_lang_change_is_not_a_defect(self):
        report = diff_files(ORIG, _translate_with(ORIG, lambda s: s), {})
        self.assertNotIn("attr", _kinds(report))
        self.assertNotIn("prologue", _kinds(report))


class TestAttributeMessage(unittest.TestCase):
    """A malformed start tag must not bury the report under its pseudo-attributes.

    When the model breaks an attribute's quoting the tokenizer reads the swallowed
    prose as attributes — 1525 of them on csapp ch03. The count is the evidence
    that the tag is malformed; the list stops being evidence long before that.
    """

    def test_long_attribute_list_is_capped(self):
        detail = S._attr_list([(f"w{i}", "") for i in range(500)])
        self.assertLess(len(detail), 200)
        self.assertIn("+494 more", detail)

    def test_short_attribute_list_is_shown_whole(self):
        detail = S._attr_list([("class", "calibre36")])
        self.assertEqual(detail, "[class='calibre36']")
        self.assertNotIn("more", detail)

    def test_the_defect_is_still_reported(self):
        orig = '<html lang="en"><body><p class="a">Testo</p></body></html>'
        trad = '<html lang="it"><body><p class="b">Testo</p></body></html>'
        report = diff_files(orig, trad, {})
        self.assertFalse(report["passed"])
        self.assertIn("attr", _kinds(report))


class TestInlineReorder(unittest.TestCase):
    """Inline markup that moves with the word it marks is not a defect.

    The measured case (csapp ch11): "The TINY <tt>main</tt> Routine" becomes
    "La routine <tt>main</tt> di TINY", and the harness used to reject the whole
    chapter for `missing tag <span>; extra tag <span>` — then re-translate it for
    an hour and get the identical output back. Everything below the first test
    guards the other side of the trade: what must keep failing.
    """

    # One block, two inline units, translated with the Italian word order.
    MOVED_ORIG = ('<html lang="en"><body><p class="c1">The '
                  '<span class="n">TINY</span> <em>main</em> routine handles '
                  'every request that the server receives from a client.'
                  '</p></body></html>')
    MOVED_TRAD = ('<html lang="it"><body><p class="c1">La routine '
                  '<em>main</em> di <span class="n">TINY</span> gestisce '
                  'ogni richiesta che il server riceve da un client.'
                  '</p></body></html>')

    def test_moved_inline_tag_is_accepted(self):
        report = diff_files(self.MOVED_ORIG, self.MOVED_TRAD, {})
        self.assertTrue(report["passed"], report["mismatches"])

    def test_dropped_tag_is_still_a_defect(self):
        """A tag that leaves and never comes back must not cancel."""
        trad = self.MOVED_TRAD.replace("<em>main</em>", "main")
        report = diff_files(self.MOVED_ORIG, trad, {})
        self.assertFalse(report["passed"])
        self.assertIn("missing_tag", _kinds(report))

    def test_added_tag_is_still_a_defect(self):
        trad = self.MOVED_TRAD.replace("gestisce", "<em>gestisce</em>")
        report = diff_files(self.MOVED_ORIG, trad, {})
        self.assertFalse(report["passed"])
        self.assertIn("extra_tag", _kinds(report))

    def test_moved_tag_with_changed_attributes_is_a_defect(self):
        """Same tag name, different class: a different element, not a move."""
        trad = self.MOVED_TRAD.replace('<span class="n">', '<span class="z">')
        report = diff_files(self.MOVED_ORIG, trad, {})
        self.assertFalse(report["passed"], "a class change must not cancel")

    def test_move_does_not_mask_lost_prose(self):
        """Tags cancel only while the block still carries its text."""
        trad = ('<html lang="it"><body><p class="c1">La routine '
                '<em>main</em> di <span class="n">TINY</span>.'
                '</p></body></html>')
        report = diff_files(self.MOVED_ORIG, trad,
                              {"block_text_ratio": 0.7, "block_text_min": 40})
        self.assertFalse(report["passed"], "abbreviated block must still fail")

    def test_blocks_never_cancel(self):
        """Paragraph order is not a translator's choice."""
        orig = ('<html lang="en"><body><div>'
                '<p class="a">Alpha</p><h3 class="b">Beta</h3>'
                '</div></body></html>')
        trad = ('<html lang="it"><body><div>'
                '<h3 class="b">Beta</h3><p class="a">Alfa</p>'
                '</div></body></html>')
        report = diff_files(orig, trad, {})
        self.assertFalse(report["passed"], "a reordered block must still fail")


class TestCorruptions(unittest.TestCase):

    def test_missing_close_tag(self):
        def drop_close(s):
            return s.replace("</em>", "", 1)     # model dropped a </em>
        report = diff_files(ORIG, _translate_with(ORIG, drop_close), {})
        self.assertFalse(report["passed"])
        kinds = _kinds(report)
        self.assertIn("missing_tag", kinds)
        # localized on the translated side
        m = next(x for x in report["mismatches"] if x["kind"] == "missing_tag")
        self.assertTrue(m["block_trad"][1] > m["block_trad"][0])

    def test_extra_tag(self):
        def add_span(s):
            return s.replace("Seconda ", "Seconda <span> ", 1)
        report = diff_files(ORIG, _translate_with(ORIG, add_span), {})
        self.assertIn("extra_tag", _kinds(report))

    def test_attribute_change(self):
        def change_href(s):
            return s.replace('href="#x"', 'href="#y"')   # model altered href
        report = diff_files(ORIG, _translate_with(ORIG, change_href), {})
        self.assertIn("attr", _kinds(report))

    def test_missing_block_is_truncation(self):
        def drop_block(s):
            i = s.find('<p class="p2"')
            j = s.find("</p>", i) + 4
            return s[:i] + s[j:]                       # last block dropped
        report = diff_files(ORIG, _translate_with(ORIG, drop_block), {})
        kinds = _kinds(report)
        self.assertIn("missing_block", kinds)

    def test_missing_block_localizes_to_the_block_not_the_container(self):
        """Regression (#1): a dropped internal block must localize to that block,
        not to its <body> container — else repair re-inserts the whole body."""
        head = ('<?xml version="1.0"?>'
                '<html xmlns="http://www.w3.org/1999/xhtml">'
                '<head><title>T</title></head>')
        orig = head + '<body><p>Alpha</p><p>Beta</p><p>Gamma</p></body></html>'
        trad = head + '<body><p>Alfa</p><p>Gamma</p></body></html>'
        defects = [d for d in S.analyze(orig, trad, {}).defects
                   if d.kind == "missing_block"]
        self.assertTrue(defects)
        d = defects[0]
        span = d.block_orig[1] - d.block_orig[0]
        body = orig[orig.find("<body"):orig.find("</body>")]
        # the localized span is a single <p> (~12 chars), not the whole body
        self.assertLess(span, len(body) // 2)
        self.assertTrue(orig[d.block_orig[0]:d.block_orig[1]].startswith("<p>"))

    def test_inserted_lang_is_not_a_prologue_defect(self):
        """Regression: repair inserts lang on sources that lack it; the prologue
        comparison must not then flag our own fix as a mismatch."""
        orig = ('<?xml version="1.0"?>\n<html xmlns="http://www.w3.org/1999/xhtml">'
                '<body><p>Hello</p></body></html>')
        trad = ('<?xml version="1.0"?>\n<html xmlns="http://www.w3.org/1999/xhtml"'
                ' lang="it" xml:lang="it"><body><p>Ciao</p></body></html>')
        report = diff_files(orig, trad, {})
        self.assertTrue(report["passed"], report["mismatches"])

    def test_prologue_doctype_drop(self):
        def drop_decl(s):
            return s.replace('<?xml version="1.0"?>\n', "", 1)
        report = diff_files(ORIG, _translate_with(ORIG, drop_decl), {})
        self.assertIn("prologue", _kinds(report))

    def test_pure_rephrase_is_not_structural(self):
        """Rephrasing text without touching tags must not be flagged."""
        def rephrase(s):
            return s.replace("Primo", "La prima")     # different text, same tags
        report = diff_files(ORIG, _translate_with(ORIG, rephrase), {})
        # no missing/extra/wrong tag defects
        bad = [k for k in _kinds(report)
               if k in ("missing_tag", "extra_tag", "wrong_tag")]
        self.assertEqual(bad, [], report["mismatches"])


class TestContentDefects(unittest.TestCase):
    """Prose lost inside aligned blocks: the tags match, only the text differs."""

    LONG = ("Questa e una frase lunga che serve a superare la soglia minima di "
            "caratteri prevista dal controllo di contenuto per blocco di prosa, "
            "cosi il confronto viene eseguito davvero e non saltato per rumore.")

    def _doc(self, body: str) -> str:
        return ('<html lang="en" xml:lang="en"><body>' + body + "</body></html>")

    def test_abbreviated_block_is_flagged(self):
        orig = self._doc(f'<p class="a">{self.LONG}</p>')
        trad = self._doc('<p class="a">Questa e una frase breve...</p>')
        report = diff_files(orig, trad, {})
        self.assertFalse(report["passed"])
        kinds = [m["kind"] for m in report["mismatches"]]
        self.assertIn("short_text", kinds)
        detail = next(m["detail"] for m in report["mismatches"]
                      if m["kind"] == "short_text")
        self.assertIn("abbreviated", detail)

    def test_full_translation_passes(self):
        orig = self._doc(f'<p class="a">{self.LONG}</p>')
        trad = self._doc(f'<p class="a">{self.LONG} ok</p>')
        report = diff_files(orig, trad, {})
        self.assertTrue(report["passed"], report["mismatches"])

    def test_short_block_is_ignored(self):
        # under block_text_min: too short to judge, must not be flagged
        orig = self._doc('<p>Ciao mondo bello</p>')
        trad = self._doc('<p>Hi</p>')
        report = diff_files(orig, trad, {})
        self.assertNotIn("short_text", [m["kind"] for m in report["mismatches"]])

    def test_an_ellipsis_the_source_spells_as_an_entity_is_not_added(self):
        orig = '<html><body><p>' + "word " * 30 + 'and so on&#8230; the end.</p></body></html>'
        trad = '<html><body><p>' + "parola " * 30 + 'e così via… la fine.</p></body></html>'
        report = diff_files(orig, trad, {})
        self.assertNotIn("ellipsis", [m["kind"] for m in report["mismatches"]])

    def test_added_ellipsis_is_flagged(self):
        # same length, but the model inserted an ellipsis that was not there
        text = self.LONG
        orig = self._doc(f"<p>{text}</p>")
        trad = self._doc(f"<p>{text[:len(text)//2]}... {text[len(text)//2:]}</p>")
        report = diff_files(orig, trad, {})
        self.assertIn("ellipsis", [m["kind"] for m in report["mismatches"]])

    def test_ellipsis_form_conversion_is_not_flagged(self):
        # "…" -> "..." keeps the count identical: legitimate, must pass
        orig = self._doc(f"<p>{self.LONG} …</p>")
        trad = self._doc(f"<p>{self.LONG} ...</p>")
        report = diff_files(orig, trad, {})
        self.assertTrue(report["passed"], report["mismatches"])

    def test_tightening_a_spaced_ellipsis_is_not_flagged(self):
        # Old typesetting writes ". . ."; tightening it to "..." adds nothing.
        # Counting only the compact form made this look like an ellipsis the model
        # had inserted to cover omitted text — a defect no translation could clear.
        orig = self._doc(f"<p>{self.LONG} over time. . .which is what happened.</p>")
        trad = self._doc(f"<p>{self.LONG} nel tempo... che è ciò che accadde.</p>")
        report = diff_files(orig, trad, {})
        self.assertNotIn("ellipsis", [m["kind"] for m in report["mismatches"]])

    def test_an_ellipsis_added_next_to_a_spaced_one_is_still_flagged(self):
        # The looser pattern must not blind the check: two markers where the
        # original had one is still text likely omitted.
        orig = self._doc(f"<p>{self.LONG} over time. . .which is what happened.</p>")
        trad = self._doc(f"<p>{self.LONG} nel tempo... che è... accadde.</p>")
        report = diff_files(orig, trad, {})
        self.assertIn("ellipsis", [m["kind"] for m in report["mismatches"]])

    def test_code_block_is_skipped(self):
        # code is never translated: a class-hinted block must not be flagged
        cfg = {"code_class_hints": ["programs"]}
        orig = self._doc(f'<p class="programs">{self.LONG}</p>')
        trad = self._doc('<p class="programs">x</p>')
        report = diff_files(orig, trad, cfg)
        self.assertNotIn("short_text", [m["kind"] for m in report["mismatches"]])

    def test_custom_ratio_is_honoured(self):
        orig = self._doc(f'<p>{self.LONG}</p>')
        trad = self._doc(f'<p>{self.LONG[:int(len(self.LONG) * 0.75)]}</p>')
        # default ratio 0.7 -> 75% passes
        self.assertTrue(diff_files(orig, trad, {})["passed"])
        # stricter ratio 0.9 -> 75% is flagged
        report = diff_files(orig, trad, {"block_text_ratio": 0.9})
        self.assertIn("short_text", [m["kind"] for m in report["mismatches"]])


class TestGlossarySourceMatching(unittest.TestCase):
    """A pinned term must START a word; a suffix is fine, a prefix is not.

    Matching it anywhere made a term demand its rendering in blocks that never
    contained it: "trine" fired on "doctrines", so a correct chapter of The Twelve
    Houses failed verification, exhausted its repair attempts, was re-translated
    whole and failed again — a loop no translation could ever clear. The suffix has
    to keep matching, though: that is what catches the inflected source forms
    ("esoterisme" in "esoterismes").
    """

    GLO = {"trine": "trigono", "esoterisme": "esoterismo"}

    def _missing(self, orig, trad):
        return S._glossary_defects(orig, trad, self.GLO)

    def test_the_term_inside_a_longer_word_is_not_the_term(self):
        self.assertEqual(self._missing("the doctrines of the school",
                                       "le dottrine della scuola"), [])

    def test_the_term_itself_is_still_checked(self):
        self.assertEqual(self._missing("a trine to Pluto", "un aspetto a Plutone"),
                         [("trine", "trigono")])

    def test_an_inflected_source_term_still_matches(self):
        self.assertEqual(self._missing("planets trines each other",
                                       "i pianeti si toccano"),
                         [("trine", "trigono")])
        self.assertEqual(self._missing("les esoterismes", "gli aspetti"),
                         [("esoterisme", "esoterismo")])

    def test_a_rendered_term_passes(self):
        self.assertEqual(self._missing("a trine to Pluto",
                                       "un trigono a Plutone"), [])


class TestGlossaryInflectionAndIdentity(unittest.TestCase):
    """Two ways a CORRECT translation used to be flagged, both unclearable.

    On the Rudhyar book these two bugs produced 76 of the 79 glossary flags raised
    over 648 translated blocks: every one of them on faultless Italian, each costing
    the repair attempts and then a whole re-translation that could not clear it.
    """

    def test_a_rendering_identical_to_the_source_passes(self):
        # Italian spells these exactly as English does. The source word surviving
        # in the translation IS the correct rendering, so the left-untranslated
        # test cannot fire — it would make the entry impossible to satisfy.
        glo = {"quintile": "quintile", "novile": "novile"}
        self.assertEqual(S._glossary_defects("the quintile aspect",
                                             "l'aspetto quintile", glo), [])
        self.assertEqual(S._glossary_defects("the novile", "il novile", glo), [])

    def test_an_identical_rendering_is_still_checked_for_presence(self):
        # The real defect this caught in the book: the model split "bi-novile"
        # into two Italian words, "bino vile".
        self.assertEqual(
            S._glossary_defects("a bi-novile between Mars and Uranus",
                                "un bino vile tra Marte e Urano",
                                {"novile": "novile"}),
            [("novile", "novile")])

    def test_the_italian_plural_of_a_seven_letter_rendering_passes(self):
        # "trigono", "sestile", "settile" are exactly 7 chars: they used to be
        # matched literally, so the ordinary plural failed.
        self.assertEqual(S._glossary_defects("two trines", "due trigoni",
                                             {"trine": "trigono"}), [])
        self.assertEqual(S._glossary_defects("several sextiles", "diversi sestili",
                                             {"sextile": "sestile"}), [])
        self.assertEqual(S._glossary_defects("septiles", "settili",
                                             {"septile": "settile"}), [])

    def test_a_cognate_plural_spelled_like_the_source_passes(self):
        # Cosmos and Psyche: "the conjunctions, oppositions, and squares" came
        # back perfectly as "le congiunzioni, le opposizioni e le quadrature" —
        # but the Italian plural of "quadratura" is spelled exactly like the
        # English "quadrature", and the left-untranslated test read it as a
        # leftover. A cognate pair is judged by `rendered` alone.
        self.assertEqual(
            S._glossary_defects("only the quadrature alignments: conjunctions, "
                                "oppositions, and squares",
                                "solo gli allineamenti di quadratura: le "
                                "congiunzioni, le opposizioni e le quadrature",
                                {"quadrature": "quadratura"}), [])

    def test_a_cognate_pair_left_untranslated_is_still_checked(self):
        # the blind spot is only the leftover test: the rendering must still
        # appear — a block that never says "quadratur-" fails as before
        self.assertEqual(
            S._glossary_defects("the quadrature alignments",
                                "gli allineamenti di configurazione",
                                {"quadrature": "quadratura"}),
            [("quadrature", "quadratura")])

    def test_a_short_rendering_still_discriminates(self):
        # The looser stem must not turn into "anything goes": a wrong rendering
        # is still caught, and two near-identical terms stay apart.
        self.assertEqual(S._glossary_defects("two trines", "due quadrature",
                                             {"trine": "trigono"}),
                         [("trine", "trigono")])
        self.assertEqual(S._glossary_defects("the septile", "il sestile",
                                             {"septile": "settile"}),
                         [("septile", "settile")])


class TestGlossaryDefects(unittest.TestCase):
    """Pinned terms must be rendered as required — structure cannot see this."""

    GLO = {"exotérisme": "essoterismo"}

    def _doc(self, body: str) -> str:
        return f'<html lang="en"><body>{body}</body></html>'

    def _kinds(self, orig, trad, glossary=None):
        cfg = {"glossary": glossary if glossary is not None else self.GLO}
        return [m["kind"] for m in diff_files(orig, trad, cfg)["mismatches"]]

    def test_wrong_rendering_is_flagged(self):
        # the real bug: exotérisme -> "esoterismo" (its opposite)
        orig = self._doc("<p>Le point de vue de l'exotérisme est limité.</p>")
        trad = self._doc("<p>Il punto di vista dell'esoterismo è limitato.</p>")
        self.assertIn("glossary", self._kinds(orig, trad))

    def test_correct_rendering_passes(self):
        orig = self._doc("<p>Le point de vue de l'exotérisme est limité.</p>")
        trad = self._doc("<p>Il punto di vista dell'essoterismo è limitato.</p>")
        self.assertNotIn("glossary", self._kinds(orig, trad))

    def test_term_absent_from_block_is_not_checked(self):
        orig = self._doc("<p>Une phrase sans le terme en question.</p>")
        trad = self._doc("<p>Una frase senza il termine in questione.</p>")
        self.assertNotIn("glossary", self._kinds(orig, trad))

    def test_empty_glossary_changes_nothing(self):
        orig = self._doc("<p>Le point de vue de l'exotérisme est limité.</p>")
        trad = self._doc("<p>Il punto di vista dell'esoterismo è limitato.</p>")
        self.assertNotIn("glossary", self._kinds(orig, trad, glossary={}))

    def test_inflected_form_counts_as_correct(self):
        # "essoterici" shares the stem of "essoterismo": the concept is rendered
        orig = self._doc("<p>Les dogmes exotérisme et leurs limites.</p>")
        trad = self._doc("<p>I dogmi essoterici e i loro limiti.</p>")
        self.assertNotIn("glossary", self._kinds(orig, trad))

    def test_accent_and_case_insensitive(self):
        orig = self._doc("<p>Exoterisme sans accent, en début de phrase.</p>")
        trad = self._doc("<p>Esoterismo senza accento, a inizio frase.</p>")
        self.assertIn("glossary", self._kinds(orig, trad))

    def test_detail_names_term_and_expected(self):
        orig = self._doc("<p>Le point de vue de l'exotérisme est limité.</p>")
        trad = self._doc("<p>Il punto di vista dell'esoterismo è limitato.</p>")
        d = next(m for m in diff_files(orig, trad, {"glossary": self.GLO})["mismatches"]
                 if m["kind"] == "glossary")
        self.assertIn("exotérisme", d["detail"])
        self.assertIn("essoterismo", d["detail"])


class TestSelfDiff(unittest.TestCase):
    """A file compared to itself passes — whatever markup it contains."""

    def test_self_diff_passes(self):
        report = diff_files(CHAPTER, CHAPTER, {})
        self.assertTrue(report["passed"], report["mismatches"][:3])


if __name__ == "__main__":
    unittest.main()
