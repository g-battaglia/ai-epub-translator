"""Tests for the ``setup`` command and slug derivation (ai_epub_translator/cli.py)."""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_epub_translator import cli
from ai_epub_translator.config import merged_config


def _make_minimal_epub(path: str) -> None:
    """Write a tiny but valid EPUB (one spine file) to ``path``."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?>'
                   '<container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles>'
                   '</container>')
        z.writestr("OEBPS/content.opf",
                   '<?xml version="1.0"?>'
                   '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
                   'unique-identifier="bookid">'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   '<dc:identifier id="bookid">id</dc:identifier>'
                   '<dc:title>T</dc:title></metadata>'
                   '<manifest><item id="c1" href="chapter.xhtml" '
                   'media-type="application/xhtml+xml"/></manifest>'
                   '<spine><itemref idref="c1"/></spine></package>')
        z.writestr("OEBPS/chapter.xhtml",
                   '<?xml version="1.0"?>\n'
                   '<html xmlns="http://www.w3.org/1999/xhtml">'
                   '<body><p>Hello world</p></body></html>')


class TestSlugify(unittest.TestCase):

    def test_basic(self):
        self.assertEqual(cli._slugify("Effective C.epub"), "effective-c")

    def test_punctuation_collapses(self):
        self.assertEqual(cli._slugify("Foo_Bar! Baz.zip"), "foo-bar-baz")

    def test_empty_fallback(self):
        # no alnum chars -> "book" fallback
        self.assertEqual(cli._slugify("---.epub"), "book")


def _make_book_with_title(bd: str, title: str) -> str:
    """Build a minimal target/ with an OPF carrying a dc:title."""
    os.makedirs(os.path.join(bd, ".work", "target", "OEBPS"))
    os.makedirs(os.path.join(bd, ".work", "target", "META-INF"))
    with open(os.path.join(bd, ".work", "target", "META-INF", "container.xml"), "w") as f:
        f.write('<?xml version="1.0"?><container version="1.0" '
                'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                'media-type="application/oebps-package+xml"/></rootfiles></container>')
    with open(os.path.join(bd, ".work", "target", "OEBPS", "content.opf"), "w") as f:
        f.write('<?xml version="1.0"?>'
                '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata>'
                f'<dc:title>{title}</dc:title>'
                '<dc:creator>John Doe</dc:creator>'
                '</metadata></package>')
    return os.path.join(bd, ".work", "target", "OEBPS", "content.opf")


class TestPathCommand(unittest.TestCase):
    """`path` is how a script finds a book without knowing where the library is."""

    def test_path_prints_the_library_and_a_book_folder(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as td:
            epub = os.path.join(td, "Book.epub")
            _make_minimal_epub(epub)
            original_books = cli.BOOKS
            cli.BOOKS = os.path.join(td, "books")
            try:
                cli.cmd_setup(types.SimpleNamespace(
                    epub=epub, slug="b", source="english", target="italian"))
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    cli.cmd_path(types.SimpleNamespace(slug=None))
                    cli.cmd_path(types.SimpleNamespace(slug="b"))
                library, book = out.getvalue().split()
                self.assertEqual(library, cli.BOOKS)
                self.assertEqual(book, os.path.join(cli.BOOKS, "b"))
            finally:
                cli.BOOKS = original_books

    def test_path_of_an_unknown_book_fails(self):
        with tempfile.TemporaryDirectory() as td:
            original_books = cli.BOOKS
            cli.BOOKS = os.path.join(td, "books")
            try:
                with self.assertRaises(SystemExit):
                    cli.cmd_path(types.SimpleNamespace(slug="nope"))
            finally:
                cli.BOOKS = original_books


class TestRedoCommand(unittest.TestCase):
    """redo drops a file from done + cache so translate redoes it from scratch."""

    def test_redo_clears_done_and_cache(self):
        from ai_epub_translator import state as state_mod
        from ai_epub_translator.cache import Cache
        with tempfile.TemporaryDirectory() as td:
            epub = os.path.join(td, "Book.epub")
            _make_minimal_epub(epub)
            original_books = cli.BOOKS
            cli.BOOKS = os.path.join(td, "books")
            try:
                cli.cmd_setup(types.SimpleNamespace(
                    epub=epub, slug="b", source="english", target="italian"))
                bd = os.path.join(td, "books", "b")
                rel = "OEBPS/chapter.xhtml"
                # simulate a completed (but damaged) file
                st = state_mod.load(bd)
                state_mod.mark_done(st, rel)
                state_mod.save(bd, st)
                with Cache(bd) as c:
                    c.put_file(rel, Cache.hash_text("x"), "<p>abbreviato...</p>", "fail")

                cli.cmd_redo(types.SimpleNamespace(slug="b", files=[rel]))

                st = state_mod.load(bd)
                self.assertNotIn(rel, st["done"])      # queued for re-translation
                with Cache(bd) as c:
                    self.assertIsNone(c.get(rel))      # cache dropped
            finally:
                cli.BOOKS = original_books

    def test_redo_ignores_unknown_file(self):
        with tempfile.TemporaryDirectory() as td:
            epub = os.path.join(td, "Book.epub")
            _make_minimal_epub(epub)
            original_books = cli.BOOKS
            cli.BOOKS = os.path.join(td, "books")
            try:
                cli.cmd_setup(types.SimpleNamespace(
                    epub=epub, slug="b", source="english", target="italian"))
                cli.cmd_redo(types.SimpleNamespace(slug="b", files=["NOPE.html"]))
                # no crash; state untouched
                from ai_epub_translator import state as state_mod
                self.assertEqual(state_mod.load(os.path.join(td, "books", "b"))["done"], [])
            finally:
                cli.BOOKS = original_books


class TestVerifyFix(unittest.TestCase):
    """verify --fix requeues what fails; without the flag it stays read-only."""

    def _book_with_broken_target(self, td):
        """Set up a book whose target/ is damaged (a dropped <em>)."""
        epub = os.path.join(td, "Book.epub")
        _make_minimal_epub(epub)
        cli.cmd_setup(types.SimpleNamespace(
            epub=epub, slug="b", source="english", target="italian"))
        bd = os.path.join(td, "books", "b")
        rel = "OEBPS/chapter.xhtml"
        # original has an <em>; the "translation" drops it -> struct defect
        with open(os.path.join(bd, ".work", "original", rel), "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"'
                    ' lang="en"><body><p>Hello <em>world</em></p></body></html>')
        with open(os.path.join(bd, ".work", "target", rel), "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"'
                    ' lang="it"><body><p>Ciao mondo</p></body></html>')
        from ai_epub_translator import state as state_mod
        st = state_mod.load(bd)
        state_mod.mark_done(st, rel)
        state_mod.save(bd, st)
        return bd, rel

    def test_fix_requeues_failing_file(self):
        from ai_epub_translator import state as state_mod
        with tempfile.TemporaryDirectory() as td:
            original_books = cli.BOOKS
            cli.BOOKS = os.path.join(td, "books")
            try:
                bd, rel = self._book_with_broken_target(td)
                cli.cmd_verify(types.SimpleNamespace(slug="b", file=None, fix=True))
                self.assertNotIn(rel, state_mod.load(bd)["done"])
            finally:
                cli.BOOKS = original_books

    def test_without_fix_state_is_untouched(self):
        from ai_epub_translator import state as state_mod
        with tempfile.TemporaryDirectory() as td:
            original_books = cli.BOOKS
            cli.BOOKS = os.path.join(td, "books")
            try:
                bd, rel = self._book_with_broken_target(td)
                cli.cmd_verify(types.SimpleNamespace(slug="b", file=None, fix=False))
                self.assertIn(rel, state_mod.load(bd)["done"])   # read-only
            finally:
                cli.BOOKS = original_books


class TestCheckAll(unittest.TestCase):
    """check-all reports health per book and exits non-zero on problems."""

    def _setup_book(self, td, translated_ok: bool):
        epub = os.path.join(td, "Book.epub")
        _make_minimal_epub(epub)
        cli.cmd_setup(types.SimpleNamespace(
            epub=epub, slug="b", source="english", target="italian"))
        bd = os.path.join(td, "books", "b")
        rel = "OEBPS/chapter.xhtml"
        with open(os.path.join(bd, ".work", "original", rel), "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"'
                    ' lang="en"><body><p>Hello <em>world</em></p></body></html>')
        body = ('<p>Ciao <em>mondo</em></p>' if translated_ok
                else '<p>Ciao mondo</p>')          # broken: <em> dropped
        with open(os.path.join(bd, ".work", "target", rel), "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"'
                    f' lang="it"><body>{body}</body></html>')
        from ai_epub_translator import state as state_mod
        st = state_mod.load(bd)
        state_mod.mark_done(st, rel)
        state_mod.save(bd, st)
        return bd

    def _args(self):
        return types.SimpleNamespace(slug="b", llm=False, base_url=None, model=None)

    def test_clean_book_returns_zero(self):
        with tempfile.TemporaryDirectory() as td:
            original_books = cli.BOOKS
            cli.BOOKS = os.path.join(td, "books")
            try:
                self._setup_book(td, translated_ok=True)
                self.assertEqual(cli.cmd_check_all(self._args()), 0)
            finally:
                cli.BOOKS = original_books

    def test_broken_book_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as td:
            original_books = cli.BOOKS
            cli.BOOKS = os.path.join(td, "books")
            try:
                self._setup_book(td, translated_ok=False)
                self.assertEqual(cli.cmd_check_all(self._args()), 1)
            finally:
                cli.BOOKS = original_books

    def test_health_counts(self):
        from ai_epub_translator.config import merged_config
        with tempfile.TemporaryDirectory() as td:
            original_books = cli.BOOKS
            cli.BOOKS = os.path.join(td, "books")
            try:
                bd = self._setup_book(td, translated_ok=False)
                rep = cli._book_health(bd, "b", merged_config(bd))
                self.assertEqual(rep["total"], 1)
                self.assertEqual(rep["done"], 1)
                self.assertEqual(rep["passed"], 0)
                self.assertEqual(len(rep["failures"]), 1)
                self.assertEqual(rep["failures"][0]["cause"], "quality")
            finally:
                cli.BOOKS = original_books


class TestResolveBook(unittest.TestCase):
    """`run <arg>` takes a book already set up: a slug or its folder.

    Setting a book up is deliberately a separate step, so the languages and the
    glossary get reviewed before any token is spent.
    """

    def _setup(self, td, name="Book.epub"):
        epub = os.path.join(td, name)
        _make_minimal_epub(epub)
        cli.cmd_setup(types.SimpleNamespace(
            epub=epub, slug=None, source="english", target="italian"))
        return epub

    def test_slug_of_existing_book(self):
        with tempfile.TemporaryDirectory() as td:
            original_books = cli.BOOKS
            cli.BOOKS = os.path.join(td, "books")
            try:
                self._setup(td)
                self.assertEqual(cli._resolve_book("book"), "book")
            finally:
                cli.BOOKS = original_books

    def test_workspace_folder_of_existing_book(self):
        with tempfile.TemporaryDirectory() as td:
            original_books = cli.BOOKS
            cli.BOOKS = os.path.join(td, "books")
            try:
                self._setup(td)
                bd = os.path.join(cli.BOOKS, "book")
                self.assertEqual(cli._resolve_book(bd), "book")
            finally:
                cli.BOOKS = original_books

    def test_epub_is_refused_with_a_setup_hint(self):
        # run must NOT set books up: it points at setup instead
        with tempfile.TemporaryDirectory() as td:
            original_books = cli.BOOKS
            cli.BOOKS = os.path.join(td, "books")
            try:
                epub = os.path.join(td, "Never.epub")
                _make_minimal_epub(epub)
                with self.assertRaises(SystemExit) as cm:
                    cli._resolve_book(epub)
                self.assertIn("setup", str(cm.exception))
                self.assertFalse(os.path.isdir(os.path.join(cli.BOOKS, "never")))
            finally:
                cli.BOOKS = original_books

    def test_unknown_book_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            original_books = cli.BOOKS
            cli.BOOKS = os.path.join(td, "books")
            os.makedirs(cli.BOOKS)
            try:
                with self.assertRaises(SystemExit):
                    cli._resolve_book("does-not-exist")
            finally:
                cli.BOOKS = original_books


class TestLlmGate(unittest.TestCase):
    """The quality gate decides whether the EPUB may be built."""

    def _book(self, td):
        epub = os.path.join(td, "Book.epub")
        _make_minimal_epub(epub)
        cli.cmd_setup(types.SimpleNamespace(
            epub=epub, slug=None, source="english", target="italian"))
        bd = os.path.join(td, "books", "book")
        from ai_epub_translator import state as state_mod
        st = state_mod.load(bd)
        state_mod.mark_done(st, "OEBPS/chapter.xhtml")
        state_mod.save(bd, st)
        return bd

    def _args(self, **over):
        base = dict(min_score=7, base_url=None, model=None, progress=None,
                    no_llm_check=False, passes=1, open=False)
        base.update(over)
        return types.SimpleNamespace(**base)

    def _patch_check(self, scores):
        """Make check_translation return the given scores in order."""
        seq = list(scores)

        def fake(orig, trad, cfg, base_url, model):
            score = seq.pop(0) if seq else 10
            return {"score": score, "comment": "x", "sampled": False,
                    "report": "", "prompt_tokens": 0, "completion_tokens": 0,
                    "elapsed": 0.1}
        return fake

    def test_good_scores_allow_the_epub(self):
        with tempfile.TemporaryDirectory() as td:
            books, check = cli.BOOKS, cli.check_translation
            cli.BOOKS = os.path.join(td, "books")
            cli.check_translation = self._patch_check([9])
            try:
                bd = self._book(td)
                cfg = merged_config(bd)
                ok = cli._llm_gate(bd, "book", cfg, self._args(),
                                   ["OEBPS/chapter.xhtml"])
                self.assertTrue(ok)
            finally:
                cli.BOOKS, cli.check_translation = books, check

    def test_low_score_retranslates_then_passes(self):
        with tempfile.TemporaryDirectory() as td:
            books, check = cli.BOOKS, cli.check_translation
            proc = cli._process_file
            cli.BOOKS = os.path.join(td, "books")
            cli.check_translation = self._patch_check([4, 9])   # bad, then good
            cli._process_file = lambda *a, **k: True            # fake re-translation
            try:
                bd = self._book(td)
                cfg = merged_config(bd)
                ok = cli._llm_gate(bd, "book", cfg, self._args(),
                                   ["OEBPS/chapter.xhtml"])
                self.assertTrue(ok)
            finally:
                cli.BOOKS, cli.check_translation = books, check
                cli._process_file = proc

    def test_persistent_low_score_blocks_the_epub(self):
        with tempfile.TemporaryDirectory() as td:
            books, check = cli.BOOKS, cli.check_translation
            proc = cli._process_file
            cli.BOOKS = os.path.join(td, "books")
            cli.check_translation = self._patch_check([4, 4])   # bad twice
            cli._process_file = lambda *a, **k: True
            try:
                bd = self._book(td)
                cfg = merged_config(bd)
                ok = cli._llm_gate(bd, "book", cfg, self._args(),
                                   ["OEBPS/chapter.xhtml"])
                self.assertFalse(ok)                            # EPUB blocked
            finally:
                cli.BOOKS, cli.check_translation = books, check
                cli._process_file = proc

    def test_llm_outage_does_not_block(self):
        # an unreachable server must not be mistaken for bad quality
        def down(*a, **k):
            raise RuntimeError("LLM unreachable")
        with tempfile.TemporaryDirectory() as td:
            books, check = cli.BOOKS, cli.check_translation
            cli.BOOKS = os.path.join(td, "books")
            cli.check_translation = down
            try:
                bd = self._book(td)
                cfg = merged_config(bd)
                self.assertTrue(cli._llm_gate(bd, "book", cfg, self._args(),
                                              ["OEBPS/chapter.xhtml"]))
            finally:
                cli.BOOKS, cli.check_translation = books, check

    def test_min_score_is_honoured(self):
        with tempfile.TemporaryDirectory() as td:
            books, check = cli.BOOKS, cli.check_translation
            cli.BOOKS = os.path.join(td, "books")
            cli.check_translation = self._patch_check([8])
            try:
                bd = self._book(td)
                cfg = merged_config(bd)
                # 8/10 passes the default 7, and would fail a stricter 9
                ok = cli._llm_gate(bd, "book", cfg, self._args(min_score=7),
                                   ["OEBPS/chapter.xhtml"])
                self.assertTrue(ok)
            finally:
                cli.BOOKS, cli.check_translation = books, check


class TestGlossaryCommand(unittest.TestCase):
    """--add is the only writing path; --suggest/--extract must not write."""

    def _book(self, td):
        epub = os.path.join(td, "Book.epub")
        _make_minimal_epub(epub)
        cli.cmd_setup(types.SimpleNamespace(
            epub=epub, slug="b", source="french", target="italian"))
        return os.path.join(td, "books", "b")

    def _args(self, **over):
        base = dict(slug="b", add=None, suggest=False, extract=False, file=None,
                    yes=False, min_hits=5, base_url=None, model=None)
        base.update(over)
        return types.SimpleNamespace(**base)

    def test_add_writes_the_term(self):
        from ai_epub_translator.config import load_glossary
        with tempfile.TemporaryDirectory() as td:
            original_books = cli.BOOKS
            cli.BOOKS = os.path.join(td, "books")
            try:
                bd = self._book(td)
                cli.cmd_glossary(self._args(add=["exotérisme=essoterismo"]))
                self.assertEqual(load_glossary(bd),
                                 {"exotérisme": "essoterismo"})
            finally:
                cli.BOOKS = original_books

    def test_malformed_add_is_rejected(self):
        from ai_epub_translator.config import load_glossary
        with tempfile.TemporaryDirectory() as td:
            original_books = cli.BOOKS
            cli.BOOKS = os.path.join(td, "books")
            try:
                bd = self._book(td)
                cli.cmd_glossary(self._args(add=["senza uguale"]))
                self.assertEqual(load_glossary(bd), {})   # nothing written
            finally:
                cli.BOOKS = original_books

    def test_suggest_does_not_write(self):
        from ai_epub_translator.config import load_glossary
        with tempfile.TemporaryDirectory() as td:
            original_books = cli.BOOKS
            cli.BOOKS = os.path.join(td, "books")
            try:
                bd = self._book(td)
                cli.cmd_glossary(self._args(suggest=True))
                self.assertEqual(load_glossary(bd), {})   # read-only
            finally:
                cli.BOOKS = original_books

    def test_extract_without_yes_does_not_write(self):
        from ai_epub_translator.config import load_glossary
        with tempfile.TemporaryDirectory() as td:
            original_books = cli.BOOKS
            original_extract = cli.extract_glossary
            cli.BOOKS = os.path.join(td, "books")
            cli.extract_glossary = lambda *a, **k: [
                {"source": "exotérisme", "current": "esoterismo",
                 "correct": "essoterismo", "reason": "opposto"}]
            try:
                bd = self._book(td)
                # mark the file done so extract has something to analyse
                from ai_epub_translator import state as state_mod
                st = state_mod.load(bd)
                state_mod.mark_done(st, "OEBPS/chapter.xhtml")
                state_mod.save(bd, st)
                cli.cmd_glossary(self._args(extract=True))
                self.assertEqual(load_glossary(bd), {})   # candidates only
                # ...with --yes it does write
                cli.cmd_glossary(self._args(extract=True, yes=True))
                self.assertEqual(load_glossary(bd), {"exotérisme": "essoterismo"})
            finally:
                cli.BOOKS = original_books
                cli.extract_glossary = original_extract


class TestMetadataTranslation(unittest.TestCase):
    """The OPF book title/description is translated once into target/."""

    def test_translates_title_once_and_is_idempotent(self):
        calls = []

        def fake_translate_text(text, cfg, base_url, model):
            calls.append(text)
            return "TRAD:" + text

        orig = cli.translate_text
        cli.translate_text = fake_translate_text
        try:
            with tempfile.TemporaryDirectory() as td:
                bd = os.path.join(td, "x")
                os.makedirs(bd)
                opf = _make_book_with_title(bd, "Hello World")
                state = {}
                cli._translate_metadata(bd, "x", {"base_url": "u", "model": "m"}, state)
                with open(opf) as f:
                    content = f.read()
                self.assertIn("<dc:title>TRAD:Hello World</dc:title>", content)
                self.assertIn("<dc:creator>John Doe</dc:creator>", content)  # untouched
                self.assertTrue(state["meta_done"])
                # second call is a no-op (idempotent)
                cli._translate_metadata(bd, "x", {}, state)
                self.assertEqual(len(calls), 1)
        finally:
            cli.translate_text = orig


class TestSetupCommand(unittest.TestCase):
    """End-to-end: setup a workspace from a minimal EPUB (BOOKS redirected)."""

    def test_setup_creates_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            epub = os.path.join(td, "My Book.epub")
            _make_minimal_epub(epub)
            original_books = cli.BOOKS
            cli.BOOKS = os.path.join(td, "books")
            try:
                args = types.SimpleNamespace(
                    epub=epub, slug=None, source="english",
                    target="italian")
                cli.cmd_setup(args)
                bd = os.path.join(td, "books", "my-book")
                self.assertTrue(os.path.isdir(os.path.join(bd, ".work", "target")))
                self.assertTrue(os.path.isdir(os.path.join(bd, ".work", "original")))
                self.assertTrue(os.path.isfile(os.path.join(bd, "book.toml")))
                spine = cli.read_spine(cli.find_opf(os.path.join(bd, ".work", "target")))
                self.assertEqual(len(spine), 1)
            finally:
                cli.BOOKS = original_books

    def test_setup_explicit_slug(self):
        with tempfile.TemporaryDirectory() as td:
            epub = os.path.join(td, "Whatever.epub")
            _make_minimal_epub(epub)
            original_books = cli.BOOKS
            cli.BOOKS = os.path.join(td, "books")
            try:
                args = types.SimpleNamespace(
                    epub=epub, slug="custom-slug", source="french",
                    target="italian")
                cli.cmd_setup(args)
                self.assertTrue(os.path.isdir(
                    os.path.join(td, "books", "custom-slug")))
            finally:
                cli.BOOKS = original_books


def _reply(text):
    """A fake llm.chat that returns ``text`` as the whole model output."""
    def chat(prompt, *a, **k):
        return {"text": text, "prompt_tokens": 5, "completion_tokens": 5,
                "finish_reason": "stop", "elapsed": 0, "attempts": 1}
    return chat


class TestProcessFileGate(unittest.TestCase):
    """End-to-end gate: _process_file segment -> LLM -> splice -> verify -> save,
    with a fake model that answers per segment.
    """

    ORIG = ('<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"'
            ' lang="en"><body><p>Hello <em>world</em></p><p>Second one.</p></body></html>')
    # the two units, as the model should answer them
    GOOD = '<seg id="1">Ciao <g1>mondo</g1></seg>\n<seg id="2">Seconda.</seg>'
    # placeholder dropped in unit 0: rejected, retried, rejected again
    BAD = '<seg id="1">Ciao mondo</seg>\n<seg id="2">Seconda.</seg>'

    def setUp(self):
        self._obooks = cli.BOOKS
        self._tmp = tempfile.TemporaryDirectory()
        cli.BOOKS = os.path.join(self._tmp.name, "books")
        epub = os.path.join(self._tmp.name, "Book.epub")
        _make_minimal_epub(epub)
        cli.cmd_setup(types.SimpleNamespace(
            epub=epub, slug="b", source="english", target="italian"))
        self.bd = os.path.join(self._tmp.name, "books", "b")
        self.rel = "OEBPS/chapter.xhtml"
        with open(os.path.join(self.bd, ".work", "original", self.rel), "w",
                  encoding="utf-8") as f:
            f.write(self.ORIG)

    def tearDown(self):
        cli.BOOKS = self._obooks
        self._tmp.cleanup()

    def _process(self, chat, translate_it=True):
        from ai_epub_translator import llm
        from ai_epub_translator.cache import Cache
        cfg = merged_config(self.bd)
        orig, llm.chat = llm.chat, chat
        try:
            with Cache(self.bd) as cache:
                return cli._process_file(self.bd, "b", self.rel, cfg,
                                         translate_it=translate_it, cache=cache)
        finally:
            llm.chat = orig

    def _target_text(self):
        with open(os.path.join(self.bd, ".work", "target", self.rel), encoding="utf-8") as f:
            return f.read()

    def test_good_translation_is_verified_and_saved(self):
        from ai_epub_translator import state as state_mod
        self.assertTrue(self._process(_reply(self.GOOD)))
        self.assertIn(self.rel, state_mod.load(self.bd)["done"])
        saved = self._target_text()
        self.assertIn("<p>Ciao <em>mondo</em></p><p>Seconda.</p>", saved)
        self.assertIn('lang="it"', saved)

    def test_a_unit_the_model_cannot_get_right_fails_the_file_by_name(self):
        from ai_epub_translator import state as state_mod
        from ai_epub_translator.cache import Cache
        before = self._target_text()
        calls = []

        def chat(prompt, *a, **k):
            calls.append(k.get("temperature"))
            return _reply(self.BAD)(prompt)

        self.assertFalse(self._process(chat))
        st = state_mod.load(self.bd)
        self.assertNotIn(self.rel, st.get("done", []))
        self.assertIn("unit 0: placeholder <g1> </g1> missing", st["failed"][self.rel])
        self.assertEqual(self._target_text(), before)   # target untouched on fail
        # one batch call at the base temperature, then the bounded retries, warmer
        self.assertEqual(calls, [0.15, 0.4, 0.4])
        with Cache(self.bd) as c:
            self.assertEqual(c.unit_status(self.rel), (1, 1))   # unit 1 kept

    def test_a_rejected_unit_is_fixed_on_retry(self):
        answers = iter([self.BAD, '<seg id="1">Ciao <g1>mondo</g1></seg>'])

        def chat(prompt, *a, **k):
            return _reply(next(answers))(prompt)

        self.assertTrue(self._process(chat))
        self.assertIn("<p>Ciao <em>mondo</em></p>", self._target_text())

    def test_cached_units_are_not_asked_again(self):
        from ai_epub_translator import state as state_mod
        from ai_epub_translator.cache import Cache
        oh = Cache.hash_text(self.ORIG)
        with Cache(self.bd) as c:
            c.put_unit(self.rel, 0, oh, "Ciao <em>mondo</em>", "ok")
            c.put_unit(self.rel, 1, oh, "Seconda.", "ok")

        def _boom(*a, **k):
            raise AssertionError("LLM must not be called when the cache has every unit")

        self.assertTrue(self._process(_boom))           # no exception -> not called
        self.assertIn(self.rel, state_mod.load(self.bd)["done"])
        self.assertIn("Ciao", self._target_text())

    def test_completing_a_file_asks_only_for_the_missing_units(self):
        from ai_epub_translator.cache import Cache
        oh = Cache.hash_text(self.ORIG)
        with Cache(self.bd) as c:
            c.put_unit(self.rel, 1, oh, "Seconda.", "ok")
            c.put_file(self.rel, oh, "", "fail")
        prompts = []

        def chat(prompt, *a, **k):
            prompts.append(prompt)
            return _reply('<seg id="1">Ciao <g1>mondo</g1></seg>')(prompt)

        self.assertTrue(self._process(chat, translate_it=False))
        self.assertEqual(len(prompts), 1)
        self.assertIn('<seg id="1">Hello', prompts[0])
        self.assertNotIn("Second one", prompts[0])

    def test_a_saved_file_failing_a_new_glossary_term_re_asks_that_unit_only(self):
        # translated and saved under the old rules...
        self.assertTrue(self._process(_reply(self.GOOD)))
        # ...then a term is pinned that the saved unit 1 violates
        with open(os.path.join(self.bd, "glossary.toml"), "w", encoding="utf-8") as f:
            f.write('[terms]\n"one" = "uno"\n')
        cfg = merged_config(self.bd)
        self.assertTrue(cli._needs_repair(self.bd, cfg, self.rel))
        prompts = []

        def chat(prompt, *a, **k):
            prompts.append(prompt)
            return _reply('<seg id="1">Secondo uno.</seg>')(prompt)

        self.assertTrue(self._process(chat, translate_it=False))
        self.assertEqual(len(prompts), 1)
        self.assertNotIn("Hello", prompts[0])
        self.assertIn("<p>Ciao <em>mondo</em></p><p>Secondo uno.</p>", self._target_text())

    def test_a_pending_file_with_no_units_is_translated_whole(self):
        # a stale file-level row from an older run, target/ still the original:
        # nothing may be "kept" from it — every unit goes to the model
        from ai_epub_translator.cache import Cache
        with Cache(self.bd) as c:
            c.put_file(self.rel, Cache.hash_text(self.ORIG),
                       "<html>old whole-file output</html>", "fail")
        prompts = []

        def chat(prompt, *a, **k):
            prompts.append(prompt)
            return _reply(self.GOOD)(prompt)

        self.assertTrue(self._process(chat, translate_it=False))
        self.assertEqual(len(prompts), 1)
        self.assertIn("Hello", prompts[0])
        self.assertIn("Second one", prompts[0])
        self.assertIn("<p>Ciao <em>mondo</em></p><p>Seconda.</p>", self._target_text())

    def test_a_unit_past_its_budget_is_reported_not_asked_again(self):
        from ai_epub_translator.cache import Cache
        oh = Cache.hash_text(self.ORIG)
        with Cache(self.bd) as c:
            c.put_unit(self.rel, 1, oh, "Seconda.", "ok")
            for _ in range(3):
                c.put_unit(self.rel, 0, oh, None, "fail", "placeholder <g1> </g1> missing")
            c.put_file(self.rel, oh, "", "fail")

        def _boom(*a, **k):
            raise AssertionError("a unit past its budget must not be asked again")

        from ai_epub_translator import state as state_mod
        self.assertFalse(self._process(_boom, translate_it=False))
        self.assertIn("gave up after 3 attempts", state_mod.load(self.bd)["failed"][self.rel])

    def test_unreachable_llm_aborts_the_run(self):
        def _refused(*a, **k):
            raise RuntimeError("connection refused")
        self.assertEqual(self._process(_refused), "abort")


class TestHasIssue(unittest.TestCase):
    """What earns a polish is the judge's note, not the score alone."""

    def test_a_named_defect_above_the_pass_mark_is_flagged(self):
        self.assertTrue(cli._has_issue(9, '"fifty centuries" reso "cinquant\'anni"'))
        self.assertTrue(cli._has_issue(8, "un calco inglese"))

    def test_a_clean_chapter_is_not_flagged(self):
        self.assertFalse(cli._has_issue(10, "faithful"))
        self.assertFalse(cli._has_issue(9, "faithful"))
        self.assertFalse(cli._has_issue(9, "fedele"))          # older logs
        self.assertFalse(cli._has_issue(9, ""))
        self.assertFalse(cli._has_issue(None, "boh"))

    def test_a_perfect_score_is_never_polished(self):
        # nothing to gain, and a rewrite could only lose
        self.assertFalse(cli._has_issue(10, "piccolissimo refuso"))


