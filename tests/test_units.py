"""Tag protection: segmentation, placeholders, validation, reassembly."""

import glob
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_epub_translator import units as U  # noqa: E402
from ai_epub_translator.paths import library_dir  # noqa: E402
from ai_epub_translator.verify import verify_file  # noqa: E402

BOOKS = library_dir()          # $AI_EPUB_TRANSLATOR_BOOKS, else the XDG library
DOC = ('<?xml version="1.0" encoding="utf-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml" lang="en">'
       '<head><title>T</title><style>p {{ color: red; }}</style></head>'
       '<body>{body}</body></html>\n')


def doc(body: str) -> str:
    return DOC.format(body=body)


def visible(body: str, cfg=None) -> list:
    return [u.visible for u in U.segment(doc(body), cfg).translatable]


class TestSegmentation(unittest.TestCase):

    def test_wrapping_layers_go_to_the_skeleton(self):
        body = '<p class="para"><span><span class="italic"><span>Hello world</span></span></span></p>'
        sk = U.segment(doc(body))
        # the <title> is a unit too
        self.assertEqual([u.visible for u in sk.units], ["T", "Hello world"])
        self.assertEqual(sk.units[1].runs, [])

    def test_inline_runs_become_numbered_placeholders(self):
        body = ('<p><span><span class="italic"><span>We call it the</span></span> kairos'
                '<span class="italic"><span>—the moment—which is</span></span> not '
                '<span class="italic"><span>of our choosing.</span></span></span></p>')
        (u,) = [u for u in U.segment(doc(body)).units if u.idx == 1]
        self.assertEqual(u.visible, "<g1>We call it the</g1> kairos<g2>—the moment—which is</g2>"
                                    " not <g3>of our choosing.</g3>")
        self.assertEqual(u.markers, ["<g1>", "</g1>", "<g2>", "</g2>", "<g3>", "</g3>"])
        self.assertEqual(u.runs[0][1], '<span class="italic"><span>')
        self.assertEqual(u.runs[1][1], "</span></span>")

    def test_nested_leading_layer_is_peeled_only_when_it_wraps_everything(self):
        body = ('<p class="x"><span><span class="bold"><span><span class="italic"><span>'
                'The natal chart:</span></span></span></span> The positions.</span></p>')
        self.assertEqual(visible(body)[1], "<g1>The natal chart:</g1> The positions.")

    def test_void_and_mixed_runs_are_lone_placeholders(self):
        body = '<p>—C. G. Jung<br class="c"/><span class="italic"><span>The Self</span></span></p>'
        self.assertEqual(visible(body)[1], "—C. G. Jung<x1/><g2>The Self</g2>")
        body = '<p><em>a</em><strong>b</strong></p>'
        self.assertEqual(visible(body)[1], "<g1>a</g1><g2>b</g2>")
        body = '<p>z <a href="#x"><b>x</b> y</a></p>'
        self.assertEqual(visible(body)[1], "z <g1><g2>x</g2> y</g1>")
        body = '<p>z <a href="#x">x<b>y</b></a></p>'
        self.assertEqual(visible(body)[1], "z <g1>x<g2>y</g2></g1>")
        # a pair wrapping the whole unit is structure: it goes to the skeleton
        body = '<p><a href="#x"><b>x</b> y</a></p>'
        self.assertEqual(visible(body)[1], "<g1>x</g1> y")

    def test_code_and_letterless_inline_are_opaque(self):
        body = ('<p>Use <code class="k">printf</code> here<sup><a href="#n1" id="r1">12</a></sup>'
                ' and <tt class="calibre35"><span>|</span></tt> too.</p>')
        (u,) = [u for u in U.segment(doc(body)).units if u.idx == 1]
        self.assertEqual(u.visible, "Use <x1/> here<x2/> and <x3/> too.")
        self.assertIn("printf", u.runs[0][1])
        self.assertIn('id="r1"', u.runs[1][1])

    def test_whitespace_between_tags_stays_visible(self):
        body = '<p><em>a</em> <em>b</em></p>'
        (u,) = [u for u in U.segment(doc(body)).units if u.idx == 1]
        self.assertEqual(u.visible, "<g1>a</g1> <g2>b</g2>")
        body = '<p><span>x</span> <span>y</span></p>'
        (u,) = [u for u in U.segment(doc(body)).units if u.idx == 1]
        self.assertEqual(u.visible, "<g1>x</g1> <g2>y</g2>")

    def test_edge_whitespace_is_kept_out_of_the_visible_text(self):
        body = '<p>\n   Hello there\n</p>'
        (u,) = [u for u in U.segment(doc(body)).units if u.idx == 1]
        self.assertEqual((u.lead, u.visible, u.trail), ("\n   ", "Hello there", "\n"))

    def test_line_breaks_inside_prose_are_folded(self):
        body = '<p>one\n  two\r\n three</p>'
        self.assertEqual(visible(body)[1], "one two three")

    def test_entities_are_decoded_for_the_model(self):
        body = '<p>it&#8217;s &amp; a&#160;b &lt;x&gt;</p>'
        self.assertEqual(visible(body)[1], "it’s & a b <x>")

    def test_literal_blocks_and_code_classes_produce_no_units(self):
        body = ('<pre>int main()</pre><div class="programlisting">x = 1;</div>'
                '<p class="para">prose</p><script>var a;</script>')
        cfg = {"code_class_hints": ["programlisting"]}
        self.assertEqual(visible(body, cfg), ["T", "prose"])

    def test_letterless_content_is_not_a_unit(self):
        body = '<p>12</p><p>•</p><p class="spaceBreak"><span>&#160;</span></p><p>(0°)</p>'
        self.assertEqual(visible(body), ["T"])

    def test_text_directly_in_a_div_and_around_nested_blocks(self):
        body = '<div>lead text<p>inner</p>tail text</div><li>item<ul><li>sub</li></ul></li>'
        self.assertEqual(visible(body), ["T", "lead text", "inner", "tail text", "item", "sub"])

    def test_inline_open_straddling_a_block_stays_in_the_skeleton(self):
        body = '<div><span class="a">Title<h2>Head</h2>rest</span></div>'
        sk = U.segment(doc(body))
        self.assertEqual([u.visible for u in sk.units], ["T", "Title", "Head", "rest"])
        self.assertEqual(U.reassemble(sk, {}), doc(body))


