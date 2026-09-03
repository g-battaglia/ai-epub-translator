"""Structural diff between an original XHTML file and its translation.

Goal
----
A correct translation changes **only the prose** and the ``lang``/``xml:lang`` of
``<html>``. Tags, their order, and every attribute (id, class, href, src,
epub:type, …) must be preserved verbatim. This module verifies that invariant and,
when it is violated, **localizes the violation** to a specific block so that
:mod:`repair` can fix just that spot instead of re-translating the whole chapter.

Approach
--------
Both sides are tokenized with the tolerant lexer (:mod:`xhtml`), which works even
when the translation is malformed. A structural *signature* collapses text content
(the prose legitimately differs) and keeps only tag kind + name, so a
``difflib`` alignment pinpoints exactly where tags were added, dropped or swapped.
Tag pairs that align are then compared by attribute (``lang`` excluded) to catch
attributes the model translated or dropped.

The output is an :class:`Analysis` of :class:`Defect` objects, each carrying the
character spans (original and translated) and the enclosing block.
"""

from __future__ import annotations

import difflib
import html
import re
import unicodedata
from dataclasses import dataclass, field

from . import xhtml as X
from .xhtml import CLOSE, OPEN, VOID_K, tokenize

# Defect kinds (consumed by repair).
PROLOGUE = "prologue"          # pre-<html> content differs (xml decl / doctype)
MISSING_TAG = "missing_tag"    # a tag present in orig is absent in trad
EXTRA_TAG = "extra_tag"        # a tag in trad has no counterpart in orig
WRONG_TAG = "wrong_tag"        # tags differ at the same position
ATTR = "attr"                  # an aligned element's attributes differ
MISSING_BLOCK = "missing_block"  # a whole prose block is absent (truncation)
PARSE_ERROR = "parse_error"    # trad is not well-formed XML
SHORT_TEXT = "short_text"      # a block's prose was abbreviated (content lost)
ELLIPSIS = "ellipsis"          # the model added "…"/"..." — text likely omitted
GLOSSARY = "glossary"          # a pinned term was not rendered as required

# Blocks whose prose is compared for content loss. Structural containers (div,
# table, body…) are excluded: their text is the concatenation of inner blocks and
# would double-count.
PROSE_BLOCKS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td", "th", "dt", "dd",
    "caption", "figcaption", "blockquote", "title",
}

# Every ellipsis spelling counts as one marker: the model legitimately converts
# between them, so only an *increase* in the total is suspicious. The spaced form
# ". . ." must count too — old typesetting is full of it (11 times in the Rudhyar
# book), and matching only the compact form made the model's normal tightening of
# ". . ." to "..." look like an ellipsis it had added to cover omitted text: a
# defect no correct translation could clear.
_ELLIPSIS_RE = re.compile(r"…|\.(?:\s*\.){2,}")


@dataclass
class Defect:
    """A localized structural violation, with original/translated spans."""

    kind: str
    detail: str
    orig_start: int = -1
    orig_end: int = -1
    trad_start: int = -1
    trad_end: int = -1
    block_orig: tuple = ()     # (start, end) enclosing block on original side
    block_trad: tuple = ()     # (start, end) enclosing region on translated side


@dataclass
class Analysis:
    defects: list = field(default_factory=list)
    recovered: bool = False    # True if the translation was parsed with recovery

    @property
    def passed(self) -> bool:
        # parse_error alone is not blocking if no structural defect was found, but
        # in practice a parse error always coincides with a tag defect; treat any
        # defect as a failure.
        return not self.defects


# --- helpers ------------------------------------------------------------------

def _sig(tok) -> tuple:
    """Structural signature: collapses text, keeps tag kind + name.

    Two translations of the same sentence have equal signatures; a missing
    ``</em>`` produces a CLOSE signature present only on one side.
    """
    if tok.kind in (OPEN, VOID_K, CLOSE):
        return (tok.kind, tok.name)
    if tok.kind == X.TEXT:
        return ("T",)            # any prose — content ignored for structure
    return (tok.kind,)           # comment/decl/pi/cdata/lt — keep kind


