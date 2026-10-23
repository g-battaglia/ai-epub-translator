"""Visible AI attribution added at export, never to the recovery workspace.

Packaging uses a private copy: generated pages must not enter the source-aligned
spine, cache or verification loop. The original artwork and every reading-order
entry remain intact; an attributed cover is prepended and a technical page is
appended. macOS exports a native PNG for Apple Books thumbnails; other platforms
use a self-contained SVG. Neither path adds a third-party dependency.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import html
import os
from pathlib import Path
import re
import shutil
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET

from . import __version__
from .config import lang_code
from .cover_raster import render_cover_png
from .epub import find_opf, localname, pack_epub
from .xhtml import CLOSE, OPEN, VOID_K, tokenize

OPF_NS = "http://www.idpf.org/2007/opf"
LABEL = "AI TRANSLATED"
_LABELS = {
    "en": LABEL, "it": "Tradotto con l'AI", "fr": "Traduit par l'IA",
    "de": "KI-übersetzt", "es": "Traducido con IA", "pt": "Traduzido com IA",
    "nl": "Vertaald met AI", "sv": "AI-översatt", "no": "Oversatt med KI",
    "da": "Oversat med AI", "fi": "Käännetty tekoälyllä", "pl": "Przetłumaczono przez AI",
    "cs": "Přeloženo pomocí AI", "sk": "Preložené pomocou AI",
    "sl": "Prevedeno z umetno inteligenco", "hr": "Prevedeno uz AI",
    "sr": "Преведено помоћу ВИ", "bg": "Преведено с ИИ",
    "hu": "MI-vel fordítva", "ro": "Tradus cu AI", "ru": "Переведено ИИ",
    "uk": "Перекладено ШІ", "el": "Μετάφραση με AI",
    "tr": "Yapay zekâ ile çevrildi", "ca": "Traduït amb IA", "gl": "Traducido con IA",
    "eu": "AA bidez itzulita", "la": "Intelligentia artificiali translatum",
    "ar": "مترجم بالذكاء الاصطناعي", "he": "תורגם באמצעות AI",
    "fa": "ترجمه‌شده با هوش مصنوعی", "hi": "AI द्वारा अनुवादित",
    "zh": "AI 翻译", "ja": "AI翻訳", "ko": "AI 번역",
    "vi": "Được dịch bằng AI", "th": "แปลด้วย AI", "id": "Diterjemahkan dengan AI",
    "ms": "Diterjemahkan dengan AI", "eo": "Tradukita per AI",
}


def translation_label(cfg: dict) -> str:
    """Localize the visible stamp deterministically, never through the model."""
    code = str(cfg.get("dest_code") or lang_code(cfg.get("target_lang") or "english"))
    code = code.lower().split("-", 1)[0]
    if code not in _LABELS:
        raise ValueError("No localized AI translation stamp for language: " + code)
    return _LABELS[code]


def _local_file(root: Path, parent: Path, href: str) -> Path:
    """Never follow a manifest URI or symlink outside the staged EPUB."""
    uri = urllib.parse.urlsplit(href)
    if uri.scheme or uri.netloc:
        raise ValueError("An EPUB cover must be a local resource")
    path = (parent / urllib.parse.unquote(uri.path)).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        raise ValueError("EPUB cover escapes the book directory") from None
    if not path.is_file():
        raise ValueError("The EPUB cover resource is missing")
    return path


def _cover_art(root: Path, opf_path: Path, package: ET.Element) -> tuple:
    """Prefer the declared cover, not an arbitrary image from the book."""
    manifest = package.find(f"{{{OPF_NS}}}manifest")
    items = list(manifest) if manifest is not None else []
    by_id = {item.get("id"): item for item in items}
    cover = next((item for item in items
                  if "cover-image" in item.get("properties", "").split()), None)
    if cover is None:
        for el in package.iter():
            if localname(el.tag) == "meta" and el.get("name") == "cover":
                cover = by_id.get(el.get("content"))
                if cover is not None:
                    break
    if cover is None:
        # Some EPUBs declare only a cover document in the guide. Look inside
        # that document rather than guessing from image filenames.
        refs = [el for el in package.iter()
                if localname(el.tag) == "reference" and el.get("type") == "cover"]
        for ref in refs:
            page = _local_file(root, opf_path.parent, ref.get("href", ""))
            for el in ET.parse(page).iter():
                href = None
                if localname(el.tag) == "img":
                    href = el.get("src")
                elif localname(el.tag) == "image":
                    href = el.get("{http://www.w3.org/1999/xlink}href") or el.get("href")
                if href:
                    image = _local_file(root, page.parent, href)
                    for item in items:
                        if not item.get("media-type", "").startswith("image/"):
                            continue
                        if _local_file(root, opf_path.parent, item.get("href", "")) == image:
                            cover = item
                            break
                    if cover is not None:
                        break
            if cover is not None:
                break
    if cover is None:
        return None, None
    media = cover.get("media-type", "")
    if media not in {"image/jpeg", "image/png", "image/gif", "image/svg+xml", "image/webp"}:
        raise ValueError("Unsupported cover image type: " + media)
    return _local_file(root, opf_path.parent, cover.get("href", "")), media


def _cover_svg(art: Path, media: str, title: str, label: str = LABEL) -> str:
    """Overlay a conspicuous stamp without reserving a band or shrinking the art."""
    image = ""
    if art is not None:
        data = base64.b64encode(art.read_bytes()).decode("ascii")
        image = (f'<image x="0" y="0" width="1200" height="1800" '
                 f'preserveAspectRatio="xMidYMid meet" '
                 f'xlink:href="data:{media};base64,{data}"/>')
    else:
        # A book without artwork still gets a real, clearly attributed cover.
        words = title.split()
        lines, line = [], ""
        for word in words:
            if len(line) + len(word) > 28 and line:
                lines.append(line)
                line = ""
            line = (line + " " + word).strip()
        if line:
            lines.append(line)
        image = '<text x="600" y="500" text-anchor="middle" font-size="54">'
        image += "".join(f'<tspan x="600" dy="76">{html.escape(line)}</tspan>'
                         for line in lines)
        image += '</text>'
    escaped_label = html.escape(label)
    # Long localized labels must stay inside the frame instead of clipping.
    fit = ' textLength="940" lengthAdjust="spacingAndGlyphs"' if len(label) > 18 else ""
    return (f'<?xml version="1.0" encoding="utf-8"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="1200" height="1800" viewBox="0 0 1200 1800">'
            f'<title>{escaped_label}</title><desc>{html.escape(title)}</desc>'
            f'<rect width="1200" height="1800" fill="white"/>{image}'
            # The stamp is painted after the full-size image, not laid out beside
            # it. A translucent backing keeps it readable on any source artwork.
            f'<g id="ai-translation-stamp" transform="rotate(-12 600 620)">'
            f'<rect x="90" y="510" width="1020" height="210" rx="8" '
            f'fill="white" fill-opacity="0.88" stroke="#b91c1c" stroke-width="12"/>'
            f'<rect x="108" y="528" width="984" height="174" rx="3" '
            f'fill="none" stroke="#b91c1c" stroke-width="3"/>'
            f'<text x="600" y="650" text-anchor="middle" fill="#b91c1c" '
            f'font-family="sans-serif" font-size="92" font-weight="bold"{fit}>'
            f'{escaped_label}</text></g></svg>')


def _mark_cover_references(root: Path, opf_path: Path, package: ET.Element,
                           art: Path, marked: Path) -> None:
    """Older cover pages must not display the unmarked artwork a second time.

    Only references to the declared cover image change; text, other artwork and
    document structure remain byte-identical. Both HTML img and inline SVG
    image references occur in real EPUB cover documents.
    """
    if art is None:
        return
    for item in package.findall(f".//{{{OPF_NS}}}item"):
        if item.get("media-type") != "application/xhtml+xml":
            continue
        page = _local_file(root, opf_path.parent, item.get("href", ""))
        with page.open(encoding="utf-8", newline="") as f:
            source = f.read()
        changes = []
        for token in tokenize(source):
            if token.kind not in {OPEN, VOID_K} or token.name.split(":")[-1] not in {"img", "image"}:
                continue
            attrs = dict(token.attrs)
            attribute = next((name for name in ("src", "xlink:href", "href")
                              if attrs.get(name)), None)
            if attribute is None:
                continue
            uri = urllib.parse.urlsplit(html.unescape(attrs[attribute]))
            if uri.scheme or uri.netloc:
                continue
            referenced = (page.parent / urllib.parse.unquote(uri.path)).resolve()
            if referenced != art.resolve():
                continue
            href = urllib.parse.quote(os.path.relpath(marked.resolve(), page.parent).replace(os.sep, "/"))
            changes.append((token.start, token.end, _attr(token.raw, attribute, href)))
        for start, end, raw in reversed(changes):
            source = source[:start] + raw + source[end:]
        if changes:
            ET.fromstring(source.encode("utf-8"))
            with page.open("w", encoding="utf-8", newline="") as f:
                f.write(source)


def _page(title: str, body: str, language: str) -> str:
    return (f'<?xml version="1.0" encoding="utf-8"?>\n'
            f'<html xmlns="http://www.w3.org/1999/xhtml" '
            f'xml:lang="{html.escape(language, quote=True)}">'
            f'<head><title>{html.escape(title)}</title>'
            f'<style type="text/css">body {{ margin: 6%; line-height: 1.5; }} '
            f'dt {{ font-weight: bold; }} dd {{ margin-bottom: 0.7em; }} '
            f'img {{ max-width: 100%; height: auto; }}</style></head>'
            f'<body>{body}</body></html>')


def _technical_page(cfg: dict, records: list, count: int) -> str:
    """Do not mistake current configuration for historical model provenance."""
    models = sorted({str(r["model"]) for r in records if r.get("model")})
    rows = [
        ("Translation method", "Automatic AI translation, chapter by chapter; "
         "prose units with protected markup placeholders and resumable caching."),
        ("Source language", str(cfg.get("source_lang") or "Not recorded")),
        ("Target language", str(cfg.get("target_lang") or "Not recorded")),
        ("Recorded translation model(s)", ", ".join(models) if models else
         "Not recorded in the historical translation logs."),
        ("Model configured at export", str(cfg.get("model") or "Not configured")),
        ("Model provenance limits", "Older cached units may predate model logging. "
         "The export configuration alone does not prove which model translated them."),
        ("Tool", "AI EPUB Translator " + __version__),
        ("Exported at (UTC)", datetime.now(timezone.utc).isoformat(timespec="seconds")),
        ("Original reading-order entries", str(count)),
        ("Translation settings at export", f"Batch budget: {cfg.get('batch_chars', 'not recorded')} "
         f"characters; unit retry budget: {cfg.get('unit_retries', 'not recorded')}."),
        ("Verification design", "Deterministic checks compare markup, attributes, "
         "links, language and text-length heuristics. These are safeguards, not "
         "proof of complete semantic fidelity. This page does not certify a "
         "human review or a successful model-based quality assessment."),
    ]
    body = (f'<h1>{html.escape(translation_label(cfg))}</h1><h2>Translation details</h2>'
            '<p>This edition was translated using artificial intelligence. '
            'It may contain mistakes, omissions or terminology inconsistencies. '
            'Consult the original when accuracy matters. The original author '
            'is not the author of this machine translation.</p><dl>')
    body += "".join(f'<dt>{html.escape(k)}</dt><dd>{html.escape(v)}</dd>'
                    for k, v in rows)
    body += '</dl><p>The original artwork and book content have been retained; '
    body += 'the attributed cover and this technical page were added at export.</p>'
    # Technical provenance is deliberately English for every edition: it is
    # generated deterministically, not submitted to the translation model.
    return _page("AI translation details", body, "en")


def _attr(raw: str, name: str, value: str) -> str:
    pattern = rf'''(\b{re.escape(name)}\s*=\s*)(["'])(.*?)\2'''
    changed, count = re.subn(pattern, lambda m: m[1] + '"' + html.escape(value, quote=True) + '"',
                             raw, count=1, flags=re.S)
    if not count:
        raise ValueError("Missing attribute in EPUB package: " + name)
    return changed


def _package_edits(source: str, directory: str, ids: dict, epub3: bool,
                   cover_name: str, cover_media: str) -> str:
    """Splice OPF additions by offsets; never reserialize the original XML."""
    item = lambda key, filename, media, extra="": (
        f'<item xmlns="{OPF_NS}" id="{ids[key]}" '
        f'href="{directory}/{filename}" media-type="{media}"{extra}/>')
    additions = (
        item("image", cover_name, cover_media, ' properties="cover-image"' if epub3 else "")
        + item("cover", "cover.xhtml", "application/xhtml+xml")
        + item("details", "translation.xhtml", "application/xhtml+xml"))
    ref = lambda key: f'<itemref xmlns="{OPF_NS}" idref="{ids[key]}"/>'
    changes, seen = [], set()
    for token in tokenize(source):
        name = token.name.split(":")[-1]
        attrs = dict(token.attrs)
        if token.kind == CLOSE and name in {"metadata", "manifest", "spine"}:
            content = {"metadata": f'<meta xmlns="{OPF_NS}" name="cover" content="{ids["image"]}"/>',
                       "manifest": additions, "spine": ref("details")}[name]
            changes.append((token.start, token.start, content))
            seen.add(name)
        elif token.kind == OPEN and name == "spine":
            changes.append((token.end, token.end, ref("cover")))
        if name == "meta" and attrs.get("name") == "cover" and token.kind != CLOSE:
            # Replace the old pointer with one authoritative cover declaration.
            changes.append((token.start, token.end, ""))
        elif name == "item" and "cover-image" in attrs.get("properties", "").split():
            properties = " ".join(p for p in attrs["properties"].split() if p != "cover-image")
            if properties:
                raw = _attr(token.raw, "properties", properties)
            else:
                raw = re.sub(r'''\s+properties\s*=\s*(["']).*?\1''', "", token.raw, flags=re.S)
            changes.append((token.start, token.end, raw))
        elif name == "reference" and attrs.get("type") == "cover" and token.kind != CLOSE:
            changes.append((token.start, token.end,
                            _attr(token.raw, "href", directory + "/cover.xhtml")))
    if seen != {"metadata", "manifest", "spine"}:
        raise ValueError("EPUB package needs non-empty metadata, manifest and spine")
    for start, end, replacement in sorted(changes, reverse=True):
        source = source[:start] + replacement + source[end:]
    ET.fromstring(source.encode("utf-8"))
    return source


def add_disclosure(target: str, cfg: dict, records: list = None) -> None:
    """Add visible attribution to a disposable, unpacked EPUB staging copy."""
    root = Path(target)
    opf_path = Path(find_opf(target))
    with opf_path.open(encoding="utf-8", newline="") as f:
        source = f.read()
    package = ET.fromstring(source.encode("utf-8"))
    if package.find(f"{{{OPF_NS}}}spine") is None:
        raise ValueError("EPUB package has no spine")
    title = " ".join(package.findtext(".//{http://purl.org/dc/elements/1.1/}title", "").split())
    art, media = _cover_art(root, opf_path, package)
    # Never overwrite a publisher resource, even if it happens to use our name.
    directory = "ai-translation"
    suffix = 1
    while (opf_path.parent / directory).exists():
        directory = f"ai-translation-{suffix}"
        suffix += 1
    used = {el.get("id") for el in package.iter() if el.get("id")}
    ids = {}
    for key in ("image", "cover", "details"):
        candidate = "ai-translation-" + key
        while candidate in used:
            candidate += "-new"
        ids[key] = candidate
        used.add(candidate)
    folder = opf_path.parent / directory
    folder.mkdir()
    label = translation_label(cfg)
    if render_cover_png(art, folder / "cover.png", label):
        cover_name, cover_media = "cover.png", "image/png"
    else:
        cover_name, cover_media = "cover.svg", "image/svg+xml"
        (folder / cover_name).write_text(_cover_svg(art, media, title, label), encoding="utf-8")
    _mark_cover_references(root, opf_path, package, art, folder / cover_name)
    language = cfg.get("dest_code") or lang_code(cfg.get("target_lang") or "english")
    (folder / "cover.xhtml").write_text(_page(
        label, f'<div style="text-align:center"><img src="{cover_name}" '
        f'alt="{html.escape(label, quote=True)}"/></div>', language), encoding="utf-8")
    spine = package.find(f"{{{OPF_NS}}}spine")
    (folder / "translation.xhtml").write_text(
        _technical_page(cfg, records or [], len(spine)), encoding="utf-8")
    new_opf = _package_edits(source, directory, ids,
                             package.get("version", "2").startswith("3"), cover_name, cover_media)
    with opf_path.open("w", encoding="utf-8", newline="") as f:
        f.write(new_opf)


def pack_translation(target: str, output: str, cfg: dict, records: list = None) -> str:
    """Export atomically, with attribution, without changing verified chapters."""
    output = os.path.abspath(output)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ai-epub-export-") as temporary:
        staged = os.path.join(temporary, "book")
        shutil.copytree(target, staged)
        add_disclosure(staged, cfg, records)
        # A failed export must not destroy an existing, readable EPUB.
        fd, pending = tempfile.mkstemp(prefix=".ai-epub-", suffix=".epub",
                                      dir=os.path.dirname(output))
        os.close(fd)
        try:
            pack_epub(staged, pending)
            os.replace(pending, output)
        finally:
            if os.path.exists(pending):
                os.unlink(pending)
    return output
