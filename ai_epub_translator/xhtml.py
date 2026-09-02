"""Tolerant XHTML lexer.

The translator model sometimes emits broken markup (a missing ``</span>``, a
stray ``&``, a ``<br>`` left unclosed). A strict XML parser stops at the first
error and tells us nothing about the rest. This module tokenizes XHTML with a
single regex that **never fails**: it produces a flat list of tokens, each
carrying its character span in the source, even when the markup is malformed.

The lexer is the foundation for:

* :mod:`structdiff` — structural alignment original vs translated;
* :mod:`repair`    — localizing and splicing fixes.

Everything operates on ``str`` (never re-serialized) so that byte-level details
the serializer would rewrite (numeric entities, CRLF, attribute order) survive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Void elements: never have a closing tag; canonicalized as a single "void"
# event regardless of a trailing slash (so ``<br>`` and ``<br/>`` are equal).
VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}

# Block-level elements. Everything else is treated as inline. A run of inline
# content between two block boundaries is the unit of prose translation/repair.
BLOCK = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td", "th", "dt", "dd",
    "caption", "figcaption", "blockquote", "title", "div", "table", "tr",
    "thead", "tbody", "tfoot", "colgroup", "ol", "ul", "nav", "section",
    "article", "header", "footer", "body", "head", "hr",
}

# Attribute names that may legitimately change between original and translated
# (the language switch); excluded from attribute comparison.
LANG_ATTRS = {"lang", "xml:lang"}

# Token kinds.
OPEN, CLOSE, VOID_K, TEXT, COMMENT, DECL, PI, CDATA, LT = (
    "open", "close", "void", "text", "comment", "decl", "pi", "cdata", "lt",
)

# Master tokenizer. Alternation order matters: comments/CDATA/decl/PI are tried
# before generic open/close so their content is never mis-parsed. The ``open``
# attribute scan excludes ``>`` outside quotes, so a start tag ends at the first
# unquoted ``>``; a stray ``<`` (e.g. de-escaped ``a < b``) falls through to LT.
_TOKEN_RE = re.compile(
    r"""
      (?P<comment><!--.*?-->)
    | (?P<cdata><!\[CDATA\[.*?\]\]>)
    | (?P<decl><![^>]*>)
    | (?P<pi><\?.*?\?>)
    | (?P<close></\s*(?P<close_name>[A-Za-z][\w:.\-]*)\s*>)
    | (?P<open><(?P<open_name>[A-Za-z][\w:.\-]*)(?P<open_attrs>(?:[^>"']|"[^"]*"|'[^']*')*?)(?P<open_self>/)?>)
    | (?P<lt><)
    | (?P<text>[^<]+)
    """,
    re.S | re.X,
)

_ATTR_RE = re.compile(
    r"""([A-Za-z_:][\w:.\-]*)        # name
        (?:\s*=\s*                   # optional value (applies to all forms below)
            (?:
                "(?P<dq>[^"]*)"|     # double-quoted
                '(?P<sq>[^']*)'|     # single-quoted
                (?P<bare>[^\s>]+)    # bare / unquoted
            )
        )?""",
    re.X,
)


@dataclass
class Token:
    """A single lexical unit with its character span in the source string."""

    kind: str
    raw: str = ""
    name: str = ""                  # tag name (open/close/void), lowercased
    start: int = 0                  # char offset, inclusive
    end: int = 0                    # char offset, exclusive
    attrs: list = field(default_factory=list)   # [(name, value)] for open/void
    self_close: bool = False

    @property
    def is_block(self) -> bool:
        return self.kind in (OPEN, CLOSE, VOID_K) and self.name in BLOCK


def is_void(name: str) -> bool:
    return name.lower() in VOID


def is_block(name: str) -> bool:
    return name.lower() in BLOCK


def parse_attrs(attr_str: str) -> list:
    """Parse an attribute string into a list of ``(name, value)`` tuples.

    Values are empty strings for boolean attributes. Quoting style is
    normalized away (``a='x'`` and ``a="x"`` are equal).
    """
    attrs = []
    for m in _ATTR_RE.finditer(attr_str):
        name = m.group(1)
        if m.group("dq") is not None:
            val = m.group("dq")
        elif m.group("sq") is not None:
            val = m.group("sq")
        elif m.group("bare") is not None:
            val = m.group("bare")
        else:
            val = ""
        attrs.append((name, val))
    return attrs


def attr_signature(token: Token) -> tuple:
    """Normalized attribute signature for structural comparison.

    Sorted by name, with ``lang``/``xml:lang`` removed (they may legitimately
    change on translation). Two elements with the same tag and same normalized
    attributes compare equal.
    """
    pairs = sorted((n, v) for (n, v) in token.attrs if n not in LANG_ATTRS)
    return tuple(pairs)


def tokenize(text: str) -> list:
    """Tokenize ``text`` into a list of :class:`Token` (never raises).

    The whole input is consumed: a stray ``<`` becomes an ``LT`` token and a
    malformed tag falls back to text, so tokenization always terminates.
    """
    tokens = []
    for m in _TOKEN_RE.finditer(text):
        kind = m.lastgroup
        start, end = m.start(), m.end()
        raw = text[start:end]
        if kind == "text" or kind == "comment" or kind == "decl" \
                or kind == "pi" or kind == "cdata" or kind == "lt":
            tokens.append(Token(kind, raw=raw, start=start, end=end))
        elif kind == "close":
            name = m.group("close_name").lower()
            tokens.append(Token(CLOSE, raw=raw, name=name, start=start, end=end))
        elif kind == "open":
            name = m.group("open_name").lower()
            attrs = parse_attrs(m.group("open_attrs") or "")
            self_close = bool(m.group("open_self"))
            if is_void(name):
                tokens.append(Token(VOID_K, raw=raw, name=name, start=start,
                                    end=end, attrs=attrs))
            else:
                tokens.append(Token(OPEN, raw=raw, name=name, start=start,
                                    end=end, attrs=attrs, self_close=self_close))
    return tokens
