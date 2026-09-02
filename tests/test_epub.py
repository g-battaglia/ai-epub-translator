"""Characterization tests for EPUB discovery (ai_epub_translator/epub.py)."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_epub_translator import epub


def _make_minimal_epub(path: str) -> None:
    """Write a tiny but valid EPUB (one spine file) to ``path``."""
    import zipfile
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles>'
                   '</container>')
        z.writestr("OEBPS/content.opf",
                   '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
                   'version="3.0" unique-identifier="b"><metadata xmlns:dc='
                   '"http://purl.org/dc/elements/1.1/">'
                   '<dc:identifier id="b">b</dc:identifier></metadata>'
                   '<manifest><item id="c" href="chapter.xhtml" '
                   'media-type="application/xhtml+xml"/></manifest>'
                   '<spine><itemref idref="c"/></spine></package>')
        z.writestr("OEBPS/chapter.xhtml",
                   '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
                   '<body><p>Hello world</p></body></html>')


def _make_epub(path: str, spine: tuple) -> None:
    """An EPUB whose spine is ``spine`` — repeat an id to exercise the de-duplication."""
    import zipfile
    items = "".join(f'<item id="{i}" href="{i}.xhtml" '
                    'media-type="application/xhtml+xml"/>' for i in sorted(set(spine)))
    refs = "".join(f'<itemref idref="{i}"/>' for i in spine)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles>'
                   '</container>')
        z.writestr("OEBPS/content.opf",
                   '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
                   'version="3.0" unique-identifier="b"><metadata xmlns:dc='
                   '"http://purl.org/dc/elements/1.1/">'
                   '<dc:identifier id="b">b</dc:identifier></metadata>'
                   f'<manifest>{items}</manifest><spine>{refs}</spine></package>')
        for i in sorted(set(spine)):
            z.writestr(f"OEBPS/{i}.xhtml",
                       '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
                       f'<body><p>{i}</p></body></html>')


class TestSpine(unittest.TestCase):

    def test_read_spine_order_and_existence(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "b.epub")
            _make_epub(src, ("c03", "c01", "c02", "c01"))
            target = epub.unpack_epub(src, os.path.join(tmp, "target"))
            files = epub.read_spine(epub.find_opf(target))
            self.assertEqual([os.path.basename(f) for f in files],
                             ["c03.xhtml", "c01.xhtml", "c02.xhtml"])   # spine order, no repeats
            for f in files:
                self.assertTrue(os.path.isfile(f), f"{f} missing")

    def test_localname(self):
        self.assertEqual(epub.localname("{ns}item"), "item")
        self.assertEqual(epub.localname("item"), "item")


class TestSpineResolution(unittest.TestCase):
    """read_spine must percent-decode hrefs, allow entries above the OPF (jacket
    at ../), and reject a crafted traversal outside the EPUB root."""

    def _book(self):
        import tempfile
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "META-INF"))
        os.makedirs(os.path.join(root, "OEBPS"))
        with open(os.path.join(root, "jacket.xhtml"), "w") as f:
            f.write("<html/>")
        with open(os.path.join(root, "OEBPS", "cap 1.xhtml"), "w") as f:
            f.write("<html/>")
        opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf">'
               '<manifest>'
               '<item id="j" href="../jacket.xhtml" media-type="application/xhtml+xml"/>'
               '<item id="c" href="cap%201.xhtml" media-type="application/xhtml+xml"/>'
               '<item id="e" href="../../../../etc/passwd" media-type="application/xhtml+xml"/>'
               '</manifest><spine>'
               '<itemref idref="j"/><itemref idref="c"/><itemref idref="e"/>'
               '</spine></package>')
        opf_path = os.path.join(root, "OEBPS", "content.opf")
        with open(opf_path, "w") as f:
            f.write(opf)
        return opf_path

    def test_percent_encoded_name_resolves(self):
        names = [os.path.basename(p) for p in epub.read_spine(self._book())]
        self.assertIn("cap 1.xhtml", names)          # href was cap%201.xhtml

    def test_entry_above_opf_allowed(self):
        names = [os.path.basename(p) for p in epub.read_spine(self._book())]
        self.assertIn("jacket.xhtml", names)         # href ../jacket.xhtml

    def test_traversal_outside_root_rejected(self):
        paths = epub.read_spine(self._book())
        self.assertFalse(any("passwd" in p for p in paths))


class TestPackEpub(unittest.TestCase):
    """Round-trip: unpack -> pack -> unpack preserves content and validity."""

    def test_pack_roundtrip_and_mimetype_first(self):
        import tempfile
        import zipfile
        with tempfile.TemporaryDirectory() as td:
            epub_path = os.path.join(td, "in.epub")
            _make_minimal_epub(epub_path)
            unpacked = os.path.join(td, "unpacked")
            epub.unpack_epub(epub_path, unpacked)
            # mutate a content file to prove pack reads from target
            with open(os.path.join(unpacked, "OEBPS", "chapter.xhtml"),
                      encoding="utf-8") as f:
                chap = f.read()
            with open(os.path.join(unpacked, "OEBPS", "chapter.xhtml"),
                      "w", encoding="utf-8") as f:
                f.write(chap.replace("Hello world", "Ciao mondo"))
            out = epub.pack_epub(unpacked, os.path.join(td, "out.epub"))
            # mimetype must be the first entry and stored uncompressed
            with zipfile.ZipFile(out) as zf:
                names = zf.namelist()
                self.assertEqual(names[0], "mimetype")
                self.assertEqual(zf.read("mimetype"), b"application/epub+zip")
                info = zf.getinfo("mimetype")
                self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
                # mutated content survived the round-trip
                self.assertIn("Ciao mondo", zf.read("OEBPS/chapter.xhtml").decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
