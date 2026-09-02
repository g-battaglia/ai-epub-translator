"""Deterministic quality checks for a translated file.

Compares the translated text (in memory) against the original. Hard-fail: every
check must pass before the file is written back to ``target/``.

The heavy structural alignment (tags + attributes, original vs translated) lives
in :mod:`structdiff` and is attached here as the ``struct`` check, which callers
enable via ``run_struct=True``.
"""

from __future__ import annotations

import re
import xml.dom.minidom as minidom

# Tags whose count must match between original and translated.
TAG_RE = r'<p |<div |<em>|</em>|<strong>|</strong>|<a |<h[1-6] '
ATTR_RE = {
    "id": re.compile(r'\sid="([^"]*)"'),
    "href": re.compile(r'\shref="([^"]*)"'),
}


def _safe_parse(text: str) -> tuple:
    """Parse with minidom; return (ok, error_message)."""
    try:
        minidom.parseString(text)
        return True, ""
    except Exception as e:                          # parse error → mismatched tag etc.
        return False, str(e)


def _counts(text: str, pattern: str) -> int:
    return len(re.findall(pattern, text))


def _attr_set(text: str, attr: str) -> list:
    return sorted(ATTR_RE[attr].findall(text))


def _lang_ok(text: str, dest_code: str) -> bool:
    """True if the <html> tag carries ``lang="<dest_code>"``."""
    m = re.search(r"<html\b[^>]*>", text)
    if not m:
        return False
    tag = m.group(0)
    return f'lang="{dest_code}"' in tag or f"lang='{dest_code}'" in tag


def verify_file(original: str, translated: str, cfg: dict,
                run_struct: bool = True) -> dict:
    """Run all deterministic checks; return ``{checks, passed, score, reasons}``.

    With ``run_struct=True`` the structural diff is appended as the ``struct``
    check. Callers inside :mod:`structdiff` pass ``run_struct=False`` to avoid
    recursion (structdiff is the authority on structure, not verify on itself).
    """
    checks, reasons = {}, []
    dest_code = cfg.get("dest_code", "it")
    hints = cfg.get("code_class_hints") or []
    pagebreak_re = cfg.get("pagebreak_re") or 'epub:type="pagebreak"'
    trunc = float(cfg.get("truncation_ratio") or 0.5)

    def fail(name, why):
        checks[name] = False
        reasons.append(f"{name}: {why}")

    def ok(name):
        checks[name] = True

    # 1. well-formed XHTML
    good, err = _safe_parse(translated)
    ok("xhtml") if good else fail("xhtml", err)

    # 2. key-tag counts match
    co, ct = _counts(original, TAG_RE), _counts(translated, TAG_RE)
    ok("tags") if co == ct else fail("tags", f"orig={co} trad={ct}")

    # 3. code intact (when configured)
    if hints:
        cg = "|".join(f'class="{re.escape(h)}"' for h in hints)
        co2, ct2 = _counts(original, cg), _counts(translated, cg)
        ok("code") if co2 == ct2 else fail("code", f"orig={co2} trad={ct2}")
    else:
        ok("code")

    # 4. page-break markers preserved
    po, pt = _counts(original, pagebreak_re), _counts(translated, pagebreak_re)
    ok("pagebreak") if po == pt else fail("pagebreak", f"orig={po} trad={pt}")

    # 5. ids and hrefs preserved
    ok("ids") if _attr_set(original, "id") == _attr_set(translated, "id") \
        else fail("ids", "id set differs")
    ok("hrefs") if _attr_set(original, "href") == _attr_set(translated, "href") \
        else fail("hrefs", "href set differs")

    # 6. xml:lang/lang updated
    ok("lang") if _lang_ok(translated, dest_code) \
        else fail("lang", f'missing lang="{dest_code}" on <html>')

    # 7. no truncation
    ok("length") if len(translated) >= trunc * len(original) \
        else fail("length", f"translated {len(translated)} < {trunc*100:.0f}% of orig {len(original)}")

    # 8. structural diff original vs translated (tags + attrs aligned 1:1)
    if run_struct:
        from .structdiff import analyze
        defects = analyze(original, translated, cfg).defects
        if defects:
            fail("struct", "; ".join(d.detail for d in defects[:3]))
        else:
            ok("struct")

    passed = all(checks.values())
    score = round(100 * sum(checks.values()) / len(checks))
    return {"checks": checks, "passed": passed, "score": score, "reasons": reasons}
