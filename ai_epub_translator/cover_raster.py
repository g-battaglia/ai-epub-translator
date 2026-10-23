"""Native raster covers for Apple Books, using only tools shipped with macOS.

An SVG can render perfectly inside a book while Apple Books ignores it as the
library thumbnail and invents a generic cover. A PNG containing both the original
artwork and the stamp avoids that split. AppKit is invoked through macOS's built-in
JavaScript/Objective-C bridge, so no Pillow, browser or image library is installed.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


# Arguments are passed separately, never interpolated into JavaScript or a shell.
# AppKit's coordinate system starts at the bottom, unlike the SVG fallback.
_APPKIT_SCRIPT = r"""
ObjC.import('AppKit');
ObjC.import('Foundation');
function run(argv) {
    var image = $.NSImage.alloc.initWithContentsOfFile(argv[0]);
    if (!image || image.isNil()) throw new Error('Cannot load cover artwork');
    var sourceBitmap = $.NSBitmapImageRep.imageRepWithContentsOfFile(argv[0]);
    var width = sourceBitmap.isNil() ? Math.ceil(image.size.width) : sourceBitmap.pixelsWide;
    var height = sourceBitmap.isNil() ? Math.ceil(image.size.height) : sourceBitmap.pixelsHigh;
    if (width < 1 || height < 1) throw new Error('Cover has invalid dimensions');
    var bitmap = $.NSBitmapImageRep.alloc
        .initWithBitmapDataPlanesPixelsWidePixelsHighBitsPerSampleSamplesPerPixelHasAlphaIsPlanarColorSpaceNameBytesPerRowBitsPerPixel(
            null, width, height, 8, 4, true, false, $.NSDeviceRGBColorSpace, 0, 0);
    if (!bitmap || bitmap.isNil()) throw new Error('Cannot allocate cover bitmap');
    $.NSGraphicsContext.saveGraphicsState;
    try {
        var context = $.NSGraphicsContext.graphicsContextWithBitmapImageRep(bitmap);
        // A JavaScript property assignment does NOT call AppKit's class setter:
        // it leaves currentContext nil and produces a completely blank PNG.
        $.NSGraphicsContext.setCurrentContext(context);
        image.drawInRectFromRectOperationFraction(
            $.NSMakeRect(0, 0, width, height), $.NSZeroRect, $.NSCompositingOperationCopy, 1);
        var transform = $.NSAffineTransform.transform;
        transform.translateXByYBy(width / 2, height * (1 - 620 / 1800));
        transform.rotateByDegrees(12);
        transform.scaleBy(width / 1200);
        transform.concat;
        var red = $.NSColor.colorWithSRGBRedGreenBlueAlpha(185 / 255, 28 / 255, 28 / 255, 1);
        $.NSColor.colorWithSRGBRedGreenBlueAlpha(1, 1, 1, 0.88).setFill;
        var outer = $.NSBezierPath.bezierPathWithRoundedRectXRadiusYRadius(
            $.NSMakeRect(-510, -105, 1020, 210), 8, 8);
        outer.fill;
        red.setStroke;
        outer.setLineWidth(12);
        outer.stroke;
        var inner = $.NSBezierPath.bezierPathWithRoundedRectXRadiusYRadius(
            $.NSMakeRect(-492, -87, 984, 174), 3, 3);
        inner.setLineWidth(3);
        inner.stroke;
        var attributes = $.NSMutableDictionary.alloc.init;
        attributes.setObjectForKey($.NSFont.boldSystemFontOfSize(92), $.NSFontAttributeName);
        attributes.setObjectForKey(red, $.NSForegroundColorAttributeName);
        var text = $(argv[2]);
        var size = text.sizeWithAttributes(attributes);
        if (size.width > 940) {
            attributes.setObjectForKey($.NSFont.boldSystemFontOfSize(92 * 940 / size.width), $.NSFontAttributeName);
            size = text.sizeWithAttributes(attributes);
        }
        text.drawAtPointWithAttributes($.NSMakePoint(-size.width / 2, -size.height / 2), attributes);
        context.flushGraphics;
    } finally {
        $.NSGraphicsContext.restoreGraphicsState;
    }
    var data = bitmap.representationUsingTypeProperties($.NSPNGFileType, $.NSDictionary.dictionary);
    if (!data || data.isNil() || !data.writeToFileAtomically(argv[1], true)) {
        throw new Error('Cannot write stamped PNG cover');
    }
}
"""


def render_cover_png(art: Path, destination: Path, label: str) -> bool:
    """Rasterize the stamp on macOS; return False for the portable SVG fallback.

    A native rendering failure is explicit: silently publishing another SVG on
    macOS would recreate the very thumbnail failure this function prevents.
    """
    if sys.platform != "darwin" or art is None:
        return False
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", "-e", _APPKIT_SCRIPT,
             str(art), str(destination), label],
            capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Native cover rendering failed") from exc
    if result.returncode:
        raise RuntimeError("Native cover rendering failed: " + result.stderr.strip()[:500])
    if not destination.is_file() or not destination.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("Native cover renderer did not produce a PNG")
    return True