class TestRoundTrip(unittest.TestCase):

    def test_identity_is_byte_exact(self):
        body = ('<div class="c" id="x"></div><p>a <em>b</em> c&#160;d\n</p>'
                '<pre>  keep\n  this</pre><p><span>e<br/>f</span></p><!-- note -->')
        src = doc(body)
        sk = U.segment(src)
        self.assertEqual(U.reassemble(sk, {}), src)
        inners = {u.idx: U.render(u, u.visible)[0] for u in sk.units}
        rebuilt = U.reassemble(sk, inners)
        ver = verify_file(src, rebuilt, {"dest_code": "en"})
        self.assertTrue(ver["passed"], ver["reasons"])

    @unittest.skipUnless(os.environ.get("AI_EPUB_TRANSLATOR_CORPUS"),
                         "set AI_EPUB_TRANSLATOR_CORPUS=1 to round-trip the real books")
    def test_real_books_round_trip(self):
        """Every original of every book present: skeleton + raw units == source,
        and the visible-text route rebuilds a file the gate accepts."""
        files = glob.glob(os.path.join(BOOKS, "*", ".work", "original", "**", "*.*htm*"),
                          recursive=True)
        if not files:
            self.skipTest(f"no books in {BOOKS}")
        from ai_epub_translator.config import merged_config
        for path in files:
            bd = path.split(os.sep + "original" + os.sep)[0]
            cfg = merged_config(bd)
            with open(path, encoding="utf-8") as f:
                src = f.read()
            sk = U.segment(src, cfg)
            self.assertEqual(U.reassemble(sk, {}), src, path)
            inners = {u.idx: U.render(u, u.visible)[0] for u in sk.units}
            rebuilt = U.reassemble(sk, inners)
            ver = verify_file(src, rebuilt, dict(cfg, glossary={},
                                                 dest_code=cfg["dest_code"]))
            reasons = [r for r in ver["reasons"] if not r.startswith("lang")]
            self.assertEqual(reasons, [], path)