def _move_sig(tok) -> tuple:
    """Identity of a tag for move detection: kind, name **and** attributes.

    Deliberately stricter than :func:`_sig`, which ignores attributes because it
    only has to align. Here two tags are interchangeable only if they are the
    *same element*: a ``<span class="a">`` that reappears as ``<span class="b">``
    is a defect, not a move, and must not cancel out.
    """
    if tok.kind in (OPEN, VOID_K):
        return (tok.kind, tok.name, X.attr_signature(tok))
    return (tok.kind, tok.name)


def _ctx_at(ctx: list, idx: int):
    """Enclosing-block span at token ``idx``, tolerating an index past the end.

    A ``delete`` opcode has an empty range on the translated side, so ``j1`` can
    sit at (or past) the last token; the block it belongs to is then the one the
    preceding token is in.
    """
    if not ctx:
        return None
    if 0 <= idx < len(ctx):
        return ctx[idx]
    return ctx[-1] if idx >= len(ctx) else None


def _block_text_ratio_ok(orig: str, trad: str, o_span, t_span, cfg: dict) -> bool:
    """True if the translated block still carries the original's prose.

    Guards :func:`_inline_moves`: cancelling a move silences every tag defect in
    that block, so the block must be shown not to have lost text as well. Without
    this, "the model dropped half the sentence *and* moved the ``<em>``" would
    cancel to nothing.
    """
    def _prose(markup: str) -> str:
        return re.sub(r"<[^>]*>", "", markup).strip()

    o_len = len(_prose(orig[o_span[0]:o_span[1]]))
    t_len = len(_prose(trad[t_span[0]:t_span[1]]))
    if o_len < int(cfg.get("block_text_min", 80)):
        return True                        # too short to judge — not our call
    return t_len >= float(cfg.get("block_text_ratio", 0.7)) * o_len


def _inline_moves(O, T, opcodes, ctx_o, ctx_t, orig, trad, cfg) -> set:
    """Indices of opcodes that are an inline *reordering*, not a defect.

    Translating reorders words, and inline markup travels with the word it marks:
    "The TINY ``main`` Routine" becomes "La routine ``main`` di TINY", so the
    ``<span>`` around the small-caps name legitimately lands elsewhere in the
    sentence. ``difflib`` aligns by position and reports that as a delete plus an
    insert — ``missing tag <span>; extra tag <span>`` — and the whole chapter is
    rejected for a defect it does not have. Measured on csapp: three files failed
    on nothing else, and each re-translation reproduced the same "defect", because
    the translation was right all along.

    A group of opcodes is a move when, inside **one aligned block**, the multiset
    of tags leaving the original equals the multiset arriving in the translation.
    Three restrictions keep that from hiding real damage:

    * attributes are part of the identity (:func:`_move_sig`), so a tag that comes
      back altered does not cancel;
    * block-level tags never cancel — paragraph order is not a translator's
      choice, and this must not mask a dropped or duplicated block;
    * the block must still carry its prose (:func:`_block_text_ratio_ok`).

    What this does accept is emphasis landing on the wrong word — indistinguishable
    from a legitimate move without reading the sentence. That trade is deliberate:
    the false rejection costs a whole chapter and an hour of re-translation that
    provably changes nothing, the false acceptance costs one misplaced ``<em>``.
    """
    groups: dict = {}
    for k, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag == "equal":
            continue
        key = (_ctx_at(ctx_o, i1), _ctx_at(ctx_t, j1))
        if key[0] is None or key[1] is None:
            continue                       # outside any block: too loose to pair
        groups.setdefault(key, []).append(k)

    moved = set()
    for (o_span, t_span), indices in groups.items():
        removed, added = [], []
        for k in indices:
            _, i1, i2, j1, j2 = opcodes[k]
            removed += [_move_sig(t) for t in O[i1:i2]
                        if t.kind in (OPEN, CLOSE, VOID_K)]
            added += [_move_sig(t) for t in T[j1:j2]
                      if t.kind in (OPEN, CLOSE, VOID_K)]
        if not removed or sorted(removed) != sorted(added):
            continue                       # tags were gained or lost, not moved
        if any(X.is_block(name) for (_kind, name, *_rest) in removed):
            continue                       # blocks do not get reordered
        if not _block_text_ratio_ok(orig, trad, o_span, t_span, cfg):
            continue                       # prose was lost too — report it
        moved.update(indices)
    return moved


