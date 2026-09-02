"""Configuration: built-in defaults, the user config, a project config, the book.

Layers, lowest to highest — each key comes from the highest layer that sets it:

1. ``DEFAULTS`` below (the same values ``defaults.toml`` documents);
2. the user config, ``~/.config/ai-epub-translator/config.toml`` (see :mod:`paths`);
3. ``./config.toml`` in the current directory, when present — a library folder
   with its own settings;
4. the book's ``book.toml``;
5. the environment: ``AI_EPUB_TRANSLATOR_BASE_URL``, ``AI_EPUB_TRANSLATOR_MODEL``;
6. command-line flags (applied by the CLI).

A minimal dependency-free TOML parser is included (Python 3.9, no tomllib).
"""

from __future__ import annotations

import os
import re

from . import paths

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS_TOML = os.path.join(_HERE, "defaults.toml")

# Per-book files.
BOOK_TOML = "book.toml"
GLOSSARY_NAME = "glossary.toml"

# ISO 639-1 codes for the ``xml:lang``/``lang`` attribute of <html>. A language
# may also be given as its two-letter code; an unknown name falls back to its
# first two letters, with a warning — the code only labels the file.
LANG_CODE = {
    "english": "en", "italian": "it", "french": "fr", "german": "de",
    "spanish": "es", "portuguese": "pt", "bulgarian": "bg", "dutch": "nl",
    "swedish": "sv", "norwegian": "no", "danish": "da", "finnish": "fi",
    "polish": "pl", "czech": "cs", "slovak": "sk", "hungarian": "hu",
    "romanian": "ro", "greek": "el", "turkish": "tr", "russian": "ru",
    "ukrainian": "uk", "serbian": "sr", "croatian": "hr", "slovenian": "sl",
    "catalan": "ca", "galician": "gl", "basque": "eu", "latin": "la",
    "arabic": "ar", "hebrew": "he", "persian": "fa", "hindi": "hi",
    "chinese": "zh", "japanese": "ja", "korean": "ko", "vietnamese": "vi",
    "thai": "th", "indonesian": "id", "malay": "ms", "esperanto": "eo",
}

# Recognized config keys with their defaults (used when missing everywhere).
DEFAULTS = {
    "model": "",
    "base_url": "http://localhost:11434/v1",
    "source_lang": "english",
    "target_lang": "italian",
    "dest_code": "it",
    "register": "tu",
    "truncation_ratio": 0.5,
    "pagebreak_re": 'epub:type="pagebreak"',
    "code_class_hints": [],
    # per-block content checks (catch abbreviated prose)
    "block_text_ratio": 0.7,   # min translated/original text length per block
    "block_text_min": 80,      # skip blocks shorter than this (noise)
    # llm-check: per-side char budget before sampling kicks in (safety net only)
    "check_max_chars": 150000,
    # unit translation (tag protection, see units.py)
    "batch_chars": 16000,   # chars of prose per model call
    "unit_retries": 2,      # single-unit re-asks after a rejected answer
    # per-block LLM attempts (polish, leftovers)
    "block_retries": 2,
    # display mode for the streaming translation
    "progress": "percent",  # stream | percent | both
    # LLM transient-error retries
    "retries": 3,           # attempts on 5xx / connection errors
    "backoff_s": 5,         # base backoff (doubles each attempt)
    "library": "",          # [paths] library — the folder holding the books
    "source_epub": "",      # [source] epub — where the book came from (setup)
}


# --- minimal TOML parser (tables, strings, multiline arrays) ------------------

def _strip_comment(line: str) -> str:
    """Drop ``#`` comments while respecting quoted strings."""
    out, in_str, q = [], False, ""
    for ch in line:
        if in_str:
            out.append(ch)
            if ch == q:
                in_str = False
        elif ch in ('"', "'"):
            in_str, q = True, ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out)


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] in ('"', "'") and s[-1] == s[0]:
        return s[1:-1]
    return s


def _parse_array(text: str) -> list:
    start, end = text.find("["), text.rfind("]")
    body = text[start + 1:end] if start != -1 and end != -1 else text
    return [_unquote(p) for p in body.split(",") if p.strip()]


