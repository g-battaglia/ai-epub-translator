"""What the quality gate rewrites, and the small deterministic helpers.

Translation itself no longer needs repair: the model never sees markup (see
:mod:`units`), so there is no dropped ``</span>`` to restore and no invented
``</p>`` to delete. What remains here is the second half of the pipeline — the
LLM judge has read a finished chapter and named a defect of *meaning* — and
the deterministic transforms every path shares:

* :func:`polish_file` — rewrite the units a reviewer's note applies to, on the
  placeholder-protected text, keeping a unit only when it still validates;
* :func:`fix_leftovers` — translate the source words the model carried over;
* :func:`apply_quoted_correction` — a note that states the fix outright
  (``"ASPECTI" invece di "ASPETTI"``) is a find-and-replace, no model;
* :func:`rewrite_lang`, :func:`match_line_ending`, :func:`_fix_named_entities`.
"""

from __future__ import annotations

import html.entities
import re
from dataclasses import dataclass

from . import leftovers, llm
from . import units as U
from . import xhtml as X
from .structdiff import _fold
from .xhtml import CLOSE, OPEN, tokenize

# A NAMED entity valid in HTML (&mdash;, &copy;) but UNDEFINED in XML is a strict
# parse error. gemma emits these even when the source uses numeric refs, and a
# future book's source may use them directly. Convert a known name to its numeric
# form; escape an unknown one so the file stays well-formed. The five XML entities
# are left untouched.
_NAMED_ENTITY = re.compile(r'&([A-Za-z][A-Za-z0-9]*);')


_XML_ENTITIES = frozenset(("amp", "lt", "gt", "quot", "apos"))


def _fix_named_entities(text: str) -> str:
    def repl(m):
        name = m.group(1)
        if name in _XML_ENTITIES:
            return m.group(0)
        cp = html.entities.name2codepoint.get(name)
        return f"&#{cp};" if cp is not None else f"&amp;{name};"
    return _NAMED_ENTITY.sub(repl, text)


# lang / xml:lang attribute values.
_LANG_RE = re.compile(r'''(xml:lang|lang)\s*=\s*(?:"[^"]*"|'[^']*')''')


# the opening <html> tag (where the language attribute belongs).
_HTML_TAG_RE = re.compile(r'<html\b[^>]*>', re.I)


def rewrite_lang(text: str, dest_code: str) -> str:
    """Set every ``lang``/``xml:lang`` attribute value to ``dest_code``.

    When the ``<html>`` tag carries no language attribute at all (some source
    files simply omit it), one is *inserted*: the verification requires the target
    language on ``<html>``, so a pure substitution would leave such a file
    impossible to pass.
    """
    text = _LANG_RE.sub(lambda m: f'{m.group(1)}="{dest_code}"', text)
    m = _HTML_TAG_RE.search(text)
    if m and "lang=" not in m.group(0):
        tag = m.group(0)
        # insert before the closing '>' (keeping any self-closing slash intact)
        head = tag[:-1].rstrip()
        new_tag = f'{head} lang="{dest_code}" xml:lang="{dest_code}">'
        text = text[:m.start()] + new_tag + text[m.end():]
    return text


def match_line_ending(text: str, reference: str) -> str:
    """Make ``text`` use the same line endings (CRLF/LF) as ``reference``."""
    ref_crlf = "\r\n" in reference
    if ref_crlf:
        norm = text.replace("\r\n", "\n").replace("\r", "\n")
        return norm.replace("\n", "\r\n")
    return text.replace("\r\n", "\n").replace("\r", "\n")


@dataclass
class Edit:
    """Replace ``text[start:end]`` with ``replacement``."""

    start: int
    end: int
    replacement: str

    @property
    def span(self) -> tuple:
        return (self.start, self.end)


def _resolve_overlaps(edits: list) -> list:
    """Keep a maximal set of non-overlapping edits (drop those contained in another)."""
    edits = sorted(edits, key=lambda e: (e.start, -(e.end - e.start)))
    kept, last_end = [], -1
    for e in edits:
        if e.start >= last_end:
            kept.append(e)
            last_end = e.end
        # else: overlaps a kept (larger/earlier) edit -> drop
    return kept


def apply_edits(text: str, edits: list) -> str:
    """Apply non-overlapping edits in one descending pass (offsets stay valid)."""
    out = text
    for e in reversed(_resolve_overlaps(edits)):
        out = out[:e.start] + e.replacement + out[e.end:]
    return out