def _block_contexts(tokens: list) -> list:
    """For each token index, the (start, end) char span of its innermost block.

    Tolerant of imbalance (a stray close pops the stack regardless of name).
    Tokens outside any block get ``None``.
    """
    # pass 1: match each block open to its close
    pairs, stack = {}, []
    for i, t in enumerate(tokens):
        if t.kind == OPEN and X.is_block(t.name):
            stack.append(i)
        elif t.kind == CLOSE and X.is_block(t.name) and stack:
            pairs[stack.pop()] = i
    # pass 2: assign innermost context. A block's OWN open/close tag belongs to
    # the block it delimits, not to its parent — so push before assigning (and pop
    # after). Getting this wrong labels a block boundary with the container's span,
    # and a defect there would make repair re-translate/insert the whole <body>.
    ctx = [None] * len(tokens)
    open_stack = []              # (open_idx, close_idx)
    for i, t in enumerate(tokens):
        if t.kind == OPEN and X.is_block(t.name) and i in pairs:
            open_stack.append((i, pairs[i]))
        if open_stack:
            o, c = open_stack[-1]
            ctx[i] = (tokens[o].start, tokens[c].end)
        if t.kind == CLOSE and X.is_block(t.name) and open_stack:
            open_stack.pop()
    return ctx


def _span(tokens: list, i1: int, i2: int) -> tuple:
    """Char span [start, end] of tokens[i1:i2] (empty range -> (-1,-1))."""
    if i1 >= i2 or i1 >= len(tokens):
        return (-1, -1)
    return (tokens[i1].start, tokens[i2 - 1].end)


def _has_block_open(tokens: list, i1: int, i2: int) -> bool:
    return any(tokens[k].kind == OPEN and X.is_block(tokens[k].name)
               for k in range(i1, min(i2, len(tokens))))


def _has_tag(tokens: list, i1: int, i2: int) -> bool:
    return any(tokens[k].kind in (OPEN, CLOSE, VOID_K)
               for k in range(i1, min(i2, len(tokens))))


def _tag_names(tokens: list, i1: int, i2: int) -> str:
    names = sorted({tokens[k].name for k in range(i1, min(i2, len(tokens)))
                    if tokens[k].kind in (OPEN, CLOSE, VOID_K)})
    return ",".join(names)


# --- core analysis ------------------------------------------------------------

def analyze(orig: str, trad: str, cfg: dict) -> Analysis:
    """Compute the structural diff and localize every defect."""
    O = tokenize(orig)
    T = tokenize(trad)
    ctx_o, ctx_t = _block_contexts(O), _block_contexts(T)
    defects: list = []

    # 1. prologue (everything up to and including the <html> start tag)
    defects += _prologue_defects(O, T, orig, trad)

    # 2. full token-sequence alignment on structural signatures
    osig = [_sig(t) for t in O]
    tsig = [_sig(t) for t in T]
    sm = difflib.SequenceMatcher(None, osig, tsig, autojunk=False)
    opcodes = sm.get_opcodes()
    # Inline markup that merely changed place is not a defect (see _inline_moves).
    moved = _inline_moves(O, T, opcodes, ctx_o, ctx_t, orig, trad, cfg)

    for k, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag == "equal":
            # 3. attribute check on aligned tag pairs
            defects += _attr_defects(O, T, i1, i2, j1, j2, ctx_o, ctx_t)
            # 3b. content check: prose lost inside aligned blocks (the tags all
            # match, so only comparing the text can catch an abbreviation)
            defects += _content_defects(O, T, i1, i2, j1, j2, ctx_o, ctx_t, cfg)
            continue
        if k in moved:
            continue
        # Skip pure-prose divergences (the model legitimately rephrases text):
        # only flag opcodes that touch at least one tag.
        if tag == "delete" and not _has_tag(O, i1, i2):
            continue
        if tag == "insert" and not _has_tag(T, j1, j2):
            continue
        if tag == "replace" and not (_has_tag(O, i1, i2) or _has_tag(T, j1, j2)):
            continue
        o_span = _span(O, i1, i2)
        t_span = _span(T, j1, j2)
        # classify
        if tag == "delete":
            kind = MISSING_BLOCK if _has_block_open(O, i1, i2) else MISSING_TAG
            names = _tag_names(O, i1, i2)
            detail = (f"missing block <{names}>" if kind == MISSING_BLOCK
                      else f"missing tag <{names}>")
        elif tag == "insert":
            kind = EXTRA_TAG
            detail = f"extra tag <{_tag_names(T, j1, j2)}> in translation"
        else:  # replace
            kind = WRONG_TAG
            detail = (f"tag mismatch: orig <{_tag_names(O, i1, i2)}> "
                      f"vs trad <{_tag_names(T, j1, j2)}>")
        defects.append(_make(kind, detail, o_span, t_span, ctx_o, ctx_t, i1, j1))

    # 4. well-formedness check (diagnostic, non-authoritative)
    recovered = _add_parse_error_if_any(trad, defects)

    return Analysis(defects=defects, recovered=recovered)