def parse_toml(text: str) -> dict:
    """Parse a (small subset of) TOML into nested dicts.

    Supports tables, strings, arrays (single- and multi-line) and triple-quoted
    multi-line strings — the last of these carries the glossary notes, which are
    prose and need real line breaks.
    """
    data = {}
    section = data
    buf_key = None
    buf = []
    text_key = None                                   # triple-quoted string open
    text_buf = []
    for raw in text.splitlines():
        if text_key is not None:                      # inside a """ block
            if raw.rstrip().endswith('"""'):
                trimmed = raw.rstrip()[:-3]
                if trimmed:
                    text_buf.append(trimmed)
                section[text_key] = "\n".join(text_buf).strip("\n")
                text_key, text_buf = None, []
            else:
                text_buf.append(raw)
            continue
        line = _strip_comment(raw).strip()
        if not line:
            continue
        if buf_key is not None:                       # multiline array in progress
            buf.append(line)
            if "]" in line:
                section[buf_key] = _parse_array(" ".join(buf))
                buf_key, buf = None, []
            continue
        if line.startswith("[") and line.endswith("]") and "=" not in line:
            section = data.setdefault(line[1:-1].strip(), {})
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            key = _unquote(key.strip())
            val = val.strip()
            if val.startswith('"""'):
                rest = val[3:]
                if rest.endswith('"""') and len(rest) >= 3:   # """one line"""
                    section[key] = rest[:-3]
                else:
                    text_key, text_buf = key, ([rest] if rest else [])
            elif val.startswith("["):
                if "]" in val:
                    section[key] = _parse_array(val)
                else:
                    buf_key, buf = key, [val]
            else:
                section[key] = _unquote(val)
    return data


def _flatten(toml: dict) -> dict:
    """Flatten [model]/[languages]/[register]/[verify]/[detection]/[units]/[repair]/[llm]."""
    model = toml.get("model", {})
    langs = toml.get("languages", {})
    return {
        "model": model.get("model"),
        "base_url": model.get("base_url"),
        "source_lang": langs.get("source"),
        "target_lang": langs.get("target"),
        "dest_code": langs.get("dest_code"),
        "library": toml.get("paths", {}).get("library"),
        "source_epub": toml.get("source", {}).get("epub"),
        "register": toml.get("register", {}).get("default"),
        "truncation_ratio": toml.get("verify", {}).get("truncation_ratio"),
        "pagebreak_re": toml.get("verify", {}).get("pagebreak_re"),
        "block_text_ratio": toml.get("verify", {}).get("block_text_ratio"),
        "block_text_min": toml.get("verify", {}).get("block_text_min"),
        "check_max_chars": toml.get("verify", {}).get("check_max_chars"),
        "code_class_hints": toml.get("detection", {}).get("code_class_hints"),
        "batch_chars": toml.get("units", {}).get("batch_chars"),
        "unit_retries": toml.get("units", {}).get("retries"),
        "block_retries": toml.get("repair", {}).get("block_retries"),
        "progress": toml.get("ui", {}).get("progress"),
        "retries": toml.get("llm", {}).get("retries"),
        "backoff_s": toml.get("llm", {}).get("backoff_s"),
    }


def _load(path: str) -> dict:
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return _flatten(parse_toml(f.read()))


def load_user() -> dict:
    """The user config (``paths.user_config_path()``), flattened; ``{}`` if absent."""
    return _load(paths.user_config_path())


def load_book(book_dir: str) -> dict:
    """Load and flatten the per-book config (``<book>/book.toml``)."""
    return _load(os.path.join(book_dir, BOOK_TOML)) if book_dir else {}


def _env_layer() -> dict:
    return {"base_url": os.environ.get("AI_EPUB_TRANSLATOR_BASE_URL"),
            "model": os.environ.get("AI_EPUB_TRANSLATOR_MODEL")}


def glossary_path(book_dir: str) -> str:
    return os.path.join(book_dir, GLOSSARY_NAME)


def load_glossary(book_dir: str) -> dict:
    """Load ``<book>/glossary.toml`` as ``{source_term: target_term}``.

    The glossary pins terms the model gets systematically wrong (e.g. French
    *exotérisme* rendered as "esoterismo", its opposite). Absent file -> empty
    dict, i.e. no effect anywhere.
    """
    p = glossary_path(book_dir)
    if not os.path.isfile(p):
        return {}
    with open(p, encoding="utf-8") as f:
        data = parse_toml(f.read())
    terms = data.get("terms") or {}
    return {k: v for k, v in terms.items() if k and v}


def load_glossary_exceptions(book_dir: str) -> dict:
    """``{source_term: [phrase, ...]}`` from ``[exceptions]``: contexts to skip.

    A term pinned for the prose can be wrong in a proper name: "archetypal" ->
    "archetipico" everywhere, except inside the journal title "Archetypal
    Psychology", which stays in English. A block that carries the term only
    inside one of its exception phrases is not checked for that term.
    """
    p = glossary_path(book_dir)
    if not os.path.isfile(p):
        return {}
    data = parse_toml(open(p, encoding="utf-8").read())
    out = {}
    for term, phrases in (data.get("exceptions") or {}).items():
        phrases = phrases if isinstance(phrases, list) else [phrases]
        out[term] = [ph for ph in phrases if ph]
    return out