class TestGatePolish(unittest.TestCase):
    """The judge's note is spent on a targeted polish before any re-translation.

    Re-translating a flagged chapter re-runs the prompt that produced the slip and
    tends to reproduce it; the note says exactly what is wrong, so it must reach
    the model first. The rewrite is kept only when the re-judge scores it higher.
    """

    ORIG = ('<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" '
            'lang="en"><body><p>Moreover, the doctrine is one and the same for '
            'everybody who studies it seriously.</p></body></html>')
    BAD = ('<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" '
           'lang="it"><body><p>Moreover, la dottrina è una e la stessa per '
           'chiunque la studi seriamente.</p></body></html>')
    GOOD_BLOCK = ('<seg id="1">Inoltre, la dottrina è una e la stessa per chiunque la '
                  'studi seriamente.</seg>')

    def _book(self, td):
        bd = os.path.join(td, "books", "b")
        os.makedirs(os.path.join(bd, ".work", "original", "OEBPS"))
        os.makedirs(os.path.join(bd, ".work", "target", "OEBPS"))
        with open(os.path.join(bd, ".work", "original", "OEBPS", "ch.xhtml"), "w") as f:
            f.write(self.ORIG)
        with open(os.path.join(bd, ".work", "target", "OEBPS", "ch.xhtml"), "w") as f:
            f.write(self.BAD)
        with open(os.path.join(bd, "book.toml"), "w") as f:
            f.write('[languages]\nsource = "english"\ntarget = "italian"\n')
        return bd

    def _run(self, td, rejudge, score=8):
        """Run _gate_polish with a scripted re-judge and a model that obeys the note."""
        bd = self._book(td)
        cfg = merged_config(bd)
        seen = {}

        def fake_polish_chat(base_url, model):
            def chat(prompt, max_tokens=None):
                seen["prompt"] = prompt
                return {"text": self.GOOD_BLOCK}
            return chat

        def fake_check(orig, trad, cfg, base_url, model):
            if isinstance(rejudge, Exception):
                raise rejudge
            return rejudge

        row = ("OEBPS/ch.xhtml", score, 'ripetizione di "moreover" in inglese')
        real_chat, real_check = cli._polish_chat, cli.check_translation
        cli._polish_chat, cli.check_translation = fake_polish_chat, fake_check
        try:
            out = cli._gate_polish(bd, "b", cfg, [row], [row], "http://x", "m")
        finally:
            cli._polish_chat, cli.check_translation = real_chat, real_check
        with open(os.path.join(bd, ".work", "target", "OEBPS", "ch.xhtml")) as f:
            return out, f.read(), seen.get("prompt", "")

    def test_note_reaches_the_model_and_a_better_score_is_kept(self):
        with tempfile.TemporaryDirectory() as td:
            out, text, prompt = self._run(td, {"score": 10, "comment": "fedele"})
            self.assertIn('ripetizione di "moreover"', prompt)
            self.assertIn("Inoltre", text)               # the polish was written
            self.assertEqual(out, [("OEBPS/ch.xhtml", 10, "fedele")])

    def test_a_polish_that_does_not_improve_is_discarded(self):
        with tempfile.TemporaryDirectory() as td:
            row = ("OEBPS/ch.xhtml", 8, 'ripetizione di "moreover" in inglese')
            out, text, _p = self._run(td, {"score": 8, "comment": "uguale"})
            self.assertIn("Moreover", text)              # original kept on disk
            self.assertEqual(out, [row])                 # score unchanged

    def test_an_llm_outage_during_the_re_judge_is_reported(self):
        with tempfile.TemporaryDirectory() as td:
            out, _text, _p = self._run(td, RuntimeError("connection refused"))
            self.assertIsNone(out)