def _make(kind, detail, o_span, t_span, ctx_o, ctx_t, oi, tj) -> Defect:
    """Build a Defect with localized spans and enclosing-block context."""
    d = Defect(kind=kind, detail=detail,
               orig_start=o_span[0], orig_end=o_span[1],
               trad_start=t_span[0], trad_end=t_span[1])
    if oi < len(ctx_o) and ctx_o[oi]:
        d.block_orig = ctx_o[oi]
    if tj < len(ctx_t) and ctx_t[tj]:
        d.block_trad = ctx_t[tj]
    return d


def _prologue_defects(O, T, orig, trad) -> list:
    """Detect differences in the pre-<html> content (xml decl / doctype / <html>)."""
    def _html_end(tokens):
        for t in tokens:
            if t.kind == OPEN and t.name == "html":
                return t.end
        return -1
    oe, te = _html_end(O), _html_end(T)
    if oe < 0 or te < 0:
        return []
    op, tp = orig[:oe], trad[:te]
    # ignore the lang attribute value when comparing the <html> tag
    op_n = _strip_lang(op)
    tp_n = _strip_lang(tp)
    if op_n != tp_n:
        return [Defect(kind=PROLOGUE,
                       detail="prologue (xml decl / doctype / <html>) differs",
                       orig_start=0, orig_end=oe, trad_start=0, trad_end=te,
                       block_orig=(0, oe), block_trad=(0, te))]
    return []


def _strip_lang(text: str) -> str:
    """Remove lang/xml:lang attributes so they never count as differences.

    Both the value *and* the attribute itself are dropped: the target language is
    a legitimate change, and :func:`repair.rewrite_lang` may have to *insert* the
    attribute on sources that omit it (some files carry no ``lang`` at all).
    Masking only the value would make our own fix look like a prologue mismatch.
    """
    return re.sub(r'''\s*(?:xml:)?lang\s*=\s*(?:"[^"]*"|'[^']*')''', "", text)


# Attributes shown per side in an 'attribute differs' message. A start tag whose
# quoting the model broke swallows everything up to the next quote, and the
# tokenizer then reads the swallowed prose as attributes: csapp ch03 produced one
# "tag" carrying 1525 of them, which buried every other line of the report in the
# terminal and in the log. The count is what says "this tag is malformed"; the
# list itself stops being evidence long before that.
_ATTRS_SHOWN = 6


def _attr_list(attrs: list) -> str:
    """Render an attribute list for a message, capped at :data:`_ATTRS_SHOWN`."""
    items = sorted(attrs)
    shown = ", ".join(f"{n}={v!r}" if v else n for n, v in items[:_ATTRS_SHOWN])
    rest = len(items) - _ATTRS_SHOWN
    return f"[{shown}{f', +{rest} more' if rest > 0 else ''}]"


