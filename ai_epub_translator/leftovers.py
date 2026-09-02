"""Source-language words the model forgot to translate.

Measured on the Guénon corpus: translating a 35 KB chapter in one call, the model
renders "quite" correctly four times out of six and leaves the other two in
English — in one paragraph it writes "del tutto essenziale; inoltre, è *quite*
possibile", six words apart. It is not a gap in the vocabulary; it is also not
curable by asking again. On the ten real leftovers of this book:

* asked to fix the enclosing block, the model returned it byte-identical 12/12;
* asked to fix the sentence, it fixed 5 of 10 — and the five failures failed
  every one of six retries, so this is deterministic, not luck;
* asked to translate the source sentence afresh, it fixed 5 of 10 — *another*
  five, which is why :func:`repair.fix_leftovers` tries both and clears 7.

What survives both is named in the run output rather than shipped in silence:
some contexts this model will not translate, and that is the reader's call.

The detection is deliberately narrow, because a false positive would ask the model
to "fix" something that is correct. Three conditions must all hold:

1. the word is in a small, closed list of **function words** — never a technical
   term, a proper name or a title, which may legitimately stay in the source
   language;
2. it also occurs in the **original of the same file**, so it is a carry-over and
   not an Italian word that happens to look English;
3. it sits in plain prose, **outside** ``<i>``/``<em>``/``<cite>`` — the corpus is
   full of deliberately italicized foreign terms (*qiblah*, *dharma*, English book
   titles) and those must never be touched.

Choosing the rendering stays with the model: "quite" is "del tutto" before an
absolute adjective and "piuttosto" before a gradable one, and only the context
decides.
"""

from __future__ import annotations

import re

# Function words that never survive a competent translation. Kept small on
# purpose: every entry must be a word whose presence in the target prose is
# unambiguously a mistake. Extend a book's list via ``leftover_words`` in
# book.toml rather than widening these defaults.
FUNCTION_WORDS = {
    "english": {
        "quite", "moreover", "however", "doubtless", "thereby", "whereas",
        "indeed", "therefore", "furthermore", "nevertheless", "besides",
        "although", "though", "whereby", "henceforth", "hitherto",
        "notwithstanding", "envisaged", "insofar", "likewise", "namely",
    },
    "french": {
        "toutefois", "néanmoins", "cependant", "d'ailleurs", "ainsi",
        "désormais", "notamment", "sans doute", "en effet",
    },
}

# Inline tags whose content is quoted on purpose (foreign terms, titles).
QUOTED_TAGS = ("i", "em", "cite", "code", "kbd", "samp")

_QUOTED_RE = re.compile(
    r"<(" + "|".join(QUOTED_TAGS) + r")\b[^>]*>.*?</\1\s*>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]*>")


def _prose_only(text: str) -> str:
    """Strip markup and the content of the quoted inline tags.

    The quoted spans are removed *before* the tags, so an italicized foreign term
    disappears with them and can never be reported.
    """
    return _TAG_RE.sub(" ", _QUOTED_RE.sub(" ", text))


def word_list(cfg: dict) -> set:
    """The function words to look for, given the book's source language."""
    lang = (cfg.get("source_lang") or "english").strip().lower()
    words = set(FUNCTION_WORDS.get(lang, ()))
    words.update(w.strip().lower() for w in (cfg.get("leftover_words") or [])
                 if w and w.strip())
    return words


def _occurrences(text: str, words: set) -> dict:
    """Count each searched word in ``text`` (word-boundary, case-insensitive)."""
    found = {}
    lowered = _prose_only(text).lower()
    for word in words:
        n = len(re.findall(r"(?<!\w)" + re.escape(word) + r"(?!\w)", lowered))
        if n:
            found[word] = n
    return found


def find(original: str, translated: str, cfg: dict) -> dict:
    """Source words left in the translated prose: ``{word: count}``.

    Only words that also appear in the original of the same file are reported, so
    a coincidence in the target language can never trigger the check.
    """
    words = word_list(cfg)
    if not words:
        return {}
    left = _occurrences(translated, words)
    if not left:
        return {}
    in_original = _occurrences(original, set(left))
    return {w: n for w, n in left.items() if in_original.get(w)}


def has_any(block: str, words) -> bool:
    """True if this block still carries one of ``words`` in its plain prose."""
    lowered = _prose_only(block).lower()
    return any(re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", lowered)
               for w in words)


# --- locating the sentence to rewrite -----------------------------------------

_SENTENCE_END = re.compile(r"(?<=[.;:!?])\s+")


def sentences(text: str) -> list:
    """Split plain text into sentences (markup already stripped)."""
    return [s for s in _SENTENCE_END.split(" ".join(text.split())) if s]


def _window(node: str, start: int, end: int) -> tuple:
    """The sentence around ``[start:end]`` within one text node."""
    left = 0
    for m in _SENTENCE_END.finditer(node[:start]):
        left = m.end()
    m = _SENTENCE_END.search(node, end)
    return left, (m.end() if m else len(node))


def occurrences(translated: str, words: set) -> list:
    """Every leftover word as ``(span, fragment, word)``, sentence by sentence.

    The unit is one sentence inside one **text node**, and that is the whole point.
    Measured on this corpus with the same model and the same instruction: asked to
    correct the enclosing block, it returned it byte-identical every time; asked to
    correct the sentence, it fixed half of them. And a text node contains no markup
    at all, so the tags cannot be damaged by construction rather than by validation.
    """
    from .xhtml import TEXT, tokenize  # local: avoids a cycle
    out, seen = [], set()
    for tok in tokenize(translated):
        if tok.kind != TEXT:
            continue
        node = translated[tok.start:tok.end]
        for word in sorted(words):
            for m in re.finditer(r"(?<!\w)" + re.escape(word) + r"(?!\w)",
                                 node, re.I):
                a, b = _window(node, m.start(), m.end())
                span = (tok.start + a, tok.start + b)
                if span in seen:
                    continue      # same sentence: one rewrite clears them all
                seen.add(span)
                out.append((span, node[a:b], word))
    out.sort(key=lambda o: o[0])
    return out