def _fragment_ok(text: str) -> tuple:
    """True if a fragment is well-formed (ignoring undefined-entity errors)."""
    try:
        from lxml import etree
    except ImportError:
        return True, ""
    parser = etree.XMLParser(resolve_entities=False, recover=True)
    try:
        etree.fromstring(("<r>" + text + "</r>").encode("utf-8"), parser)
    except Exception as e:                           # noqa: BLE001 — recover never raises
        return False, str(e)
    real = [e.message for e in parser.error_log
            if "Entity " not in e.message and "not defined" not in e.message]
    return (not real), (real[0] if real else "")


def _plain_text(fragment: str) -> str:
    """Prose of a fragment (tags stripped)."""
    return "".join(t.raw for t in tokenize(fragment) if t.kind == X.TEXT)


# Two renderings this close apart are a confusion, not a synonym: "esoterismo" vs
# "essoterismo" (esoterism vs exoterism) differ by one character and mean opposite
# things. Wider than this and the candidate is simply another word.
MAX_TWIN_DISTANCE = 2


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance (small strings; no dependency worth pulling in)."""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _wrong_rendering(src: str, dst: str, orig_text: str, trad_text: str,
                     glossary: dict) -> str:
    """Explain what the previous output wrote instead of ``dst``, if identifiable.

    Two sources of evidence, strongest first:

    1. *Twin glossary term*: the block carries the rendering of a DIFFERENT
       glossary entry whose source term is not even in the original — the classic
       ``esoterism``/``exoterism`` swap. Naming the confusion ("you wrote the word
       that renders the opposite term") is the one insight that unsticks it.
    2. *Near-miss word*: no twin entry, but some word in the translation is one or
       two characters away from the expected rendering.

    Returns "" when nothing can be pinned down — an empty note is better than a
    wrong accusation, which would only mislead the model.
    """
    o_folded, t_folded = _fold(orig_text), _fold(trad_text)
    dst_f = _fold(dst)

    for other_src, other_dst in glossary.items():
        other_f = _fold(other_dst)
        if other_f == dst_f or _fold(other_src) == _fold(src):
            continue
        if _edit_distance(other_f, dst_f) > MAX_TWIN_DISTANCE:
            continue
        if not re.search(r"\b" + re.escape(other_f) + r"\b", t_folded):
            continue
        if re.search(r"\b" + re.escape(_fold(other_src)) + r"\b", o_folded):
            continue                      # both terms are legitimately present
        return (f"the previous output wrote “{other_dst}”, which is the "
                f"rendering of “{other_src}” — a DIFFERENT term. "
                f"Never use it for “{src}”.")

    # Renderings that belong to another glossary term actually present in the
    # original are legitimate: they must never be reported as a mistake.
    legit = {_fold(d) for s, d in glossary.items()
             if re.search(r"\b" + re.escape(_fold(s)) + r"\b", o_folded)}
    for word in sorted(set(re.findall(r"\w+", t_folded))):
        if word in legit or word == dst_f:
            continue
        if 0 < _edit_distance(word, dst_f) <= MAX_TWIN_DISTANCE:
            return (f"the previous output wrote “{word}” instead — "
                    f"it is not the required form.")
    return ""


def _glossary_focus_terms(missing: list, orig_text: str, trad_text: str,
                          glossary: dict) -> list:
    """Attach the 'what you wrote instead' evidence to each missing term."""
    out = []
    for src, dst in missing:
        note = _wrong_rendering(src, dst, orig_text, trad_text, glossary)
        out.append((src, dst, note) if note else (src, dst))
    return out


PROSE_BLOCKS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td", "th",
                "dt", "dd", "caption", "figcaption", "blockquote", "title"}


# A rewritten sentence this much longer or shorter than the original is not a
# word swap: the model rephrased or truncated, and the answer is discarded.
LEFTOVER_LEN_RATIO = (0.5, 2.0)


def _leftover_ok(new: str, original: str, word: str) -> bool:
    """Objective acceptance test for a rewritten sentence.

    Nothing here is a matter of taste, which is what makes this fix safe to apply
    to a chapter the judge already scored 10/10: the word must be gone, no markup
    may have appeared inside what is a plain text node, and the length must still
    be that of the same sentence.
    """
    if not new or "<" in new or ">" in new:
        return False
    if leftovers.has_any(new, {word}):
        return False
    lo, hi = LEFTOVER_LEN_RATIO
    return lo <= len(new) / max(len(original), 1) <= hi


def _source_sentence(orig: str, trad: str, span: tuple, sentence: str) -> str:
    """The source sentence facing ``sentence``, or "" if it cannot be pinned down.

    Prose blocks are aligned by position (a verified translation has the same
    blocks in the same order); within the pair, sentences are matched by index and
    only when both sides have the same count — a mismatch means the model merged or
    split sentences, and a wrong pairing would translate the wrong text.
    """
    o_toks, t_toks = tokenize(orig), tokenize(trad)
    pairs = _align_prose(o_toks, t_toks, _prose_spans(o_toks), _prose_spans(t_toks))
    target = " ".join(sentence.split())
    for (os_, oe), (ts, te) in pairs:
        if not (ts <= span[0] and span[1] <= te):
            continue
        o_sents = leftovers.sentences(_plain_text(orig[os_:oe]))
        t_sents = leftovers.sentences(_plain_text(trad[ts:te]))
        if len(o_sents) != len(t_sents):
            return ""
        for i, s in enumerate(t_sents):
            if s and (s in target or target in s):
                return o_sents[i]
        return ""
    return ""


def fix_leftovers(orig: str, trad: str, cfg: dict, chat_fn=None) -> dict:
    """Translate the source words the model carried over into the translation.

    Two strategies per occurrence, in order, each validated by
    :func:`_leftover_ok` and each retried a few times:

    1. rewrite the translated sentence, replacing the one word;
    2. translate the source sentence afresh.

    Measured on ten real occurrences: five fall to (1), five to (2), and they are
    not the same five — together they clear seven. What survives both is reported
    in ``remaining`` rather than quietly shipped.

    Returns ``{text, fixed, remaining, aborted}``.
    """
    chat_fn = chat_fn or llm.default_chat(cfg)
    words = set(leftovers.find(orig, trad, cfg))
    if not words:
        return {"text": trad, "fixed": 0, "remaining": [], "aborted": False}
    retries = int(cfg.get("block_retries", 2))
    edits, fixed, remaining = [], 0, []
    for span, fragment, word in leftovers.occurrences(trad, words):
        # The window runs to the start of the next sentence, so it swallows the
        # space after the full stop. The model answers with a bare sentence, so
        # that separator has to be put back or the two sentences fuse ("chiaro.Poi").
        sentence = fragment.rstrip()
        tail = fragment[len(sentence):]
        prompts = [llm.build_leftover_fix_prompt(sentence, word, cfg)]
        source = _source_sentence(orig, trad, span, sentence)
        if source:
            prompts.append(llm.build_leftover_retranslate_prompt(source, cfg))
        winner = None
        for prompt in prompts:
            for _ in range(retries):
                try:
                    res = chat_fn(prompt, max_tokens=max(256, len(sentence) * 3 + 256))
                except RuntimeError as e:
                    return {"text": apply_edits(trad, edits), "fixed": fixed,
                            "remaining": remaining, "aborted": True,
                            "error": str(e)}
                new = (res.get("text") or "").strip()
                if _leftover_ok(new, sentence, word):
                    winner = new
                    break
            if winner:
                break
        if winner:
            edits.append(Edit(span[0], span[1], winner + tail))
            fixed += 1
        else:
            remaining.append((word, " ".join(sentence.split())))
    return {"text": apply_edits(trad, edits), "fixed": fixed,
            "remaining": remaining, "aborted": False}


# The judge's note sometimes states the correction outright: '"ASPECTI"
# invece di "ASPETTI"', '"Luce" instead of "Luci"'. When both quoted words are
# single words a typo's worth apart, the note IS a find-and-replace, and the
# wrong form must be rare (a typo is; a real word is not — replacing "Luce"
# everywhere it is correct would wreck the chapter).
_QUOTED_INSTEAD = re.compile(
    r'["«“]([^"»”\n]{1,40})["»”]\s*(?:invece di|instead of)\s*'
    r'["«“]([^"»”\n]{1,40})["»”]', re.I)


_TYPO_MAX_DIST = 0.34          # normalized: two letters in seven


_TYPO_MAX_OCCURRENCES = 3      # a typo is rare; a real word is everywhere


def apply_quoted_correction(trad: str, issue: str) -> tuple:
    """Apply a '"wrong" invece di "right"' note literally; no model involved.

    Returns ``(text, n)`` with ``n`` the replacements made. A pair is applied
    only when both sides are single words whose edit distance is typo-scale
    (a rewording like "cane" -> "gatto" is a content decision, not a typo),
    the wrong form occurs whole-word and rarely, and each occurrence keeps its
    case (all-caps title stays all-caps).
    """
    count = 0
    for wrong, right in _QUOTED_INSTEAD.findall(issue or ""):
        if wrong == right or " " in wrong.strip() or " " in right.strip():
            continue
        wrong, right = wrong.strip(), right.strip()
        dist = (_edit_distance(wrong.lower(), right.lower())
                / max(len(wrong), len(right), 1))
        if dist > _TYPO_MAX_DIST:
            continue
        hits = list(re.finditer(r"(?<![\w-])" + re.escape(wrong)
                                + r"(?![\w-])", trad, re.I))
        if not hits or len(hits) > _TYPO_MAX_OCCURRENCES:
            continue

        def _same_case(m, right=right):
            s = m.group(0)
            if s.isupper() and len(s) > 1:
                return right.upper()
            if s[0].isupper():
                return right[0].upper() + right[1:]
            return right

        trad = re.sub(r"(?<![\w-])" + re.escape(wrong) + r"(?![\w-])",
                      _same_case, trad, flags=re.I)
        count += len(hits)
    return trad, count


def _prose_spans(tokens: list) -> dict:
    """Map each prose-block open-token index to its ``(start, end)`` char span."""
    spans, stack = {}, []
    for i, t in enumerate(tokens):
        if t.kind == OPEN and t.name in PROSE_BLOCKS:
            stack.append(i)
        elif t.kind == CLOSE and t.name in PROSE_BLOCKS and stack:
            oi = stack.pop()
            spans[oi] = (tokens[oi].start, t.end)
    return spans


def _align_prose(o_toks, t_toks, o_spans, t_spans) -> list:
    """Pair up prose blocks that structurally correspond, in document order.

    A verified translation has the same prose blocks in the same order as the
    original (structdiff enforces that before polish runs), so pairing them by
    position is exact. When the counts differ (a damaged file), polish does
    nothing rather than risk mis-pairing.
    """
    o_open, t_open = sorted(o_spans), sorted(t_spans)
    if len(o_open) != len(t_open):
        return []
    return [(o_spans[oi], t_spans[ti]) for oi, ti in zip(o_open, t_open)]


def polish_file(orig: str, trad: str, issue: str, cfg: dict,
                chat_fn=None) -> dict:
    """Rewrite the units of a translation to fix a reviewer's issue.

    The quality gate reports a per-chapter issue (a register slip, a gender
    error, a mistranslated term). When the note states the correction outright
    — ``"ASPECTI" invece di "ASPETTI"`` — it is applied literally first (see
    :func:`apply_quoted_correction`): a typo the judge has already diagnosed
    needs no model, and the literal fix reaches the chapter ``<title>`` too.
    Then the units are shown to the model in batches with the note, as
    placeholder-protected text; it returns only what it changes, and each
    answer is accepted only when it validates like a fresh translation would
    (placeholders intact, length, glossary). Returns ``{text, changed, actions}``.
    """
    chat_fn = chat_fn or llm.default_chat(cfg)
    trad, literal = apply_quoted_correction(trad, issue)
    o_sk, t_sk = U.segment(orig, cfg), U.segment(trad, cfg)
    actions = [{"literal": True}] if literal else []
    if not U.aligned(o_sk, t_sk):
        # a damaged file: nothing to pair the units with, so do nothing
        return {"text": trad, "changed": literal, "actions": actions,
                "aborted": False}
    pairs = [(ou, tu) for ou, tu in zip(o_sk.translatable, t_sk.translatable)
             if len(tu.plain) >= 30]                 # too short to hold the issue
    inners, changed = {}, literal
    batch_chars = int(cfg.get("batch_chars", llm.DEFAULT_BATCH_CHARS))
    for batch in llm._batches([ou for ou, _tu in pairs], batch_chars):
        current = {ou.idx: tu for ou, tu in pairs}
        prompt = llm.build_polish_prompt(
            [(ou, current[ou.idx].visible) for ou in batch], issue, cfg)
        size = sum(len(current[ou.idx].visible) for ou in batch)
        try:
            res = chat_fn(prompt, max_tokens=max(512, size * 3 + 512))
        except RuntimeError as e:
            return {"text": U.reassemble(t_sk, inners), "changed": changed,
                    "actions": actions, "aborted": True, "error": str(e)}
        answers = llm.parse_units_reply(res.get("text", ""))
        for i, ou in enumerate(batch, 1):
            answer = answers.get(i)
            tu = current[ou.idx]
            if not answer or U.plain_text(answer).split() == tu.plain.split():
                continue
            inner, _why, _focus = llm.accept_unit(ou, answer, cfg, strict=True)
            if inner is None:
                continue
            inners[tu.idx] = inner
            changed += 1
            actions.append({"unit": tu.idx})
    return {"text": U.reassemble(t_sk, inners), "changed": changed,
            "actions": actions, "aborted": False}