def _attr_defects(O, T, i1, i2, j1, j2, ctx_o, ctx_t) -> list:
    """Compare attributes of aligned tag pairs within an 'equal' run."""
    out = []
    oi, tj = i1, j1
    while oi < i2 and tj < j2:
        ot, tt = O[oi], T[tj]
        if ot.kind in (OPEN, VOID_K) and X.attr_signature(ot) != X.attr_signature(tt):
            d = _make(ATTR, f"attribute differs on <{ot.name}>: "
                       f"orig {_attr_list(ot.attrs)} vs "
                       f"trad {_attr_list(tt.attrs)}",
                      (ot.start, ot.end), (tt.start, tt.end), ctx_o, ctx_t, oi, tj)
            out.append(d)
        oi += 1
        tj += 1
    return out


def _prose_block_spans(tokens: list, names: set = PROSE_BLOCKS) -> dict:
    """Map each prose-block open-token index to its ``(start, end)`` char span.

    The span covers the whole element (open tag through close tag). Unclosed
    blocks (truncated tail) are skipped: content checks need both ends.
    """
    spans, stack = {}, []
    for idx, t in enumerate(tokens):
        if t.kind == OPEN and t.name in names:
            stack.append(idx)
        elif t.kind == CLOSE and t.name in names and stack:
            oi = stack.pop()
            spans[oi] = (tokens[oi].start, t.end)
    return spans


def _prose_len(text: str) -> int:
    """Length of a text as prose: entities resolved, whitespace runs folded."""
    return len(" ".join(html.unescape(text).split()))


def _text_of(tokens: list, lo: int, hi: int) -> str:
    """Concatenate the TEXT tokens whose index is in ``[lo, hi)``."""
    return "".join(t.raw for t in tokens[lo:hi] if t.kind == X.TEXT)


def _token_range(tokens: list, open_idx: int, span: tuple) -> int:
    """Index just past the last token of the element opened at ``open_idx``."""
    end = span[1]
    j = open_idx
    while j < len(tokens) and tokens[j].end <= end:
        j += 1
    return j


def _is_code_block(tok, hints: list) -> bool:
    """True if a block carries a CSS class marking it as code (never translated)."""
    if not hints:
        return False
    classes = dict(tok.attrs).get("class", "").split()
    return any(c in hints for c in classes)


def _fold(text: str) -> str:
    """Case- and accent-insensitive form, for robust term matching.

    ``exotérisme`` and ``Exoterisme`` fold to the same key, so a glossary entry
    matches regardless of accents (which the model often normalizes away) and of
    capitalization at the start of a sentence.
    """
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _term_stem(term: str) -> str:
    """Folded stem of a glossary term, to also match its inflected forms.

    Drops a short trailing inflection so that ``exotérisme`` also matches
    ``exotérique``/``exotérismes``: the shared stem is what identifies the concept.

    A rendering of exactly 7 characters or fewer used to be matched literally, and
    the ordinary Italian plural then failed it: "trigoni" does not contain
    "trigono", so a correctly translated chapter was flagged, exhausted its repair
    attempts and was re-translated whole. Dropping the final letter keeps the stem
    specific ("trigon", "sestil", "settil") while admitting the inflection; below 5
    characters there is nothing left to drop safely.
    """
    folded = _fold(term)
    if len(folded) > 7:
        return folded[:-4]
    return folded[:-1] if len(folded) >= 5 else folded


_TERM_MISS_RE = re.compile(r"term '([^']+)' not rendered as '([^']+)'")


def glossary_conflicts(reasons_by_file: dict) -> list:
    """Pinned terms no retry ever satisfied, aggregated over the failed files.

    ``reasons_by_file`` maps a spine file to the failure texts of its units
    (the cache) and, for saved files, of its verification. A model slip is
    bounded by the retries; what survives them in several files is a
    constraint the text itself contradicts. Measured on a real book: one
    entry right for ~90% of the occurrences and wrong for the rest held 17
    files unresolved over two runs (~30 wasted LLM attempts), and the run's
    own closing advice — "a term wrong over and over? pin it" — addresses
    the opposite case, an unpinned term. Nothing pointed at the glossary.

    Returns ``[{term, expected, files, hits}]``, most widespread first. A
    term becomes a conflict only across two files or more: one file can be
    a genuinely hard unit, and a false alarm here would send a correct pin
    to be re-decided.
    """
    seen: dict = {}
    for rel, texts in (reasons_by_file or {}).items():
        for m in _TERM_MISS_RE.finditer("\n".join(texts or [])):
            src, dst = m.group(1), m.group(2)
            e = seen.setdefault(src, {"term": src, "expected": dst,
                                      "files": [], "hits": 0})
            if rel not in e["files"]:
                e["files"].append(rel)
            e["hits"] += 1
    out = [e for e in seen.values() if len(e["files"]) > 1]
    return sorted(out, key=lambda e: (-len(e["files"]), -e["hits"], e["term"]))