class TestRender(unittest.TestCase):

    def _unit(self):
        body = '<p>a <em>b</em> c <strong>d</strong> e<br/>f</p>'
        (u,) = [u for u in U.segment(doc(body)).units if u.idx == 1]
        self.assertEqual(u.visible, "a <g1>b</g1> c <g2>d</g2> e<x3/>f")
        return u

    def test_markup_is_spliced_back_and_text_escaped(self):
        u = self._unit()
        inner, why = U.render(u, "A <g1>B</g1> C & <g2>D</g2> E<x3/>F < G")
        self.assertEqual(why, "")
        self.assertEqual(inner, "A <em>B</em> C &amp; <strong>D</strong> E<br/>F &lt; G")

    def test_nbsp_and_escaped_markers_are_tolerated(self):
        u = self._unit()
        inner, _ = U.render(u, "A&#160;&lt;g1&gt;B&lt;/g1&gt; C <g2>D</g2> E<x3>F")
        self.assertEqual(inner, "A&#160;<em>B</em> C <strong>D</strong> E<br/>F")

    def test_missing_extra_and_repeated_placeholders_are_named(self):
        u = self._unit()
        self.assertEqual(U.render(u, "A B C <g2>D</g2> E<x3/>F")[1],
                         "placeholder <g1> </g1> missing")
        self.assertEqual(U.render(u, "A <g1>B</g1> C <g2>D</g2> E<x3/>F<x4/>")[1],
                         "placeholder <x4/> not in the original")
        self.assertIn("repeated", U.render(u, "A <g1>B</g1><g1>x</g1> C <g2>D</g2> E<x3/>F")[1])

    def test_reordering_is_rejected_strictly_and_accepted_relaxed_if_it_nests(self):
        u = self._unit()
        swapped = "A <g2>D</g2> C <g1>B</g1> E<x3/>F"
        self.assertEqual(U.render(u, swapped)[1], "placeholders out of their original order")
        inner, why = U.render(u, swapped, strict=False)
        self.assertEqual(why, "")
        self.assertEqual(inner, "A <strong>D</strong> C <em>B</em> E<br/>F")
        crossed = "A <g1>B <g2>C</g1> D</g2> E<x3/>F"
        self.assertNotEqual(U.render(u, crossed, strict=False)[1], "")

    def test_invented_markup_is_rejected_but_source_tags_in_prose_are_not(self):
        u = self._unit()
        self.assertEqual(U.render(u, "A <g1>B</g1> <i>C</i> <g2>D</g2> E<x3/>F")[1],
                         "markup in the answer: <i>")
        body = '<p>Use #include &lt;stdlib.h&gt; here.</p>'
        (v,) = [x for x in U.segment(doc(body)).units if x.idx == 1]
        inner, why = U.render(v, "Usa #include <stdlib.h> qui.")
        self.assertEqual(why, "")
        self.assertEqual(inner, "Usa #include &lt;stdlib.h&gt; qui.")

    def test_glossary_exceptions_skip_a_term_inside_a_named_phrase(self):
        body = '<p>' + ("word " * 20) + 'in Archetypal Psychology and Jungian Thought.</p>'
        (u,) = [x for x in U.segment(doc(body)).units if x.idx == 1]
        cfg = {"glossary": {"archetypal": "archetipico"},
               "glossary_exceptions": {"archetypal": ["Archetypal Psychology"]}}
        self.assertEqual(U.check_content(u, "parola " * 20 + "in Archetypal Psychology and Jungian Thought.", cfg), "")
        body = '<p>' + ("word " * 20) + 'an archetypal force in Archetypal Psychology.</p>'
        (u,) = [x for x in U.segment(doc(body)).units if x.idx == 1]
        self.assertIn("'archetypal'", U.check_content(u, "parola " * 20 + "una forza in Archetypal Psychology.", cfg))

    def test_content_checks(self):
        body = '<p>' + ("word " * 40).strip() + ' and the trine.</p>'
        (u,) = [u for u in U.segment(doc(body)).units if u.idx == 1]
        cfg = {"glossary": {"trine": "trigono"}}
        self.assertEqual(U.check_content(u, "parola " * 40 + "e il trigono.", cfg), "")
        self.assertIn("abbreviated", U.check_content(u, "parola parola.", cfg))
        self.assertIn("ellipsis", U.check_content(u, "parola " * 40 + "e il trigono…", cfg))
        self.assertIn("'trine'", U.check_content(u, "parola " * 40 + "e il trine.", cfg))


class TestAlignment(unittest.TestCase):

    def test_translated_file_aligns_and_units_are_addressable(self):
        body = '<p>one <em>two</em></p><p>three</p>'
        src = doc(body)
        sk = U.segment(src)
        inners = {1: "uno <em>due</em>", 2: "tre"}
        trad = U.reassemble(sk, inners)
        tk = U.segment(trad)
        self.assertTrue(U.aligned(sk, tk))
        self.assertEqual(U.inner_of(tk, tk.units[2]), "tre")
        pos = trad.find("tre")
        self.assertEqual(U.units_at(tk, [(pos, pos + 3)]), {2})
        broken = U.segment(trad.replace("<p>tre</p>", ""))
        self.assertFalse(U.aligned(sk, broken))


if __name__ == "__main__":
    unittest.main()
