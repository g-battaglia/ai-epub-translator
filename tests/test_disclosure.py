"""Export attribution must be visible without changing a translated chapter."""

import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

from ai_epub_translator.disclosure import LABEL, OPF_NS, pack_translation
from ai_epub_translator.epub import find_opf, read_spine, unpack_epub
from tests.test_epub import _make_minimal_epub

CFG = {"source_lang": "english", "target_lang": "italian", "dest_code": "it",
       "model": "test-model", "batch_chars": 16000, "unit_retries": 2,
       "base_url": "https://user:secret@example.invalid/v1", "api_key": "private"}
# A public-domain one-pixel PNG: no book or copyrighted artwork in this fixture.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aP9sAAAAASUVORK5CYII=")


def snapshot(folder):
    return {str(p.relative_to(folder)): p.read_bytes()
            for p in Path(folder).rglob("*") if p.is_file()}


class TestDisclosure(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / "in.epub"
        _make_minimal_epub(str(source))
        self.target = self.root / "target"
        unpack_epub(str(source), str(self.target))
        self.opf = Path(find_opf(str(self.target)))
        self.output = self.root / "out.epub"

    def cover(self, version="2.0", prefixed=False):
        text = self.opf.read_text()
        text = text.replace('version="3.0"', f'version="{version}"')
        text = text.replace('</metadata>', '<meta name="cover" content="art"/></metadata>')
        properties = ' properties="cover-image remote-resources"' if version == "3.0" else ""
        text = text.replace('<manifest>', '<manifest><item id="art" href="art%20work.png" '
                            f'media-type="image/png"{properties}/>')
        text = text.replace('</package>', '<guide><reference type="cover" '
                            'title="Cover" href="chapter.xhtml"/></guide></package>')
        if prefixed:
            import re
            text = text.replace(f'xmlns="{OPF_NS}"', f'xmlns:o="{OPF_NS}"')
            text = re.sub(r'<(/?)(package|metadata|manifest|spine|item|itemref|meta|guide|reference)\b',
                          r'<\1o:\2', text)
        self.opf.write_bytes(text.replace('\n', '\r\n').encode())
        (self.opf.parent / "art work.png").write_bytes(PNG)

    def exported(self, records=None):
        pack_translation(str(self.target), str(self.output), CFG, records)
        folder = self.root / "exported"
        unpack_epub(str(self.output), str(folder))
        return folder, ET.parse(find_opf(str(folder))).getroot()

    def test_adds_real_marked_cover_and_final_page_preserving_every_source_byte(self):
        self.cover()
        before = snapshot(self.target)
        folder, root = self.exported([{"model": "used-model"}])
        self.assertEqual(snapshot(self.target), before)
        spine = read_spine(find_opf(str(folder)))
        self.assertEqual([Path(p).name for p in spine],
                         ["cover.xhtml", "chapter.xhtml", "translation.xhtml"])
        self.assertEqual(Path(spine[1]).read_bytes(), before["OEBPS/chapter.xhtml"])
        self.assertIn(LABEL, Path(spine[0]).read_text())
        svg = Path(spine[0]).with_name("cover.svg")
        svg_root = ET.parse(svg).getroot()
        text = svg_root.find('{http://www.w3.org/2000/svg}text')
        self.assertEqual(text.text, LABEL)
        self.assertGreaterEqual(int(text.get("font-size")), 90)
        image = svg_root.find('{http://www.w3.org/2000/svg}image')
        data = image.get('{http://www.w3.org/1999/xlink}href').split(',', 1)[1]
        self.assertEqual(base64.b64decode(data), PNG)
        technical = Path(spine[-1]).read_text()
        for expected in (LABEL, "used-model", "test-model", "english", "italian",
                         "UTC", "16000", "not certify a"):
            self.assertIn(expected, technical)
        for secret in ("secret", "private", "example.invalid"):
            self.assertNotIn(secret, technical)
        ET.parse(spine[-1])
        self.assertEqual((folder / "OEBPS/art work.png").read_bytes(), PNG)
        cover = root.find(f'.//{{{OPF_NS}}}meta[@name="cover"]')
        item = root.find(f'.//{{{OPF_NS}}}item[@id="{cover.get("content")}"]')
        self.assertEqual(item.get("media-type"), "image/svg+xml")
        guide = root.find(f'.//{{{OPF_NS}}}reference[@type="cover"]')
        self.assertEqual(guide.get("href"), "ai-translation/cover.xhtml")

    def test_original_cover_pages_also_use_marked_image_without_changing_their_text(self):
        self.cover()
        page = self.opf.parent / "chapter.xhtml"
        source = ('<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                  '<p>Retain every word.</p><img src="art%20work.png"/>'
                  '<svg xmlns="http://www.w3.org/2000/svg" '
                  'xmlns:xlink="http://www.w3.org/1999/xlink">'
                  '<image xlink:href="art%20work.png"/></svg></body></html>')
        page.write_text(source)
        folder, _ = self.exported()
        exported = (folder / "OEBPS/chapter.xhtml").read_text()
        self.assertEqual(exported, source.replace("art%20work.png", "ai-translation/cover.svg"))
        self.assertEqual(page.read_text(), source)

    def test_guide_only_cover_discovers_original_artwork(self):
        self.cover()
        self.opf.write_text(self.opf.read_text().replace('<meta name="cover" content="art"/>', ''))
        page = self.opf.parent / "chapter.xhtml"
        page.write_text('<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                        '<img src="art%20work.png"/></body></html>')
        folder, _ = self.exported()
        svg = (folder / "OEBPS/ai-translation/cover.svg").read_text()
        self.assertIn(base64.b64encode(PNG).decode(), svg)

    def test_epub3_cover_property_moves_and_other_properties_survive(self):
        self.cover(version="3.0")
        _, root = self.exported()
        items = root.findall(f'.//{{{OPF_NS}}}item')
        covers = [el for el in items if "cover-image" in el.get("properties", "").split()]
        self.assertEqual(len(covers), 1)
        self.assertEqual(covers[0].get("href"), "ai-translation/cover.svg")
        old = root.find(f'.//{{{OPF_NS}}}item[@id="art"]')
        self.assertEqual(old.get("properties"), "remote-resources")

    def test_namespaced_opf_and_crlf_survive_splicing(self):
        self.cover(prefixed=True)
        before = self.opf.read_bytes()
        folder, _ = self.exported()
        result = Path(find_opf(str(folder))).read_bytes()
        # Existing entries are not reserialized; prefix, entities and quotes survive.
        self.assertIn(b'<o:itemref idref="c"/>', result)
        self.assertIn(b'<dc:identifier id="b">b</dc:identifier>', result)
        self.assertEqual(result.count(b'\r\n'), before.count(b'\r\n'))

    def test_no_cover_gets_a_marked_title_cover_without_guessing_art(self):
        folder, _ = self.exported()
        svg = folder / "OEBPS/ai-translation/cover.svg"
        self.assertIn(LABEL, svg.read_text())
        technical = folder / "OEBPS/ai-translation/translation.xhtml"
        self.assertIn("Not recorded in the historical", technical.read_text())
        self.assertIn("Model configured at export", technical.read_text())

    def test_repeat_exports_do_not_duplicate_pages_or_mutate_workspace(self):
        self.cover()
        before = snapshot(self.target)
        self.exported()
        folder, _ = self.exported()
        self.assertEqual(len(read_spine(find_opf(str(folder)))), 3)
        self.assertEqual(snapshot(self.target), before)

    def test_resource_and_id_collisions_do_not_overwrite_publisher_content(self):
        self.cover()
        (self.opf.parent / "ai-translation").mkdir()
        (self.opf.parent / "ai-translation/publisher.txt").write_text("retained")
        text = self.opf.read_text().replace('id="art"', 'id="ai-translation-image"')
        text = text.replace('content="art"', 'content="ai-translation-image"')
        self.opf.write_text(text)
        folder, root = self.exported()
        self.assertEqual((folder / "OEBPS/ai-translation/publisher.txt").read_text(), "retained")
        self.assertTrue((folder / "OEBPS/ai-translation-1/cover.svg").exists())
        ids = [el.get("id") for el in root.iter() if el.get("id")]
        self.assertEqual(len(ids), len(set(ids)))

    def test_cover_traversal_fails_without_destroying_previous_export(self):
        self.cover()
        self.opf.write_text(self.opf.read_text().replace('art%20work.png', '../../outside.png'))
        (self.root / "outside.png").write_bytes(PNG)
        self.output.write_bytes(b"previous valid edition")
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.exported()
        self.assertEqual(self.output.read_bytes(), b"previous valid edition")

    def test_pack_failure_preserves_existing_export(self):
        self.output.write_bytes(b"previous edition")
        with patch('ai_epub_translator.disclosure.pack_epub', side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.exported()
        self.assertEqual(self.output.read_bytes(), b"previous edition")
        self.assertEqual(list(self.root.glob('.ai-epub-*')), [])

    def test_zip_mimetype_still_first_and_uncompressed(self):
        self.exported()
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(archive.namelist()[0], "mimetype")
            self.assertEqual(archive.getinfo("mimetype").compress_type, zipfile.ZIP_STORED)
            self.assertIsNone(archive.testzip())


if __name__ == "__main__":
    unittest.main()