def load_glossary_notes(book_dir: str) -> str:
    """Load the book's free-form terminology notes from ``[notes] text``.

    A mapping says *what* to write; the notes say *why* a term is a trap ("these
    two look alike but mean the opposite"). They ride along with the glossary into
    every translation prompt. Absent -> empty string, i.e. no effect.
    """
    p = glossary_path(book_dir)
    if not os.path.isfile(p):
        return ""
    with open(p, encoding="utf-8") as f:
        data = parse_toml(f.read())
    return (data.get("notes", {}).get("text") or "").strip()


def save_glossary(book_dir: str, terms: dict, notes: str = None) -> str:
    """Write the glossary back, sorted, with a short explanatory header.

    ``notes`` and ``[exceptions]`` default to whatever the file already holds,
    so adding a term never silently drops them.
    """
    p = glossary_path(book_dir)
    exceptions = load_glossary_exceptions(book_dir)
    if notes is None:
        notes = load_glossary_notes(book_dir)
    lines = [
        "# Terms this model gets wrong every time — pinned by hand.",
        "# Used in the prompt (prevention), checked per block (detection) and",
        "# applied by repair (correction).",
        "",
        "[terms]",
    ]
    for src in sorted(terms):
        lines.append(f'"{src}" = "{terms[src]}"')
    if exceptions:
        lines += ["", "# Contexts where a term must NOT be rendered (a proper name,",
                  "# a title): a block carrying the term only inside one of these",
                  "# phrases is not checked for it.", "[exceptions]"]
        for src in sorted(exceptions):
            listed = ", ".join(f'"{ph}"' for ph in exceptions[src])
            lines.append(f'"{src}" = [{listed}]')
    if notes:
        lines += [
            "",
            "# Free-form notes: the *why* behind a term — a trap to avoid, a",
            "# distinction to keep. They ride into every translation prompt.",
            "[notes]",
            'text = """',
            notes,
            '"""',
        ]
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return p


def lang_code(name: str) -> str:
    """ISO code for a language name or code; unknown names fall back to 2 letters."""
    key = str(name or "").strip().lower()
    if key in LANG_CODE:
        return LANG_CODE[key]
    if re.fullmatch(r"[a-z]{2}", key):
        return key
    return key[:2] or "xx"


def layers(book_dir: str = None) -> list:
    """``[(name, path, values)]`` from lowest to highest precedence."""
    return [
        ("defaults", DEFAULTS_TOML, dict(DEFAULTS)),
        ("user", paths.user_config_path(), load_user()),
        ("book", os.path.join(book_dir, BOOK_TOML) if book_dir else "", load_book(book_dir)),
        ("env", "AI_EPUB_TRANSLATOR_*", _env_layer()),
    ]


def merged_config(book_dir: str = None, with_sources: bool = False) -> dict:
    """The effective config for a book (or, without one, for ``setup``).

    Each key takes the value of the highest layer that sets it (see the module
    docstring). ``with_sources=True`` adds ``_sources``: ``{key: layer_name}``,
    what ``config show`` prints.
    """
    cfg, sources = dict(DEFAULTS), {k: "defaults" for k in DEFAULTS}
    for name, _path, values in layers(book_dir)[1:]:
        for k, v in values.items():
            if k in cfg and v not in (None, "", []):
                cfg[k] = v
                sources[k] = name
    # dest_code is derived from target_lang (single source of truth): a book
    # overriding ``target`` must get a matching ``dest_code``.
    cfg["dest_code"] = lang_code(cfg["target_lang"])
    # the per-book glossary travels in cfg: prompts, checks and repair all use it
    cfg["glossary"] = load_glossary(book_dir) if book_dir else {}
    cfg["glossary_exceptions"] = load_glossary_exceptions(book_dir) if book_dir else {}
    cfg["glossary_notes"] = load_glossary_notes(book_dir) if book_dir else ""
    # type coercion
    cfg["truncation_ratio"] = float(cfg["truncation_ratio"])
    cfg["block_text_ratio"] = float(cfg["block_text_ratio"])
    for _k in ("batch_chars", "unit_retries", "block_retries", "retries",
               "backoff_s", "block_text_min", "check_max_chars"):
        cfg[_k] = int(cfg[_k])
    if with_sources:
        cfg["_sources"] = sources
    return cfg


def code_grep(hints: list) -> str:
    """Build a regex (alternation) counting code occurrences by CSS class."""
    return "|".join(f'class="{re.escape(h)}"' for h in hints) if hints else ""
