"""Characterization tests for config loading (ai_epub_translator/config.py)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_epub_translator import config as C

TOML = """
[model]
model = "omlx/gemma"
base_url = "http://localhost:9000/v1"

[lingue]
origine = "inglese"
destinazione = "italiano"

[verify]
truncation_ratio = 0.4
pagebreak_re = 'epub:type="pagebreak"'

[detection]
code_class_hints = ["programs", "literal"]
"""


class TestTomlParser(unittest.TestCase):

    def test_parse_sections_and_values(self):
        d = C.parse_toml(TOML)
        self.assertEqual(d["model"]["model"], "omlx/gemma")
        self.assertEqual(d["lingue"]["destinazione"], "italiano")
        self.assertEqual(d["verify"]["truncation_ratio"], "0.4")
        self.assertEqual(d["detection"]["code_class_hints"], ["programs", "literal"])

    def test_comments_and_quotes(self):
        d = C.parse_toml('k = "v" # trailing\n[sec]\nx = 1')
        self.assertEqual(d["k"], "v")
        self.assertEqual(d["sec"]["x"], "1")


class TestMergedConfig(unittest.TestCase):

    def test_book_overrides_harness(self):
        with tempfile.TemporaryDirectory() as bd:
            with open(os.path.join(bd, "book.toml"), "w", encoding="utf-8") as f:
                f.write('[languages]\nsource = "french"\ntarget = "italian"\n')
            cfg = C.merged_config(bd)
            self.assertEqual(cfg["source_lang"], "french")      # book wins
            self.assertEqual(cfg["target_lang"], "italian")

    def test_defaults_filled(self):
        with tempfile.TemporaryDirectory() as bd:
            cfg = C.merged_config(bd)
            self.assertEqual(cfg["dest_code"], "it")
            self.assertEqual(cfg["unit_retries"], 2)
            self.assertEqual(cfg["batch_chars"], 16000)
            self.assertEqual(cfg["block_retries"], 2)
            self.assertEqual(cfg["progress"], "percent")   # default display mode
            self.assertIsInstance(cfg["truncation_ratio"], float)

    def test_dest_code_derives_from_target(self):
        with tempfile.TemporaryDirectory() as bd:
            with open(os.path.join(bd, "book.toml"), "w", encoding="utf-8") as f:
                f.write('[languages]\ntarget = "french"\n')
            self.assertEqual(C.merged_config(bd)["dest_code"], "fr")


class TestGlossary(unittest.TestCase):
    """Per-book glossary: absent by default, round-trips through TOML."""

    def test_absent_glossary_is_empty(self):
        with tempfile.TemporaryDirectory() as bd:
            self.assertEqual(C.load_glossary(bd), {})
            self.assertEqual(C.merged_config(bd)["glossary"], {})

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as bd:
            terms = {"exotérisme": "essoterismo", "ésotérisme": "esoterismo"}
            C.save_glossary(bd, terms)
            self.assertEqual(C.load_glossary(bd), terms)
            self.assertEqual(C.merged_config(bd)["glossary"], terms)

    def test_accented_terms_survive(self):
        with tempfile.TemporaryDirectory() as bd:
            C.save_glossary(bd, {"exotérique": "essoterico"})
            self.assertIn("exotérique", C.load_glossary(bd))

    def test_empty_entries_are_ignored(self):
        with tempfile.TemporaryDirectory() as bd:
            with open(os.path.join(bd, "glossary.toml"), "w", encoding="utf-8") as f:
                f.write('[terms]\n"a" = "b"\n"" = "x"\n"c" = ""\n')
            self.assertEqual(C.load_glossary(bd), {"a": "b"})


class TestGlossaryExceptions(unittest.TestCase):
    """A term pinned for the prose can be wrong inside a proper name."""

    def test_exceptions_are_read_and_survive_a_save(self):
        from ai_epub_translator.config import (
            load_glossary,
            load_glossary_exceptions,
            merged_config,
            save_glossary,
        )
        with tempfile.TemporaryDirectory() as bd:
            with open(os.path.join(bd, "glossary.toml"), "w", encoding="utf-8") as f:
                f.write('[terms]\n"archetypal" = "archetipico"\n\n'
                        '[exceptions]\n"archetypal" = ["Archetypal Psychology"]\n')
            self.assertEqual(load_glossary_exceptions(bd),
                             {"archetypal": ["Archetypal Psychology"]})
            save_glossary(bd, dict(load_glossary(bd), trine="trigono"))
            self.assertEqual(load_glossary_exceptions(bd),
                             {"archetypal": ["Archetypal Psychology"]})
            self.assertEqual(merged_config(bd)["glossary_exceptions"],
                             {"archetypal": ["Archetypal Psychology"]})


class TestGlossaryNotesConfig(unittest.TestCase):
    """Notes: multi-line TOML strings, preserved across saves."""

    def test_multiline_notes_are_parsed(self):
        with tempfile.TemporaryDirectory() as bd:
            with open(os.path.join(bd, "glossary.toml"), "w", encoding="utf-8") as f:
                f.write('[terms]\n"a" = "b"\n\n[notes]\ntext = """\n'
                        'line one\nline two\n"""\n')
            self.assertEqual(C.load_glossary_notes(bd), "line one\nline two")
            self.assertEqual(C.merged_config(bd)["glossary_notes"],
                             "line one\nline two")

    def test_absent_notes_are_empty(self):
        with tempfile.TemporaryDirectory() as bd:
            C.save_glossary(bd, {"a": "b"})
            self.assertEqual(C.load_glossary_notes(bd), "")

    def test_notes_survive_a_term_being_added(self):
        # regression: adding a term must not silently drop the notes
        with tempfile.TemporaryDirectory() as bd:
            C.save_glossary(bd, {"a": "b"}, notes="do not confuse X and Y")
            self.assertEqual(C.load_glossary_notes(bd), "do not confuse X and Y")
            C.save_glossary(bd, {"a": "b", "c": "d"})       # notes not passed
            self.assertEqual(C.load_glossary_notes(bd), "do not confuse X and Y")
            self.assertEqual(C.load_glossary(bd), {"a": "b", "c": "d"})

    def test_terms_and_notes_roundtrip(self):
        with tempfile.TemporaryDirectory() as bd:
            C.save_glossary(bd, {"exotérisme": "essoterismo"},
                            notes="ex- -> ess-, és- -> es-")
            self.assertEqual(C.load_glossary(bd), {"exotérisme": "essoterismo"})
            self.assertIn("ex- -> ess-", C.load_glossary_notes(bd))


class TestCodeGrep(unittest.TestCase):

    def test_alternation(self):
        self.assertEqual(C.code_grep([]), "")
        self.assertIn('class="programs"', C.code_grep(["programs", "literal"]))


if __name__ == "__main__":
    unittest.main()