class TestGateLeftovers(unittest.TestCase):
    """A source word the judge did not notice is found and fixed anyway.

    The judge reads a chapter whole and scores 10/10 one that still says "una
    quite naturale estensione". This pass finds those deterministically, so it
    must also accept the rewrite on an objective basis: the words are gone, the
    file still verifies, and the score did not drop.
    """

    ORIG = ('<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" '
            'lang="en"><body><p>By a quite natural extension of this meaning, '
            'the word came to designate wisdom itself.</p></body></html>')
    BAD = ('<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" '
           'lang="it"><body><p>Per una quite naturale estensione di questo '
           'significato, la parola giunse a designare la saggezza stessa.'
           '</p></body></html>')
    # The model answers with a bare sentence: the unit of work is a text node,
    # so a reply carrying markup is rejected outright.
    FIXED_SENTENCE = ('Per una del tutto naturale estensione di questo '
                      'significato, la parola giunse a designare la saggezza '
                      'stessa.')

    def _book(self, td):
        bd = os.path.join(td, "books", "b")
        os.makedirs(os.path.join(bd, ".work", "original", "OEBPS"))
        os.makedirs(os.path.join(bd, ".work", "target", "OEBPS"))
        with open(os.path.join(bd, ".work", "original", "OEBPS", "ch.xhtml"), "w") as f:
            f.write(self.ORIG)
        with open(os.path.join(bd, ".work", "target", "OEBPS", "ch.xhtml"), "w") as f:
            f.write(self.BAD)
        with open(os.path.join(bd, "book.toml"), "w") as f:
            f.write('[languages]\nsource = "english"\ntarget = "italian"\n')
        return bd

    def _run(self, td, reply, rejudge):
        bd = self._book(td)
        cfg = merged_config(bd)

        def fake_polish_chat(base_url, model):
            return lambda prompt, max_tokens=None: {"text": reply}

        real_chat, real_check = cli._polish_chat, cli.check_translation
        cli._polish_chat = fake_polish_chat
        cli.check_translation = lambda *a, **k: rejudge
        try:
            out = cli._gate_leftovers(
                bd, "b", cfg, [("OEBPS/ch.xhtml", 10, "fedele")],
                "http://x", "m")
        finally:
            cli._polish_chat, cli.check_translation = real_chat, real_check
        with open(os.path.join(bd, ".work", "target", "OEBPS", "ch.xhtml")) as f:
            return out, f.read()

    def test_a_perfect_chapter_still_gets_its_leftover_translated(self):
        # score 10/10: "the score went up" can never hold, so the objective
        # criterion is what saves this defect from shipping.
        with tempfile.TemporaryDirectory() as td:
            out, text = self._run(td, self.FIXED_SENTENCE,
                                  {"score": 10, "comment": "fedele"})
            self.assertIn("del tutto naturale", text)
            self.assertNotIn("quite", text)
            self.assertIn("</p></body></html>", text)   # markup intact
            self.assertEqual(out, [("OEBPS/ch.xhtml", 10, "fedele")])

    def test_a_rewrite_that_keeps_the_word_is_discarded(self):
        with tempfile.TemporaryDirectory() as td:
            out, text = self._run(td, "Per una quite naturale estensione.",
                                  {"score": 10, "comment": "fedele"})
            self.assertIn("quite", text)              # original left untouched

    def test_a_reply_carrying_markup_is_discarded(self):
        with tempfile.TemporaryDirectory() as td:
            out, text = self._run(td, "<p>" + self.FIXED_SENTENCE + "</p>",
                                  {"score": 10, "comment": "fedele"})
            self.assertIn("quite", text)              # original left untouched

    def test_a_rewrite_that_reads_worse_is_discarded(self):
        with tempfile.TemporaryDirectory() as td:
            out, text = self._run(td, self.FIXED_SENTENCE,
                                  {"score": 6, "comment": "peggiorato"})
            self.assertIn("quite", text)              # original left untouched
            self.assertEqual(out, [("OEBPS/ch.xhtml", 10, "fedele")])

    def test_a_clean_book_is_left_alone(self):
        with tempfile.TemporaryDirectory() as td:
            bd = self._book(td)
            with open(os.path.join(bd, ".work", "target", "OEBPS", "ch.xhtml"), "w") as f:
                f.write(self.BAD.replace("quite naturale", "del tutto naturale"))
            rows = [("OEBPS/ch.xhtml", 10, "fedele")]
            self.assertEqual(                         # no LLM call at all
                cli._gate_leftovers(bd, "b", merged_config(bd), rows,
                                    "http://x", "m"), rows)


if __name__ == "__main__":
    unittest.main()
