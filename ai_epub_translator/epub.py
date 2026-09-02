"""EPUB OPF/spine discovery and archive unpacking. No external dependencies."""

from __future__ import annotations

import os
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile


def localname(tag: str) -> str:
    """Strip an XML namespace from a tag name (``{ns}el`` -> ``el``)."""
    return tag.rsplit("}", 1)[-1]


def unpack_epub(epub_path: str, dest: str) -> str:
    """Extract an EPUB/zip archive into ``dest`` (created if needed)."""
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(epub_path) as zf:
        zf.extractall(dest)
    return dest


def pack_epub(target_dir: str, epub_path: str) -> str:
    """Pack an unpacked EPUB directory back into a valid ``.epub``.

    The ``mimetype`` entry is written first and uncompressed (ZIP_STORED), as the
    EPUB spec requires for format sniffing; every other entry is deflated. Existing
    ``mimetype`` files in ``target_dir`` are ignored in favour of the canonical one.
    Returns the absolute path of the written EPUB.
    """
    epub_path = os.path.abspath(epub_path)
    os.makedirs(os.path.dirname(epub_path) or ".", exist_ok=True)
    # collect every file under target_dir, relative arc names with '/'
    entries = []
    for root, dirs, fnames in os.walk(target_dir):
        dirs.sort()
        for fn in sorted(fnames):
            full = os.path.join(root, fn)
            arc = os.path.relpath(full, target_dir).replace(os.sep, "/")
            if arc != "mimetype":
                entries.append((full, arc))
    with zipfile.ZipFile(epub_path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip",
                    compress_type=zipfile.ZIP_STORED)
        for full, arc in entries:
            zf.write(full, arc, compress_type=zipfile.ZIP_DEFLATED)
    return epub_path


def find_opf(target: str) -> str:
    """Locate the OPF (.opf) file of an unpacked EPUB via ``META-INF/container.xml``."""
    container = os.path.join(target, "META-INF", "container.xml")
    for el in ET.parse(container).iter():
        if localname(el.tag) == "rootfile":
            fp = el.get("full-path")
            if fp:
                return os.path.join(target, fp)
    raise SystemExit(f"No .opf found in {container}")


def read_spine(opf_path: str) -> list:
    """Absolute paths of the XHTML files in spine (reading) order, each one once."""
    tree = ET.parse(opf_path)
    manifest, spine = {}, []
    opf_dir = os.path.dirname(opf_path)
    for el in tree.iter():
        t = localname(el.tag)
        if t == "item":
            mid, href, media = el.get("id"), el.get("href"), el.get("media-type", "")
            if mid and href and "xhtml" in media:
                manifest[mid] = href
        elif t == "itemref":
            idref = el.get("idref")
            if idref:
                spine.append(idref)
    # Traversal boundary: the EPUB root (where META-INF lives), NOT opf_dir —
    # legitimate spine entries sit above the OPF (a jacket at ../jacket.xhtml is
    # normal). This still rejects a crafted ../../../etc/passwd escape.
    root_real = os.path.realpath(_epub_root(opf_dir))
    files, seen = [], set()
    for idref in spine:
        href = manifest.get(idref)
        if not href:
            continue
        # OPF hrefs are URIs: percent-decode (a chapter named "cap 1.xhtml" is
        # written "cap%201.xhtml") and drop any fragment, or the file resolves to
        # a path that does not exist on disk and the chapter is silently dropped —
        # read_spine is the only source of the chapter list, so the book would
        # then report itself complete while missing that chapter.
        rel = urllib.parse.unquote(href.split("#", 1)[0])
        path = os.path.normpath(os.path.join(opf_dir, rel))
        try:
            inside = os.path.commonpath([os.path.realpath(path), root_real]) == root_real
        except ValueError:
            inside = False
        # A repeated itemref is invalid EPUB, but it does occur; translating the
        # same file twice would send an already-translated chapter back to the
        # model, so the second occurrence is dropped.
        if inside and os.path.isfile(path) and path not in seen:
            seen.add(path)
            files.append(path)
    return files


def _epub_root(opf_dir: str) -> str:
    """The unpacked EPUB root — the nearest ancestor holding ``META-INF`` (the
    container marker). Falls back to the OPF's parent, then the OPF dir itself."""
    d = opf_dir
    for _ in range(5):
        if os.path.isdir(os.path.join(d, "META-INF")):
            return d
        parent = os.path.dirname(d)
        if not parent or parent == d:
            break
        d = parent
    return os.path.dirname(opf_dir) or opf_dir