def _glossary_defects(o_text: str, t_text: str, glossary: dict,
                      exceptions: dict = None) -> list:
    """Terms required by the glossary but not rendered in the translated block.

    Returns a list of ``(source_term, expected)`` pairs that are missing. Only
    terms actually present in the original block are checked, and the expected
    rendering is matched on its stem, so inflections still count as correct.
    ``exceptions`` (``{term: [phrase, ...]}``) names contexts — a journal title,
    a proper name — where the term legitimately stays: occurrences inside such
    a phrase do not count as presence in the block.

    A term also counts as missing when the SOURCE term itself is still in the
    translation (left in the source language). A small model often does this
    ("Islamic esoterism" copied through unchanged); and because the rendering
    "esoterismo" shares the stem "esoter" with the source "esoterism", a pure
    stem check would accept it as rendered. The source-still-present test closes
    that hole. ("essoterico", stem "essot", never matched "exoteric", which is
    why that pair was already caught.)

    That test is skipped when the rendering is the source term itself, as it is
    whenever a language keeps a word ("quintile", "novile" are spelled the same in
    English and Italian). There the source word in the translation is the correct
    result, not a leftover, and firing on it made the entry unsatisfiable.

    The source term must start at a **word boundary**, but may carry any suffix:
    that is what lets "esoterisme" catch "esoterismes" while "trine" no longer
    fires on "doctrines". Matching it anywhere, as this did, made a pinned term
    demand its rendering in blocks that never contained it — a failure no
    translation could clear, so the file looped until the run gave up.
    """
    if not glossary:
        return []
    o_folded, t_folded = _fold(o_text), _fold(t_text)
    missing = []
    for src, dst in glossary.items():
        src_f = _fold(src)
        o_seen = o_folded
        t_seen = t_folded
        for phrase in (exceptions or {}).get(src, []):
            o_seen = o_seen.replace(_fold(phrase), " ")
            # The phrase names a context where the term legitimately stays in
            # the source language — a title, a proper name — so the model keeps
            # it verbatim in the translation. Excising it from the original only
            # made the check demand the rendering in a block that never carried
            # the term, while the title surviving in the translation still read
            # as an untranslated leftover: the entry stayed unsatisfiable, which
            # is the one state a check must never be in.
            t_seen = t_seen.replace(_fold(phrase), " ")
        if not re.search(r"\b" + re.escape(src_f), o_seen):
            continue                       # term not in this block: nothing to check
        rendered = _term_stem(dst) in t_folded
        # The source term itself, as a whole word, still in the translation = the
        # model left it in the source language. Whole-word (\b) so "esoterism" does
        # not match inside the correct rendering "esoterismo" (its prefix).
        # Two cases make the test blind, and a blind test is worse than none: it
        # fails translations nothing could ever clear.
        # 1. The rendering IS the source term: Italian keeps "quintile" and
        #    "novile" unchanged, so the surviving source word is what CORRECT
        #    looks like.
        # 2. The two are cognates sharing a stem: the Italian plural of
        #    "quadratura" is spelled "quadrature" — exactly the English source
        #    term — so "le opposizioni e le quadrature" is a perfect translation
        #    the test would read as an untranslated leftover. Only `rendered`
        #    can judge a cognate pair.
        keeps_source = bool(re.match(r'\b' + re.escape(src_f) + r'\b', _fold(dst)))
        cognate = _term_stem(src) == _term_stem(dst)
        left_untranslated = (not keeps_source and not cognate) and bool(
            re.search(r'\b' + re.escape(src_f) + r'\b', t_seen))
        if not rendered or left_untranslated:
            missing.append((src, dst))
    return missing


