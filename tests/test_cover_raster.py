"""Raster-cover regressions, including the nil AppKit context that made blank PNGs."""

from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

from ai_epub_translator.cover_raster import render_cover_png


def _solid_png(width: int, height: int) -> bytes:
    """An original blue test image, not a book cover or third-party fixture."""
    def chunk(kind, data):
        return (struct.pack('>I', len(data)) + kind + data
                + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff))
    raw = (b'\0' + bytes((20, 80, 160)) * width) * height
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


def _corner_rgba(data: bytes) -> tuple:
    """Read the first pixel of an AppKit PNG without adding a test dependency.

    On the first scanline and first pixel, PNG Sub/Up/Average predictors are
    zero. This is enough to catch an entirely transparent bitmap, which can
    otherwise have the right dimensions, valid CRCs and a successful exit code.
    """
    offset, payload, header = 8, b'', None
    while offset < len(data):
        length = struct.unpack('>I', data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        body = data[offset + 8:offset + 8 + length]
        if kind == b'IHDR':
            header = struct.unpack('>IIBBBBB', body)
        if kind == b'IDAT':
            payload += body
        offset += length + 12
    width, height, depth, color, compression, filtering, interlace = header
    assert (depth, color, interlace) == (8, 6, 0), header
    raw = zlib.decompress(payload)
    assert raw[0] in range(5)
    return tuple(raw[1:5])


class TestRasterSelection(unittest.TestCase):
    def test_other_platforms_use_portable_fallback_without_spawning(self):
        with patch('ai_epub_translator.cover_raster.sys.platform', 'linux'), \
             patch('ai_epub_translator.cover_raster.subprocess.run') as run:
            self.assertFalse(render_cover_png(Path('cover.jpg'), Path('out.png'), "AI TRANSLATED"))
        run.assert_not_called()

    def test_no_art_uses_title_cover_fallback(self):
        with patch('ai_epub_translator.cover_raster.subprocess.run') as run:
            self.assertFalse(render_cover_png(None, Path('out.png'), "AI TRANSLATED"))
        run.assert_not_called()

    def test_native_error_is_not_silently_replaced_by_incompatible_svg(self):
        with patch('ai_epub_translator.cover_raster.sys.platform', 'darwin'), \
             patch('ai_epub_translator.cover_raster.subprocess.run') as run:
            run.return_value.returncode = 1
            run.return_value.stderr = "renderer failed"
            with self.assertRaisesRegex(RuntimeError, "renderer failed"):
                render_cover_png(Path('cover.jpg'), Path('out.png'), "Tradotto con l'AI")

    def test_missing_output_is_an_error_even_after_successful_exit(self):
        with tempfile.TemporaryDirectory() as td, \
             patch('ai_epub_translator.cover_raster.sys.platform', 'darwin'), \
             patch('ai_epub_translator.cover_raster.subprocess.run') as run:
            run.return_value.returncode = 0
            with self.assertRaisesRegex(RuntimeError, "did not produce"):
                render_cover_png(Path('cover.jpg'), Path(td) / 'out.png', "Tradotto con l'AI")
            argv = run.call_args.args[0]
            self.assertEqual(argv[-1], "Tradotto con l'AI")
            self.assertNotIn("Tradotto con l'AI", argv[4])   # label is data, not JavaScript

    @unittest.skipUnless(sys.platform == 'darwin', 'uses the macOS built-in AppKit bridge')
    def test_native_renderer_keeps_dimensions_and_draws_nonblank_pixels(self):
        with tempfile.TemporaryDirectory() as td:
            source, target = Path(td) / 'original.png', Path(td) / 'stamped.png'
            source.write_bytes(_solid_png(120, 180))
            self.assertTrue(render_cover_png(source, target, "Tradotto con l'AI"))
            png = target.read_bytes()
            self.assertEqual(struct.unpack('>II', png[16:24]), (120, 180))
            red, green, blue, alpha = _corner_rgba(png)
            self.assertEqual(alpha, 255)
            self.assertGreater(blue, green)
            self.assertGreater(green, red)
            self.assertEqual(source.read_bytes(), _solid_png(120, 180))


if __name__ == '__main__':
    unittest.main()
