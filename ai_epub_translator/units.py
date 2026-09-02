"""Tag protection: the model translates prose, never markup.

Every failure this harness has ever repaired was the model failing to reproduce
markup: a ``</span>`` forgotten, a ``</p>`` closed mid-sentence, a ``<span>``
invented, a quote left open. Measured on Cosmos and Psyche: 41 structural
failures, 23 tag-count failures, 16 malformed files, zero failures of meaning —
and a 1,700-line repair module that patched those slips one by one and, on the
four files that never converged, made the wreck worse (a block appended before
``</body>``, an orphaned tail, a paragraph left in English).

So the markup is taken out of the model's hands before the call, not repaired
after it:

* the file is cut into **units** — a unit is a maximal run of inline content
  between two block boundaries (the docstring of :mod:`xhtml` already names it
  as the natural atom of translation); everything else is the *skeleton*, copied
  verbatim;
* inside a unit every run of inline tags becomes a **placeholder** —
  ``<g1>…</g1>`` for a pair that opens and closes in the unit, ``<x2/>`` for a
  lone run — so ``<span class="italic"><span>kairos</span></span>`` reaches the
  model as ``<g1>kairos</g1>``; measured on eleven real books, half of all units
  then carry no placeholder at all;
* the model's answer is validated (every placeholder present once, in order) and
  the original markup is spliced back at the placeholder positions.

By construction the result has the original's tags, attributes, ids, hrefs,
page-break markers and code, so :func:`verify.verify_file` passes; it still
runs, as the safety net. If it ever fails, that is a bug here, not something to
send back to the model.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from . import xhtml as X
from .xhtml import CLOSE, OPEN, TEXT, VOID_K, is_block, tokenize

# Block boundaries beyond xhtml.BLOCK. Measured on the corpus: <style>/<script>
# (168 files) would otherwise hand the model a page of CSS as prose; <pre> (565)
# and <svg> hold nothing to translate; <dl> only wraps dt/dd.
EXTRA_BLOCKS = {"pre", "style", "script", "dl", "svg", "image", "math", "figure"}
# Blocks whose whole content is copied verbatim, never translated.
LITERAL_BLOCKS = {"pre", "style", "script", "svg", "image", "math"}
# Inline elements whose content is code and is hidden from the model as one
# opaque placeholder. csapp marks its 7,619 operators with <tt class="calibre35">,
# a class no code hint lists — the tag name is the reliable signal.
OPAQUE_INLINE = {"code", "tt", "kbd", "samp", "var"}

# Anything with a letter is prose; "12", "•", "(0°)" is not.
_LETTER_RE = re.compile(r"[^\W\d_]")
# A placeholder in the model's answer. The names are valid XML (lxml accepts
# <g1>, rejects <1>), unique per marker so a lost </g2> can be named exactly,
# and the letter keeps them apart from the numerals astrological prose is full of.
_MARKER_RE = re.compile(r"<\s*(/?)\s*([gx])(\d+)\s*(/?)\s*>")
# Anything tag-shaped left in an answer once the placeholders are out.
_TAGLIKE_RE = re.compile(r"</?[A-Za-z][^<>]*>")
# Whitespace that includes a line break: one-line-per-file calibre output has
# none, wrapped sources have them mid-sentence; the model gets a single space.
_NL_WS_RE = re.compile(r"[ \t]*(?:\r\n|\r|\n)[ \t\r\n]*")


@dataclass
class Unit:
    """One translatable run of inline content, with its markup taken out."""

    idx: int
    start: int                     # span of the inner content in the source
    end: int                       # (after the wrapping layers were peeled)
    visible: str = ""              # what the model sees: decoded text + markers
    runs: list = field(default_factory=list)   # [(marker, raw_markup)] in order
    lead: str = ""                 # whitespace at the edges, restored verbatim
    trail: str = ""
    literal: bool = False          # nothing to translate: copied as it is

    @property
    def markers(self) -> list:
        return [m for m, _raw in self.runs]

    @property
    def raw(self) -> str:
        """The unit exactly as the source has it (inner span, edges included)."""
        return self._source[self.start:self.end]

    @property
    def plain(self) -> str:
        """The model-visible prose without the placeholders."""
        return plain_text(self.visible)


@dataclass
class Skeleton:
    """A file split into literal parts and units; ``reassemble`` puts it back."""

    source: str
    parts: list = field(default_factory=list)   # ("lit", str) | ("unit", idx)
    units: list = field(default_factory=list)

    @property
    def translatable(self) -> list:
        return [u for u in self.units if not u.literal]


def plain_text(visible: str) -> str:
    """Strip the placeholders from a model-visible string."""
    return _MARKER_RE.sub("", visible)


def _has_letters(s: str) -> bool:
    return bool(_LETTER_RE.search(s))


def _is_ws_text(t) -> bool:
    return t.kind == TEXT and not t.raw.strip()


def _is_tag(t) -> bool:
    return t.kind in (OPEN, CLOSE, VOID_K)


def _classes(tok) -> list:
    return dict(tok.attrs).get("class", "").split()


def _is_code_tag(tok, hints: list) -> bool:
    return tok.name in OPAQUE_INLINE or any(c in hints for c in _classes(tok))


def _boundary(tok) -> bool:
    """A token that ends the current unit."""
    if tok.kind in (OPEN, CLOSE, VOID_K):
        return is_block(tok.name) or tok.name in EXTRA_BLOCKS
    return tok.kind in (X.COMMENT, X.DECL, X.PI, X.CDATA)


def _match_pairs(toks: list, lo: int, hi: int) -> dict:
    """``{open_idx: close_idx}`` for the inline elements of ``toks[lo:hi]``.

    Tolerant: a close that matches nothing is ignored; an open that is never
    closed stays unmatched. Both happen legitimately at unit edges when an
    inline element straddles a block (``<span>…<h2>`` exists in this corpus).
    """
    pairs, stack = {}, []
    for i in range(lo, hi):
        t = toks[i]
        if t.kind == OPEN and not t.self_close:
            stack.append(i)
        elif t.kind == CLOSE:
            for k in range(len(stack) - 1, -1, -1):
                if toks[stack[k]].name == t.name:
                    pairs[stack[k]] = i
                    del stack[k:]
                    break
    return pairs


def _decode(text: str) -> str:
    """Source text as the model should read it: entities resolved, breaks folded."""
    return _NL_WS_RE.sub(" ", html.unescape(text))


def _encode(text: str) -> str:
    """Model text back into XHTML: the three markup characters and the nbsp."""
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace(" ", "&#160;"))


def segment(source: str, cfg: dict = None) -> Skeleton:
    """Cut ``source`` into a skeleton and its units (see the module docstring)."""
    hints = (cfg or {}).get("code_class_hints") or []
    toks = tokenize(source)
    sk = Skeleton(source=source)
    lit_buf: list = []
    block_stack: list = []          # (name, literal?) of the open blocks
    run_lo = None                   # first token index of the unit in progress

    def literal_context() -> bool:
        return any(lit for _n, lit in block_stack)

    def flush_unit(lo, hi):
        if lo is None or lo >= hi:
            return
        unit = _make_unit(source, toks, lo, hi, len(sk.units), hints)
        if unit is None or unit.literal or literal_context():
            lit_buf.append(source[toks[lo].start:toks[hi - 1].end])
            return
        # the peeled wrapping layers stay in the skeleton, around the unit
        lit_buf.append(source[toks[lo].start:unit.start])
        sk.parts.append(("lit", "".join(lit_buf)))
        lit_buf.clear()
        unit._source = source
        sk.units.append(unit)
        sk.parts.append(("unit", unit.idx))
        lit_buf.append(source[unit.end:toks[hi - 1].end])

    for i, t in enumerate(toks):
        if not _boundary(t):
            if run_lo is None:
                run_lo = i
            continue
        flush_unit(run_lo, i)
        run_lo = None
        lit_buf.append(t.raw)
        if t.kind == OPEN and not t.self_close:
            lit = (t.name in LITERAL_BLOCKS
                   or any(c in hints for c in _classes(t)))
            block_stack.append((t.name, lit))
        elif t.kind == CLOSE:
            for k in range(len(block_stack) - 1, -1, -1):
                if block_stack[k][0] == t.name:
                    del block_stack[k:]
                    break
    flush_unit(run_lo, len(toks))
    if lit_buf:
        sk.parts.append(("lit", "".join(lit_buf)))
    return sk


def _make_unit(source, toks, lo, hi, idx, hints):
    """Build the unit for ``toks[lo:hi]``, or ``None`` when there is no prose."""
    if not _has_letters("".join(t.raw for t in toks[lo:hi] if t.kind == TEXT)):
        return None
    pairs = _match_pairs(toks, lo, hi)

    # opaque elements: code, and any inline whose text has no letters (a
    # footnote's <sup><a>12</a></sup>) — one placeholder hides the whole thing
    opaque_end = {}                 # open_idx -> close_idx
    i = lo
    while i < hi:
        t = toks[i]
        j = pairs.get(i)
        if t.kind == OPEN and j is not None:
            inner_text = "".join(x.raw for x in toks[i + 1:j] if x.kind == TEXT)
            if _is_code_tag(t, hints) or not _has_letters(inner_text):
                opaque_end[i] = j
                i = j + 1
                continue
        i += 1

    # Peel the edges. A layer that wraps the whole unit (an open that is the
    # first tag and closes at the last) is structure; so is anything lone at an
    # edge — an inline element straddling a block (<span>…<h2> exists in this
    # corpus), a leading <br/>, a footnote marker. Both go to the skeleton, and
    # what remains is prose with the formatting that lives inside it.
    tag_idx = [k for k in range(lo, hi) if _is_tag(toks[k])]
    a, b = lo, hi                                   # inner token range

    def _text_between(x, y):
        return any(toks[k].kind == TEXT and toks[k].raw.strip()
                   for k in range(x, y))

    def _lone(k):
        t = toks[k]
        if k in opaque_end:
            return True
        if t.kind == OPEN and not t.self_close:
            return pairs.get(k) is None
        if t.kind == CLOSE:
            return k not in pairs.values()
        return True                                 # void / self-closed

    while tag_idx:
        first, last = tag_idx[0], tag_idx[-1]
        if not _text_between(a, first):
            if len(tag_idx) >= 2 and pairs.get(first) == last \
                    and first not in opaque_end and not _text_between(last + 1, b):
                a, b = first + 1, last
                tag_idx = tag_idx[1:-1]
                continue
            if _lone(first):
                end = opaque_end.get(first, first)
                a = end + 1
                tag_idx = [k for k in tag_idx if k > end]
                continue
        if not _text_between(last + 1, b) and _lone(last) \
                and not any(o <= last <= c for o, c in opaque_end.items()
                            if o != last):
            b = last
            tag_idx = tag_idx[:-1]
            continue
        break
    if a >= b or not _has_letters("".join(t.raw for t in toks[a:b]
                                          if t.kind == TEXT)):
        return None

    unit = Unit(idx=idx, start=toks[a].start, end=toks[b - 1].end)
    # edge whitespace
    inner = source[unit.start:unit.end]
    stripped = inner.lstrip()
    unit.lead = inner[:len(inner) - len(stripped)]
    stripped = inner.rstrip()
    unit.trail = inner[len(stripped):]

    # Runs. First by kind (open / close / lone), over adjacent tag tokens; then
    # each open is keyed by the run its close lives in, so that an open-run
    # whose tags close in two different places splits into two pairs —
    # <a><b>x</b> y</a> reaches the model as <g1><g2>x</g2> y</g1> — while the
    # common <span class="italic"><span>…</span></span> stays one pair.
    spans = {}                                      # tok idx -> end idx (opaque)
    kind_run, run_n, prev_end, prev_kind = {}, -1, -2, None
    k = a
    while k < b:
        t = toks[k]
        if k in opaque_end:
            kind = "lone"
            last = opaque_end[k]
        elif not _is_tag(t):
            k += 1
            continue
        elif t.kind == OPEN and not t.self_close:
            kind, last = "open", k
        elif t.kind == CLOSE:
            kind, last = "close", k
        else:
            kind, last = "lone", k
        if not (prev_end == k - 1 and prev_kind == kind):
            run_n += 1
        kind_run[k] = (kind, run_n)
        spans[k] = last
        prev_end, prev_kind = last, kind
        k = last + 1
    key = {}
    for k, (kind, r) in kind_run.items():
        if kind == "open" and pairs.get(k) in kind_run:
            key[k] = ("open", r, kind_run[pairs[k]][1])
        elif kind == "close":
            o = next((o for o, c in pairs.items() if c == k), None)
            key[k] = ("close", kind_run[o][1], r) if o in kind_run else ("lone",)
        else:
            key[k] = ("lone",)
    runs = []                                       # [start_tok, end_tok, key]
    for k in sorted(kind_run):
        if runs and runs[-1][1] == k - 1 and runs[-1][2] == key[k]:
            runs[-1][1] = spans[k]
        else:
            runs.append([k, spans[k], key[k]])
    by_key: dict = {}
    for r_i, r in enumerate(runs):
        by_key.setdefault(r[2], []).append(r_i)
    partner = {}
    for (kind, *rest), idxs in by_key.items():
        if kind != "open" or len(idxs) != 1:
            continue
        closes = by_key.get(("close", *rest), [])
        if len(closes) == 1 and closes[0] > idxs[0]:
            partner[idxs[0]] = closes[0]
            partner[closes[0]] = idxs[0]

    # markers + the visible text
    n = 0
    marker_of = {}
    for r_i in range(len(runs)):
        if r_i in marker_of:
            continue
        n += 1
        if r_i in partner:
            marker_of[r_i] = f"<g{n}>"
            marker_of[partner[r_i]] = f"</g{n}>"
        else:
            marker_of[r_i] = f"<x{n}/>"
    out, k = [], a
    for r_i, r in enumerate(runs):
        if k < r[0]:
            out.append(_decode(source[toks[k].start:toks[r[0]].start]))
        raw = source[toks[r[0]].start:toks[r[1]].end]
        unit.runs.append((marker_of[r_i], raw))
        out.append(marker_of[r_i])
        k = r[1] + 1
    if k < b:
        out.append(_decode(source[toks[k].start:toks[b - 1].end]))
    unit.visible = "".join(out).strip()
    return unit


# --- validation + splice ------------------------------------------------------

def _normalize_markers(text: str) -> list:
    """``[(marker, text_before)]`` from a model answer, tolerantly.

    ``<x3>`` and ``<x3></x3>`` count as ``<x3/>``; ``&lt;g1&gt;`` was already
    unescaped by the caller. The last text piece is appended with marker ``""``.
    """
    out, pos = [], 0
    for m in _MARKER_RE.finditer(text):
        slash, kind, num, self_close = m.groups()
        if kind == "x":
            if slash:
                continue                            # a </x3> closes nothing: drop
            marker = f"<x{num}/>"
        else:
            marker = f"</g{num}>" if slash else f"<g{num}>"
        out.append((marker, text[pos:m.start()]))
        pos = m.end()
    out.append(("", text[pos:]))
    return out


def _nests(markers: list) -> bool:
    """True if the g-pairs open and close in a well-nested order."""
    stack = []
    for m in markers:
        if m.startswith("</"):
            if not stack or stack[-1] != m[2:-1]:
                return False
            stack.pop()
        elif m.startswith("<g"):
            stack.append(m[1:-1])
    return not stack


def render(unit: Unit, answer: str, strict: bool = True) -> tuple:
    """Splice the original markup back into the model's answer for ``unit``.

    Returns ``(inner_xhtml, reason)``: ``inner_xhtml`` is ``None`` when the
    answer is rejected, and ``reason`` names the exact defect — the thing to
    tell the model on the next attempt. ``strict`` demands the placeholders in
    their original order; the relaxed check (used on a last attempt) accepts a
    reordering that still nests, the same trade :mod:`structdiff` makes for an
    inline element that moved with the word it marks.
    """
    expected = unit.markers
    got = _normalize_markers(html.unescape(answer.strip()))
    seen = [m for m, _t in got if m]
    if seen != expected:
        missing = [m for m in expected if m not in seen]
        extra = [m for m in seen if m not in expected]
        if missing:
            return None, f"placeholder {' '.join(missing)} missing"
        if extra:
            return None, f"placeholder {' '.join(extra)} not in the original"
        if sorted(seen) != sorted(expected):
            dup = sorted({m for m in seen if seen.count(m) > 1})
            return None, f"placeholder {' '.join(dup)} repeated"
        if strict or not _nests(seen):
            return None, "placeholders out of their original order"
    # a tag the model invented (<i>, <br>) would be escaped and shipped as
    # visible "<i>" in the book; one the source prose itself carries ("#include
    # <stdlib.h>") is legitimate text and stays
    for _marker, text in got:
        for tag in _TAGLIKE_RE.findall(text):
            if tag not in unit.plain:
                return None, f"markup in the answer: {tag}"
    raw_of = dict(unit.runs)
    out = []
    for marker, text in got:
        out.append(_encode(text))
        if marker:
            out.append(raw_of[marker])
    return "".join(out), ""


def reassemble(sk: Skeleton, inners: dict) -> str:
    """Rebuild the file: the skeleton verbatim, ``inners[idx]`` for each unit.

    A unit without an entry is emitted as the source has it, so a partial
    translation is still a complete, well-formed file.
    """
    out = []
    for kind, val in sk.parts:
        if kind == "lit":
            out.append(val)
            continue
        u = sk.units[val]
        inner = inners.get(u.idx)
        out.append(u.raw if inner is None else u.lead + inner + u.trail)
    return "".join(out)


def inner_of(sk: Skeleton, u: Unit) -> str:
    """The inner content of ``u`` as the (translated) file has it, edges stripped."""
    return sk.source[u.start + len(u.lead):u.end - len(u.trail)]


def aligned(orig: Skeleton, trad: Skeleton) -> bool:
    """True when the two files have the same units with the same placeholders.

    Holds for any file that passed the structural gate (349/350 measured; the
    one exception is an inline element the gate accepted as moved). Then unit
    *i* of the translation is unit *i* of the original, and a single unit can be
    re-translated and spliced without touching the rest.
    """
    if len(orig.units) != len(trad.units):
        return False
    return all(a.markers == b.markers for a, b in zip(orig.units, trad.units))


def units_at(sk: Skeleton, spans: list) -> set:
    """Indices of the units that intersect any ``(start, end)`` char span."""
    out = set()
    for u in sk.units:
        for s, e in spans:
            if s < u.end and e > u.start:
                out.add(u.idx)
    return out


def check_content(unit: Unit, answer: str, cfg: dict) -> str:
    """Prose-level checks on an accepted answer: length, ellipsis, glossary.

    Returns "" when fine, else the reason. Reuses the per-block rules the
    structural diff applies to whole files, on the text the model actually saw
    (opaque content hidden on both sides).
    """
    from .structdiff import _ELLIPSIS_RE, _glossary_defects
    o_text = unit.plain.strip()
    t_text = plain_text(html.unescape(answer)).strip()
    ratio = float(cfg.get("block_text_ratio", 0.7))
    min_len = int(cfg.get("block_text_min", 80))
    if len(o_text) >= min_len and len(t_text) < ratio * len(o_text):
        return (f"text abbreviated: {len(t_text)} of {len(o_text)} chars "
                f"({round(100 * len(t_text) / max(len(o_text), 1))}%)")
    if len(_ELLIPSIS_RE.findall(t_text)) > len(_ELLIPSIS_RE.findall(o_text)):
        return "an ellipsis was added — text likely omitted"
    missing = _glossary_defects(o_text, t_text, cfg.get("glossary") or {},
                                cfg.get("glossary_exceptions"))
    if missing:
        return "; ".join(f"term '{s}' not rendered as '{d}'" for s, d in missing)
    return ""


def translate_paragraph(block: str, cfg: dict, chat_fn=None) -> tuple:
    """Translate one XHTML fragment (a paragraph) through the unit protocol.

    The book-setup skill measures a candidate glossary term by translating one
    real paragraph five times; this is the entry point it uses. Returns
    ``(translated_fragment, reasons)`` — ``reasons`` lists the units the model
    could not get right, so an empty list means every unit validated.
    """
    from . import llm
    chat_fn = chat_fn or llm.default_chat(cfg)
    sk = segment(block, cfg)
    res = llm.translate_units(sk.translatable, cfg, chat_fn)
    inners = {idx: inner for idx, (inner, _why) in res.items() if inner is not None}
    reasons = [f"unit {idx}: {why}" for idx, (inner, why) in res.items()
               if inner is None]
    return reassemble(sk, inners), reasons
