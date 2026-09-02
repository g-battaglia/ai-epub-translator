"""Source words left untranslated: found only when it is certainly a mistake.

The value of this check is entirely in what it refuses to report. A false positive
sends the model to "fix" a correct sentence, so each guard has its own test.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_epub_translator import leftovers  # noqa: E402

CFG = {"source_lang": "english", "target_lang": "italian"}


class TestFind(unittest.TestCase):

    def test_a_function_word_carried_over_is_found(self):
        orig = "<p>by a quite natural extension of this meaning</p>"
        trad = "<p>per una quite naturale estensione di questo significato</p>"
        self.assertEqual(leftovers.find(orig, trad, CFG), {"quite": 1})

    def test_every_occurrence_is_counted(self):
        orig = "<p>quite different</p><p>quite certain</p>"
        trad = "<p>quite diversi</p><p>è quite certo</p>"
        self.assertEqual(leftovers.find(orig, trad, CFG), {"quite": 2})

    def test_a_translated_chapter_reports_nothing(self):
        orig = "<p>by a quite natural extension</p>"
        trad = "<p>per una del tutto naturale estensione</p>"
        self.assertEqual(leftovers.find(orig, trad, CFG), {})

    def test_an_italicized_foreign_term_is_never_reported(self):
        # Guénon's prose is full of deliberate italics; they are quoted, not
        # forgotten. Even a word on the list must be ignored inside them.
        orig = '<p>the adjective <i>quite</i> as the English use it</p>'
        trad = '<p>l\'aggettivo <i>quite</i> come lo usano gli inglesi</p>'
        self.assertEqual(leftovers.find(orig, trad, CFG), {})

    def test_a_word_absent_from_the_original_is_not_a_carry_over(self):
        # Nothing to carry over: whatever put it there, it is not this defect.
        orig = "<p>a natural extension of this meaning</p>"
        trad = "<p>una estensione quite naturale</p>"
        self.assertEqual(leftovers.find(orig, trad, CFG), {})

    def test_a_word_inside_a_longer_word_is_not_a_match(self):
        orig = "<p>quite so</p>"
        trad = "<p>l'inquietudine e la requie</p>"
        self.assertEqual(leftovers.find(orig, trad, CFG), {})

    def test_tag_attributes_are_not_prose(self):
        orig = '<p>however this may be</p>'
        trad = '<p class="however">comunque sia</p>'
        self.assertEqual(leftovers.find(orig, trad, CFG), {})

    def test_an_unknown_source_language_reports_nothing(self):
        orig = "<p>quite natural</p>"
        trad = "<p>quite naturale</p>"
        self.assertEqual(
            leftovers.find(orig, trad, {"source_lang": "bulgarian"}), {})

    def test_a_book_can_add_its_own_words(self):
        cfg = dict(CFG, leftover_words=["thereupon"])
        orig = "<p>thereupon he left</p>"
        trad = "<p>thereupon se ne andò</p>"
        self.assertEqual(leftovers.find(orig, trad, cfg), {"thereupon": 1})


class TestHasAny(unittest.TestCase):
    """The acceptance test for a rewritten sentence rests on this."""

    def test_it_matches_a_carried_over_word(self):
        self.assertTrue(leftovers.has_any("<p>è quite certo</p>", {"quite"}))

    def test_it_clears_a_translated_sentence(self):
        self.assertFalse(leftovers.has_any("<p>è del tutto certo</p>", {"quite"}))

    def test_it_ignores_an_italicized_occurrence(self):
        self.assertFalse(leftovers.has_any("<p>il termine <i>quite</i></p>",
                                           {"quite"}))


class TestOccurrences(unittest.TestCase):
    """The unit of work is one sentence inside one text node."""

    DOC = ('<html><body><p>Prima frase. Per una quite naturale estensione, '
           'la parola cambia. Terza frase.</p></body></html>')

    def test_only_the_carrying_sentence_is_selected(self):
        got = leftovers.occurrences(self.DOC, {"quite"})
        self.assertEqual(len(got), 1)
        (start, end), fragment, word = got[0]
        self.assertEqual(word, "quite")
        self.assertIn("quite naturale", fragment)
        self.assertNotIn("Prima frase", fragment)
        self.assertNotIn("Terza frase", fragment)
        self.assertEqual(self.DOC[start:end], fragment)

    def test_two_sentences_in_one_node_are_both_returned(self):
        # A text node can hold several sentences: fixing only the first would
        # leave the file dirty while the run reported success.
        doc = '<p>Sia quite chiaro questo. E sia quite certo anche quello.</p>'
        got = leftovers.occurrences(doc, {"quite"})
        self.assertEqual(len(got), 2)
        self.assertNotEqual(got[0][0], got[1][0])

    def test_one_sentence_is_returned_once_per_word(self):
        doc = '<p>Sia quite chiaro e quite certo.</p>'
        self.assertEqual(len(leftovers.occurrences(doc, {"quite"})), 1)

    def test_the_fragment_never_contains_markup(self):
        doc = '<p>Sia quite chiaro.</p><p>Altro <i>corsivo</i> qui.</p>'
        for _span, fragment, _w in leftovers.occurrences(doc, {"quite"}):
            self.assertNotIn("<", fragment)


class TestFixLeftovers(unittest.TestCase):
    """Two strategies, an objective test, and no silence about what is left."""

    CFG = {"source_lang": "english", "target_lang": "italian",
           "dest_code": "it", "block_retries": 2}
    ORIG = '<html lang="en"><body><p>A quite natural extension.</p></body></html>'
    TRAD = '<html lang="it"><body><p>Una quite naturale estensione.</p></body></html>'

    def _fix(self, replies):
        from ai_epub_translator import repair
        seen = []

        def chat(prompt, max_tokens=None):
            seen.append(prompt)
            return {"text": replies[min(len(seen) - 1, len(replies) - 1)]}
        out = repair.fix_leftovers(self.ORIG, self.TRAD, self.CFG, chat_fn=chat)
        return out, seen

    def test_the_first_strategy_fixes_it_in_place(self):
        out, seen = self._fix(["Una del tutto naturale estensione."])
        self.assertEqual(out["fixed"], 1)
        self.assertEqual(out["remaining"], [])
        self.assertIn("del tutto naturale", out["text"])
        self.assertIn("<p>", out["text"])              # markup untouched
        self.assertIn('left in english: "quite"', seen[0].lower())

    def test_it_falls_back_to_translating_the_source_sentence(self):
        # the in-place prompt keeps failing; the retranslate prompt succeeds
        out, seen = self._fix(["Una quite naturale estensione.",
                               "Una quite naturale estensione.",
                               "Una estensione del tutto naturale."])
        self.assertEqual(out["fixed"], 1)
        self.assertIn("del tutto naturale", out["text"])
        self.assertIn("Translate this sentence", seen[-1])

    def test_a_word_the_model_refuses_is_reported_not_hidden(self):
        out, _seen = self._fix(["Una quite naturale estensione."])
        self.assertEqual(out["fixed"], 0)
        self.assertEqual(out["text"], self.TRAD)       # nothing written
        self.assertEqual(len(out["remaining"]), 1)
        word, sentence = out["remaining"][0]
        self.assertEqual(word, "quite")
        self.assertIn("quite naturale", sentence)

    def test_the_next_sentence_stays_separated(self):
        # The window ends where the next sentence begins, so the separator has to
        # survive the splice — otherwise "chiaro. Poi" comes back as "chiaro.Poi".
        from ai_epub_translator import repair
        orig = '<html lang="en"><body><p>Be quite clear. Then more.</p></body></html>'
        trad = '<html lang="it"><body><p>Sia quite chiaro. Poi altro.</p></body></html>'
        out = repair.fix_leftovers(
            orig, trad, self.CFG,
            chat_fn=lambda p, max_tokens=None: {"text": "Sia del tutto chiaro."})
        self.assertIn("chiaro. Poi altro.", out["text"])

    def test_a_rephrased_answer_is_rejected(self):
        out, _seen = self._fix(["No."])                # far too short
        self.assertEqual(out["fixed"], 0)

    def test_a_clean_translation_costs_no_call(self):
        from ai_epub_translator import repair

        def boom(*a, **k):
            raise AssertionError("the model must not be called")
        out = repair.fix_leftovers(
            self.ORIG, self.TRAD.replace("quite naturale", "del tutto naturale"),
            self.CFG, chat_fn=boom)
        self.assertEqual(out["fixed"], 0)
        self.assertEqual(out["remaining"], [])


if __name__ == "__main__":
    unittest.main()