def _content_defects(O, T, i1, i2, j1, j2, ctx_o, ctx_t, cfg: dict) -> list:
    """Detect prose lost inside aligned blocks (abbreviation / added ellipsis).

    The structural diff cannot see this: when the model summarizes a paragraph and
    writes "..." instead of the text, every tag is still in place. Here the *text*
    of each aligned prose block is compared, so an abbreviated block becomes a
    localized defect that :mod:`repair` can re-translate.
    """
    ratio = float(cfg.get("block_text_ratio", 0.7))
    min_len = int(cfg.get("block_text_min", 80))
    hints = cfg.get("code_class_hints") or []
    glossary = cfg.get("glossary") or {}
    o_spans = _prose_block_spans(O)
    t_spans = _prose_block_spans(T)
    out = []
    oi, tj = i1, j1
    while oi < i2 and tj < j2:
        ot = O[oi]
        if (ot.kind == OPEN and ot.name in PROSE_BLOCKS
                and oi in o_spans and tj in t_spans
                and not _is_code_block(ot, hints)):
            o_span, t_span = o_spans[oi], t_spans[tj]
            o_text = _text_of(O, oi, _token_range(O, oi, o_span))
            t_text = _text_of(T, tj, _token_range(T, tj, t_span))
            # Measured on the prose, not on its encoding: an entity is one
            # character, and a run of indentation is one space. Otherwise a
            # paragraph the source wrote as "&#8217;" plus wrapped lines reads
            # 40 % "shorter" once it comes back as ’ on a single line.
            o_len, t_len = _prose_len(o_text), _prose_len(t_text)
            if o_len >= min_len and t_len < ratio * o_len:
                pct = round(100 * t_len / max(o_len, 1))
                d = _make(SHORT_TEXT,
                          f"text abbreviated in <{ot.name}>: {t_len} of {o_len} "
                          f"chars ({pct}%)",
                          o_span, t_span, ctx_o, ctx_t, oi, tj)
                d.block_orig, d.block_trad = o_span, t_span
                out.append(d)
            else:
                # on the prose, not its encoding: "&#8230;" in the source is the
                # same ellipsis as "…" in the translation (effective-c ch05)
                o_ell = len(_ELLIPSIS_RE.findall(html.unescape(o_text)))
                t_ell = len(_ELLIPSIS_RE.findall(html.unescape(t_text)))
                if t_ell > o_ell:
                    d = _make(ELLIPSIS,
                              f"ellipsis added in <{ot.name}>: {t_ell} vs {o_ell} "
                              f"— text likely omitted",
                              o_span, t_span, ctx_o, ctx_t, oi, tj)
                    d.block_orig, d.block_trad = o_span, t_span
                    out.append(d)
                # glossary: a pinned term must be rendered as required. Structure
                # and length say nothing here — only the words do.
                for src, dst in _glossary_defects(o_text, t_text, glossary,
                                                  cfg.get("glossary_exceptions")):
                    d = _make(GLOSSARY,
                              f"term '{src}' not rendered as '{dst}' in <{ot.name}>",
                              o_span, t_span, ctx_o, ctx_t, oi, tj)
                    d.block_orig, d.block_trad = o_span, t_span
                    out.append(d)
        oi += 1
        tj += 1
    return out


def _add_parse_error_if_any(trad: str, defects: list) -> bool:
    """If the translation is genuinely malformed, record a parse_error defect.

    Uses lxml with recovery so it never raises. Undefined-entity errors (e.g.
    ``Entity 'nbsp' not defined``) are filtered out: they are an artefact of the
    XHTML DTD not being loaded (``&nbsp;`` is legal in the source), not a model
    error. Genuine well-formedness errors (mismatched tag, invalid token, …) are
    kept. Returns True if a real error was found.
    """
    try:
        from lxml import etree
    except ImportError:
        return False
    parser = etree.XMLParser(resolve_entities=False, recover=True)
    try:
        etree.fromstring(trad.encode("utf-8"), parser)
    except Exception:
        return True
    real = [e for e in parser.error_log
            if "Entity " not in e.message and "not defined" not in e.message]
    if real:
        first = real[0]
        defects.append(Defect(
            kind=PARSE_ERROR,
            detail=f"not well-formed: {first.message} (line {first.line}, col {first.column})"))
        return True
    return False
