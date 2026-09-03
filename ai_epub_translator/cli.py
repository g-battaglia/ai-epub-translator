"""Command-line interface for the EPUB translation harness.

Subcommands
-----------
run <slug|folder>             translate + verify + retry + judge + EPUB (orchestrates the rest)
setup <epub>                  configure a new book end-to-end (slug auto-derived)
list                          list translatable books (also ``-l`` / ``--list``)
path [slug]                   the library, or one book's folder
glossary <slug>               pinned terms; --suggest/--extract/--add to build it
repair <slug> [file]          ask again for the units of a file that failed
polish <slug> [file]          rewrite the units the quality gate flagged (kept only if better)
redo <slug> <file...>         queue files for a fresh translation (drop done+cache)
verify <slug> [file] [--fix]  run the deterministic checks (no LLM); --fix requeues
units <slug> <file>           show a file as the model sees it (segments + placeholders)
llm-check <slug> [file]       judge translation accuracy per file via the LLM
check-all [slug] [--llm]      overall health report (all books, or one)
pack <slug> [--open]          pack target/ into a ready-to-read EPUB
status <slug>                 progress, failed units, tokens

Per-file pipeline (``translate``)::

    original ──segment──▶ units ──LLM (prose only)──▶ answers ──splice──▶ file
                 │                        │ (a unit rejected)              │
                 └ skeleton, verbatim     └ asked again, alone        verify ──▶ save

The model never sees markup (see :mod:`units`): every unit that validates is
cached at once, a rejected one is re-asked on its own with the reason, and the
file is saved only when every unit is in and the whole passes :mod:`verify`.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import sys

from . import __version__, leftovers, llm, logs
from . import paths as P
from . import units as U
from .cache import Cache
from .config import BOOK_TOML, layers, load_user, merged_config, save_glossary
from .epub import find_opf, pack_epub, read_spine, unpack_epub
from .llm import check_translation, extract_glossary, translate_text
from .repair import fix_leftovers, match_line_ending, polish_file, rewrite_lang
from .state import load as load_state
from .state import mark_done, mark_failed
from .state import save as save_state
from .structdiff import GLOSSARY, _fold, analyze, glossary_conflicts
from .ui import MODES as PROGRESS_MODES
from .ui import Progress
from .verify import verify_file
from .xhtml import TEXT as X_TEXT
from .xhtml import tokenize

PROG = "ai-epub-translator"
# The library: one folder per book. Resolved once at startup (flag > env > user
# config > XDG data dir); tests point it at a temp dir.
BOOKS = P.library_dir(configured=load_user().get("library"))

# Thin rule that brackets the streamed model output so the per-file header and the
# result line stay visually distinct from the (potentially long) token stream.
RULE = "─" * 64


def book_dir(slug: str) -> str:
    return os.path.join(BOOKS, slug)


def _require_book(slug: str) -> str:
    bd = book_dir(slug)
    if not os.path.isfile(os.path.join(bd, BOOK_TOML)):
        sys.exit(f"Book not found: {bd}\n"
                 f"Set it up with '{PROG} setup <file.epub>'; '{PROG} list' shows "
                 f"the books in {BOOKS}.")
    return bd


def _write_book_toml(bd: str, source: str, target: str, epub: str = "") -> None:
    """Write the per-book config: the language pair and where the book came from."""
    with open(os.path.join(bd, BOOK_TOML), "w", encoding="utf-8") as f:
        f.write("# This book's settings; anything in the user config can be "
                "overridden here.\n"
                f'[languages]\nsource = "{source}"\ntarget = "{target}"\n')
        if epub:
            f.write(f'\n[source]\nepub = "{epub}"\n')


def _slugify(filename: str) -> str:
    """Derive a book slug from a filename: lower-case, non-alnum runs to ``-``."""
    base = os.path.splitext(os.path.basename(filename))[0]
    slug = re.sub(r"[^A-Za-z0-9]+", "-", base).strip("-").lower()
    return slug or "book"


def _rel(path: str, target: str) -> str:
    """A spine file's key: relative to target/, with '/' on every platform."""
    return os.path.relpath(path, target).replace(os.sep, "/")


def _read_original(bd: str, rel: str) -> str:
    """Read the snapshot original for a spine file (fallback to target)."""
    p = os.path.join(P.original(bd), rel)
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            return f.read()
    with open(os.path.join(P.target(bd), rel), encoding="utf-8") as f:
        return f.read()


# --- subcommands --------------------------------------------------------------

def cmd_setup(args):
    """Configure a new book from an EPUB in one step.

    Derives the slug from the EPUB filename when ``--slug`` is not given, unpacks
    into ``<library>/<slug>/.work/target``, snapshots ``original/``, writes
    ``book.toml`` (the language pair — from the flags or the user config — and
    where the EPUB came from), and prints the next command to run.
    """
    epub = os.path.abspath(os.path.expanduser(args.epub))
    if not os.path.isfile(epub):
        sys.exit(f"EPUB not found: {epub}")
    cfg = merged_config()
    source = args.source or cfg["source_lang"]
    target_lang = args.target or cfg["target_lang"]
    slug = args.slug or _slugify(epub)
    bd = book_dir(slug)
    if os.path.exists(bd):
        sys.exit(f"Already exists: {bd} (use --slug to pick another, or remove it)")
    os.makedirs(P.work(bd))
    unpack_epub(epub, P.target(bd))
    shutil.copytree(P.target(bd), P.original(bd))    # snapshot for verify
    _write_book_toml(bd, source, target_lang, epub)
    n = len(read_spine(find_opf(P.target(bd))))
    print(f"Set up '{slug}' from {os.path.basename(epub)}")
    print(f"  folder    : {bd}")
    print(f"  spine     : {n} file(s)")
    print(f"  languages : {source} → {target_lang}")
    # The glossary is the one thing a machine cannot decide for you, and the one
    # error class no structural check can see: pin the terms before spending tokens.
    print("\nNext — 1. the glossary (do this first: a term the model gets wrong")
    print("   every time is invisible to every other check):")
    print(f"     {PROG} glossary {slug} --suggest    look for risky terms")
    print(f"     {PROG} glossary {slug} --extract    or ask the LLM")
    print(f'     {PROG} glossary {slug} --add "term=rendering"')
    print("\n   2. then translate the whole book:")
    print(f"     {PROG} run {slug}")


def _default_epub_path(bd: str, slug: str, cfg: dict) -> str:
    """Default output: ``<library>/<slug>/<slug>.<lang>.epub`` — next to book.toml."""
    return os.path.join(bd, f"{slug}.{cfg.get('dest_code') or 'xx'}.epub")


def _pack_and_announce(bd: str, slug: str, cfg: dict, out: str = None,
                       open_it: bool = False) -> str:
    """Pack ``target/`` into an EPUB and print where to find/open it."""
    out = out or _default_epub_path(bd, slug, cfg)
    path = pack_epub(P.target(bd), out)
    size_kb = max(1, os.path.getsize(path) // 1024)
    print(f"\n✓ EPUB ready: {path}  ({size_kb} KB)")
    if open_it:
        _open_path(path)
    else:
        opener = "open" if sys.platform == "darwin" else (
            "xdg-open" if sys.platform.startswith("linux") else "start")
        print(f"  open it with: {opener} {path}")
    return path


def _translate_metadata(bd: str, slug: str, cfg: dict, state: dict) -> None:
    """Translate the OPF book metadata (``dc:title`` and ``dc:description``).

    These are outside the spine and never reach the normal pipeline, yet the EPUB
    reader shows them as the book title/description. Runs once (``meta_done``).
    """
    if state.get("meta_done"):
        return
    opf_path = find_opf(P.target(bd))
    with open(opf_path, encoding="utf-8") as f:
        opf = f.read()
    fields = ("dc:title", "dc:description")
    pattern = re.compile(r"<(" + "|".join(fields) + r")([^>]*)>(.*?)</\1>",
                         re.S)

    def repl(m):
        body = m.group(3)
        translated = translate_text(body, cfg, cfg["base_url"], cfg["model"])
        return f"<{m.group(1)}{m.group(2)}>{translated}</{m.group(1)}>"

    new_opf = pattern.sub(repl, opf)
    if new_opf != opf:
        # The translated metadata is raw model output. Never write an OPF that no
        # longer parses: a bare '&' or a stray preamble would break ET.parse in
        # read_spine — and read_spine backs nearly every command, so one bad title
        # would brick run/pack/verify/status. Validate first; keep the original on
        # failure (untranslated metadata is cosmetic; a broken OPF is not).
        import xml.etree.ElementTree as ET
        try:
            ET.fromstring(new_opf.encode("utf-8"))
        except ET.ParseError as e:
            print(f"  ⚠ metadata not translated (the resulting OPF is not valid: {e})")
            return
        with open(opf_path, "w", encoding="utf-8") as f:
            f.write(new_opf)
        print("  ↻ OPF metadata translated (title + description)")
    state["meta_done"] = True
    save_state(bd, state)


def _open_path(path: str) -> None:
    """Open a file with the OS default application."""
    import subprocess
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        elif sys.platform == "win32":
            os.startfile(path)                              # noqa: S606
        else:
            subprocess.run(["xdg-open", path], check=False)
    except Exception as e:                           # noqa: BLE001 — best-effort
        print(f"  (could not open it automatically: {e})")


def cmd_pack(args):
    """Pack the translated ``target/`` into a ready-to-read EPUB."""
    bd = _require_book(args.slug)
    cfg = merged_config(bd)
    state = load_state(bd)
    target = P.target(bd)
    files = read_spine(find_opf(target))
    if len(state.get("done", [])) < len(files):
        print(f"Warning: {len(state.get('done', []))}/{len(files)} file(s) translated "
              f"— the EPUB will be incomplete.")
    _pack_and_announce(bd, args.slug, cfg, out=args.out, open_it=args.open)


def _unit_spans_to_retry(original: str, current: str, cfg: dict,
                         orig_sk, cur_sk) -> set:
    """Indices of the units of a saved file that no longer pass the checks.

    A file in ``target/`` starts failing when the rules change — typically a
    glossary term added after it was translated. Its units align 1:1 with the
    original's (it passed the structural gate), so only the units the defects
    fall in are sent back; the rest keep their translation. A defect that has no
    span to point at (a parse error) sends the whole file.
    """
    spans, everything = [], False
    for d in analyze(original, current, cfg).defects:
        span = d.block_trad or ((d.trad_start, d.trad_end)
                                if d.trad_start >= 0 else None)
        if span:
            spans.append(span)
        else:
            everything = True
    if everything:
        return {u.idx for u in orig_sk.translatable}
    return U.units_at(cur_sk, spans)


def _units_failing_verify(original: str, text: str, cfg: dict, sk) -> list:
    """``[(idx, reason)]`` of the units the structural check blames in ``text``.

    ``text`` is ``sk`` reassembled, so its segmentation yields the same unit
    indices. A defect that has no span maps to every unit.
    """
    out_sk = U.segment(text, cfg)
    blamed: dict = {}
    for d in analyze(original, text, cfg).defects:
        span = d.block_trad or ((d.trad_start, d.trad_end)
                                if d.trad_start >= 0 else None)
        idxs = U.units_at(out_sk, [span]) if span else {u.idx for u in sk.translatable}
        for idx in idxs:
            blamed.setdefault(idx, d.detail)
    return sorted(blamed.items())


def _process_file(bd: str, slug: str, rel: str, cfg: dict, *,
                  translate_it: bool, cache: Cache):
    """Translate (or complete) one file unit by unit, then verify and save.

    Every unit that validates is cached the instant the model returns it, so a
    crash or a Ctrl-C costs at most one batch. ``translate_it=False`` completes a
    file from its cache — or, for a file already in ``target/`` that stopped
    passing, re-asks only the units that fail. Returns ``True`` on done,
    ``"abort"`` if the LLM is unreachable.
    """
    target_path = os.path.join(P.target(bd), rel)
    original_text = _read_original(bd, rel)
    orig_hash = Cache.hash_text(original_text)
    state = load_state(bd)
    sk = U.segment(original_text, cfg)
    todo = sk.translatable
    inners, prior = {}, {}

    cached = cache.units(rel, orig_hash)
    for idx, row in cached.items():
        prior[idx] = (row["attempts"] or 0, row["reason"] or "")
        if row["status"] == "ok" and row["text"] is not None:
            inners[idx] = row["text"]
    # A file that was saved and later stopped passing (a rule changed) keeps
    # its units and re-asks only the failing ones. Only a DONE file qualifies:
    # a pending file's target/ is still the untranslated original, and pairing
    # its units would keep the source language as if it were a translation.
    if (not translate_it and not cached and rel in state.get("done", [])
            and os.path.isfile(target_path)):
        with open(target_path, encoding="utf-8") as f:
            current = f.read()
        cur_sk = U.segment(current, cfg)
        if U.aligned(sk, cur_sk):
            retry = _unit_spans_to_retry(original_text, current, cfg, sk, cur_sk)
            for u in cur_sk.translatable:
                if u.idx not in retry:
                    inners[u.idx] = U.inner_of(cur_sk, u)
        else:
            print(f"\n[{rel}] the saved file's units do not align with the "
                  "original — translating it whole")

    pending = [u for u in todo if u.idx not in inners]
    what = "translating" if translate_it else "repairing"
    print(f"\n[{rel}] {what}… {len(pending)} of {len(todo)} unit(s)"
          + (" from cache" if not pending else ""))
    toks = {"prompt_tokens": 0, "completion_tokens": 0}
    failed = []
    if pending:
        print(RULE, flush=True)
        prog = Progress(cfg.get("progress") or "percent", total=1)
        raw_chat = llm.default_chat(cfg)

        def chat(prompt, max_tokens, **kw):
            res = raw_chat(prompt, max_tokens, **kw)
            toks["prompt_tokens"] += res.get("prompt_tokens", 0)
            toks["completion_tokens"] += res.get("completion_tokens", 0)
            return res

        def on_unit(u, inner, reason):
            cache.put_unit(rel, u.idx, orig_hash, inner,
                           "ok" if inner is not None else "fail", reason)

        try:
            res = llm.translate_units(pending, cfg, chat, progress=prog,
                                      on_unit=on_unit, history=prior,
                                      total=len(todo), done=len(inners))
        except RuntimeError as e:
            print(RULE)
            mark_failed(state, rel, f"LLM error: {e}")
            save_state(bd, state)
            logs.text(bd, slug, f"{rel} ERROR ({e})")
            logs.jsonl(bd, {"file": rel, "status": "error", "error": str(e)})
            # A down/unreachable server aborts the whole run; a per-file error
            # (a one-off timeout) only skips this file. Everything answered so
            # far is already in the cache.
            fatal = any(w in str(e).lower()
                        for w in ("unreachable", "refused", "connection"))
            return "abort" if fatal else False
        print(RULE, flush=True)
        for idx, (inner, why) in res.items():
            if inner is not None:
                inners[idx] = inner
            else:
                failed.append((idx, why))

    text = U.reassemble(sk, inners)
    text = rewrite_lang(text, cfg.get("dest_code", "it"))
    text = match_line_ending(text, original_text)
    ver = verify_file(original_text, text, cfg)
    if not ver["passed"] and not failed:
        # Every unit validated on its own, yet the whole does not — a relaxed
        # last attempt accepted reordered placeholders the structural check
        # rejects. Map the defects back onto their units and send those back,
        # rather than blaming the harness and stopping.
        for idx, why in _units_failing_verify(original_text, text, cfg, sk):
            cache.put_unit(rel, idx, orig_hash, None, "fail", why)
            failed.append((idx, why))
    reasons = list(ver["reasons"])
    if failed:
        listed = "; ".join(f"unit {idx}: {why}" for idx, why in failed[:3])
        reasons.insert(0, f"{len(failed)} unit(s) not translated ({listed})")
    passed = ver["passed"] and not failed
    cache.put_file(rel, orig_hash, text, "translated" if passed else "fail",
                   toks["prompt_tokens"], toks["completion_tokens"])
    rec = {"file": rel, "status": "pass" if passed else "fail",
           "score": ver["score"], "checks": ver["checks"],
           "units": len(todo), "units_failed": len(failed), **toks}

    if passed:
        with open(target_path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        mark_done(state, rel)
        save_state(bd, state)        # persist 'done' BEFORE dropping the recovery
        cache.done(rel)              # rows, or a crash in between re-translates it
        print(f"  ✓ OK (score {ver['score']}, {len(todo)} unit(s))")
        logs.text(bd, slug, f"{rel} OK score={ver['score']} units={len(todo)}")
    else:
        mark_failed(state, rel, "; ".join(reasons))
        if failed:
            print(f"  ✗ FAIL — {len(failed)} unit(s) to redo "
                  f"(the rest is done and kept in the cache):")
            for idx, why in failed[:5]:
                print(f"      unit {idx}: {why}")
        else:
            print(f"  ✗ FAIL (score {ver['score']}): " + "; ".join(ver["reasons"]))
        logs.text(bd, slug, f"{rel} FAIL score={ver['score']} " + "; ".join(reasons))
    logs.jsonl(bd, rec)
    save_state(bd, state)
    return passed


def _plan_pass(bd: str, cfg: dict, rels: list, cache: Cache) -> tuple:
    """``(to_translate, to_repair)`` for one pass over the spine.

    Two sources of repair work: a file whose cached units are not all in, and a
    file already saved in ``target/`` that no longer passes (a glossary change).
    """
    state = load_state(bd)
    done = set(state.get("done", []))
    pending = set(cache.pending())
    to_translate = [r for r in rels if r not in done and r not in pending]
    to_repair = [r for r in rels
                 if r in pending or (r in done and _needs_repair(bd, cfg, r))]
    return to_translate, to_repair


def _run_pass(bd: str, slug: str, cfg: dict, to_translate: list,
              to_repair: list, cache: Cache) -> bool:
    """Process one pass; ``False`` when the LLM became unreachable."""
    for rel in to_translate:
        if _process_file(bd, slug, rel, cfg, translate_it=True,
                         cache=cache) == "abort":
            return False
    for rel in to_repair:
        if _process_file(bd, slug, rel, cfg, translate_it=False,
                         cache=cache) == "abort":
            return False
    return True


def _resolve_book(arg: str) -> str:
    """Resolve a book folder or slug to its slug. The book must already exist.

    Setting a book up is deliberately a separate step ('setup <epub>'): it is
    where languages and glossary get reviewed, before any token is spent.
    """
    path = os.path.abspath(os.path.expanduser(arg))
    if os.path.isdir(path) and os.path.isfile(os.path.join(path, BOOK_TOML)):
        return os.path.basename(path.rstrip(os.sep))
    if os.path.isdir(book_dir(arg)):
        return arg
    hint = f"  ai-epub-translator setup {arg}" if arg.endswith((".epub", ".zip")) \
        else "  ai-epub-translator setup <file.epub>     to set a book up\n" \
             "  ai-epub-translator list                  to see the books you have"
    sys.exit(f"'{arg}' is not a book that has been set up.\n{hint}")


def cmd_run(args):
    """Run a configured book to completion: translate, verify, retry, pack.

    The book must already be set up ('setup <epub>'), so that its languages and
    glossary are reviewed before any token is spent. Each pass translates what is
    missing and asks again for the units that failed. It stops when a pass no
    longer changes anything, and is resume-safe: run it again after a crash — or
    after editing the glossary — and it picks up from the units it still lacks.
    """
    slug = _resolve_book(args.book)
    bd = _require_book(slug)
    cfg = merged_config(bd)
    if args.base_url:
        cfg["base_url"] = args.base_url
    if args.model:
        cfg["model"] = args.model
    if args.progress:
        cfg["progress"] = args.progress

    target = P.target(bd)
    files = read_spine(find_opf(target))
    total = len(files)
    rels = [_rel(p, target) for p in files]
    max_passes = max(1, int(args.passes))

    print(f"\n{'=' * 60}")
    print(f"{slug}  ({cfg['source_lang']} → {cfg['target_lang']})  ·  {total} chapters")
    print(f"{cfg['model']} @ {cfg['base_url']}")
    glossary = cfg.get("glossary") or {}
    print(f"glossary: {len(glossary)} term(s)" if glossary else
          "glossary: none — a systematically wrong term will go unnoticed "
          f"(see 'glossary {slug} -h')")
    print(f"{'=' * 60}")

    previous = None
    for pass_i in range(1, max_passes + 1):
        with Cache(bd) as cache:
            todo_translate, todo_repair = _plan_pass(bd, cfg, rels, cache)
            if not todo_translate and not todo_repair:
                break                                    # nothing left to do
            print(f"\n── pass {pass_i}/{max_passes}: "
                  f"{len(todo_translate)} to translate, {len(todo_repair)} to complete")
            if not _run_pass(bd, slug, cfg, todo_translate, todo_repair, cache):
                print("\n✗ LLM unreachable — stopping. Everything done so far is "
                      "cached: run this again when the server is back.")
                return 1

        # Converged? A pass that leaves the same files unresolved with the same
        # units failed changed nothing; asking the model the same things again
        # would change nothing either. Retries within a file are already
        # bounded per unit (units.retries), so this loop only has to notice.
        state = load_state(bd)
        done_now = set(state.get("done", []))
        bad = [r for r in rels if r not in done_now or _needs_repair(bd, cfg, r)]
        with Cache(bd) as cache:
            failed_units = sum(cache.unit_status(r)[1] for r in bad)
        progress_state = (tuple(bad), failed_units)
        if previous is not None and progress_state == previous:
            print(f"\n⚠ this pass changed nothing — stopping "
                  f"({len(bad)} file(s) still unresolved).")
            break
        previous = progress_state
        if not bad:
            break

    # --- final report ---
    state = load_state(bd)
    done = set(state.get("done", []))
    bad = [r for r in rels if r not in done or _needs_repair(bd, cfg, r)]
    print(f"\n{'=' * 60}")
    if not bad:
        print(f"✓ {slug}: {total}/{total} chapters translated and verified.")
        # Quality gate before the EPUB: the checks so far prove the markup is
        # sound, not that the text is right. Only the model can judge meaning —
        # and a book that reads wrong must not be packed as if it were finished.
        if not args.no_llm_check:
            if not _llm_gate(bd, slug, cfg, args, rels):
                return 1
        try:
            _translate_metadata(bd, slug, cfg, state)
        except RuntimeError as e:
            print(f"  ⚠ metadata not translated ({e})")
        _pack_and_announce(bd, slug, cfg, open_it=args.open)
        return 0

    print(f"✗ {slug}: {len(done)}/{total} translated, {len(bad)} file(s) unresolved:")
    # The listing is per file; a pin that no file could satisfy is invisible
    # in it. Aggregate the failure texts once, over every unresolved file, and
    # say it plainly when one glossary entry explains the hold-out: that is
    # the reader's call to make, not another retry's.
    reasons_by_file = {}
    with Cache(bd) as cache:
        for rel in bad:
            reasons_by_file[rel] = [why for _, _, why in cache.failed_units(rel)]
    for rel in bad[:10]:
        reasons = ""
        path = os.path.join(target, rel)
        if rel in done and os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                ver = verify_file(_read_original(bd, rel), f.read(), cfg)
            reasons_by_file.setdefault(rel, []).extend(ver["reasons"])
            reasons = "; ".join(ver["reasons"])[:110]
        print(f"  - {rel}{': ' + reasons if reasons else ' (not translated)'}")
    conflicts = glossary_conflicts(reasons_by_file)
    if conflicts:
        print()
        for e in conflicts[:3]:
            print(f"  ⚠ the pinned '{e['term']}' = '{e['expected']}' was never met "
                  f"in {len(e['files'])} file(s) ({e['hits']} failure(s))")
        print("    a pin no retry can satisfy means the text disagrees with it:")
        print("    either the rendering is wrong for this book, or the term has a")
        print("    second sense here — [exceptions] in glossary.toml names those")
        print(f"    contexts ('units {slug} <file>' shows the text to judge from).")
        print(f"    Fix the glossary first: 'run {slug}' then re-translates only")
        print("    the units that carry the term.")
    print("\nWhat to do:")
    print(f"  ai-epub-translator status {slug}       the units that failed, and why")
    print(f"  ai-epub-translator run {slug}          ask again (the rest of each file is kept)")
    print(f"  ai-epub-translator llm-check {slug}    a term wrong over and over?")
    print("                                         -> it is a glossary job")
    # A unit past its retry budget is not asked again by 'run': a fresh start
    # of its file is the way out — print the command ready to paste, because
    # retyping the spine paths is where this goes wrong.
    print("\n  a unit past its retries? start its file over (costs LLM time):")
    if len(bad) <= 6:
        print(f"  ai-epub-translator redo {slug} {' '.join(bad)}")
    else:
        print(f"  ai-epub-translator verify {slug} --fix     queues all of them at once")
    print(f"  ai-epub-translator run {slug}")
    return 1


def _llm_gate(bd: str, slug: str, cfg: dict, args, rels: list) -> bool:
    """Judge every chapter before packing; True when the book may be packed.

    The checks prove the markup, not the meaning. Every chapter the judge flags
    is polished with the judge's own note (a defect at 8/10 is a defect), the
    source words it carried over are translated, and what is still below the
    mark is re-translated once. What still reads wrong blocks the EPUB. An LLM
    outage never claims "faithful": the book is packed unjudged, and says so.
    """
    threshold = int(args.min_score)
    base_url = args.base_url or cfg["base_url"]
    model = args.model or cfg["model"]
    unjudged = "  ⚠ quality gate not run (LLM unavailable) — packing unjudged."
    print(f"\n── quality gate: judging {len(rels)} chapter(s) "
          f"(pass mark {threshold}/10)")
    results = _judge_files(bd, cfg, rels, base_url, model, status="gate")
    if results is None:
        print(unjudged)
        return True
    flagged = [row for row in results if _has_issue(*row[1:])]
    if flagged:
        results = _gate_polish(bd, slug, cfg, results, flagged, base_url, model)
        if results is None:
            print(unjudged)
            return True
    results = _gate_leftovers(bd, slug, cfg, results, base_url, model)
    if results is None:
        print(unjudged)
        return True
    low = [row for row in results if row[1] is not None and row[1] < threshold]
    if not low:
        print("  ✓ every chapter reads faithfully.")
        return True

    print(f"\n  {len(low)} chapter(s) still below {threshold}/10 — "
          "re-translating them once")
    with Cache(bd) as cache:
        for rel, _, _ in low:
            _requeue(bd, slug, [rel], known=set(rels), quiet=True)
        for rel, _, _ in low:
            if _process_file(bd, slug, rel, cfg,
                             translate_it=True, cache=cache) == "abort":
                print("  ✗ LLM unreachable — stopping before the EPUB.")
                return False
    rejudged = _judge_files(bd, cfg, [rel for rel, _, _ in low], base_url, model,
                            status="gate")
    if rejudged is None:
        print(unjudged)
        return True
    still = [row for row in rejudged if row[1] is not None and row[1] < threshold]
    if not still:
        print("  ✓ the re-translation fixed them.")
        return True
    print(f"\n{'=' * 60}")
    print(f"✗ EPUB not built: {len(still)} chapter(s) still read wrong:")
    for rel, score, comment in still:
        print(f"  - {rel} ({score}/10): {comment}")
    print("\nThis is a job for you, not for a retry:")
    print("  a term wrong over and over -> pin it, then run again")
    print(f"    ai-epub-translator glossary {slug} --suggest")
    print(f'    ai-epub-translator glossary {slug} --add "term=rendering"')
    print("  otherwise, inspect and decide:")
    print(f"    ai-epub-translator llm-check {slug} <file>")
    print("  build it anyway (the text stays as it is):")
    print(f"    ai-epub-translator pack {slug}")
    return False


# A chapter scoring this is clean: there is nothing left for a polish to gain,
# and rewriting it could only lose.
PERFECT_SCORE = 10


def _has_issue(score, comment) -> bool:
    """True if the judge named a concrete defect worth a targeted polish.

    Scores alone are not the trigger. The gate's pass mark decides what *blocks*
    the EPUB; what earns a polish is the note — "fifty centuries rendered as fifty
    years" is a defect at 8/10 exactly as at 5/10, and it is cheaper to fix it now
    than to ship it.
    """
    if score is None or score >= PERFECT_SCORE:
        return False
    issue = (comment or "").strip()
    return bool(issue) and not issue.lower().startswith(("faithful", "fedele"))


def _gate_polish(bd: str, slug: str, cfg: dict, results: list, flagged: list,
                 base_url: str, model: str):
    """Fix every flagged chapter with the judge's own note; keep what scores better.

    Three gates guard the rewrite, because it touches text the judge already
    accepted: each unit must validate, the file must pass the checks, and the
    re-judge must score it *higher* — otherwise the original is kept. Measured:
    a blind re-translation re-runs the prompt that produced the slip and
    reproduces it. Returns ``results`` with the improved scores, or ``None`` on
    an LLM outage.
    """
    print(f"\n  {len(flagged)} chapter(s) flagged by the judge — targeted polish")
    updated = {}
    for rel, score, comment in flagged:
        issue = (comment or "").strip()
        print(f"  · {rel} ({score}/10): {issue[:70]}")
        original_text = _read_original(bd, rel)
        path = os.path.join(P.target(bd), rel)
        with open(path, encoding="utf-8") as f:
            translated = f.read()
        out = polish_file(original_text, translated, issue, cfg,
                          chat_fn=_polish_chat(base_url, model))
        if out.get("aborted"):
            return None
        if not out["changed"]:
            print("      · the note did not map to any unit")
            continue
        if not verify_file(original_text, out["text"], cfg)["passed"]:
            print("      · polished version failed verify — kept the original")
            continue
        try:
            re_res = check_translation(original_text, out["text"], cfg,
                                       base_url, model)
        except RuntimeError:
            return None
        new_score = re_res["score"]
        if new_score is None or new_score <= score:
            print(f"      · no improvement ({score} -> {new_score}) — "
                  "kept the original")
            continue
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(out["text"])
        updated[rel] = (rel, new_score, re_res["comment"])
        print(f"      ✓ {score} -> {new_score}/10, "
              f"{out['changed']} unit(s) rewritten")
        logs.jsonl(bd, {"file": rel, "status": "polish", "score": new_score,
                        "comment": re_res["comment"]})
        logs.text(bd, slug, f"{rel} polished from gate note "
                            f"({score}->{new_score}): {issue[:80]}")
    if not updated:
        print("  · nothing improved — the text stays as it was")
    return [updated.get(row[0], row) for row in results]


def _gate_leftovers(bd: str, slug: str, cfg: dict, results: list,
                    base_url: str, model: str):
    """Translate the source words the model carried over, chapter by chapter.

    The judge reads a chapter whole and scores 10/10 one that still says "una
    quite naturale estensione"; this finds those deterministically, so the
    acceptance is objective: the words gone, the checks passed, the score not
    lower (a 10/10 chapter could never score "higher"). Returns ``results``
    refreshed, or ``None`` on an LLM outage.
    """
    pending = []
    for rel, score, comment in results:
        original_text = _read_original(bd, rel)
        path = os.path.join(P.target(bd), rel)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            translated = f.read()
        found = leftovers.find(original_text, translated, cfg)
        if found:
            pending.append((rel, score, comment, found))
    if not pending:
        return results

    total = sum(sum(f.values()) for *_, f in pending)
    print(f"\n  {total} source word(s) left untranslated in {len(pending)} "
          "chapter(s) — targeted fix")
    updated, stubborn = {}, []
    for rel, score, comment, found in pending:
        listed = ", ".join(f"{w}x{n}" if n > 1 else w
                           for w, n in sorted(found.items()))
        print(f"  · {rel}: {listed}")
        original_text = _read_original(bd, rel)
        path = os.path.join(P.target(bd), rel)
        with open(path, encoding="utf-8") as f:
            translated = f.read()
        out = fix_leftovers(original_text, translated, cfg,
                            chat_fn=_polish_chat(base_url, model))
        if out.get("aborted"):
            return None
        stubborn += [(rel, w, s) for w, s in out["remaining"]]
        if not out["fixed"]:
            print("      · the model would not translate it — left as it is")
            continue
        if not verify_file(original_text, out["text"], cfg)["passed"]:
            print("      · rewrite failed verify — kept the original")
            continue
        try:
            re_res = check_translation(original_text, out["text"], cfg,
                                       base_url, model)
        except RuntimeError:
            return None
        new_score = re_res["score"]
        if new_score is not None and score is not None and new_score < score:
            print(f"      · the chapter read worse ({score} -> {new_score}) — "
                  "kept the original")
            continue
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(out["text"])
        updated[rel] = (rel, new_score, re_res["comment"])
        left = len(out["remaining"])
        print(f"      ✓ {out['fixed']} translated"
              f"{f', {left} the model refused' if left else ''} "
              f"({score} -> {new_score}/10)")
        logs.jsonl(bd, {"file": rel, "status": "leftover", "score": new_score,
                        "comment": re_res["comment"], "words": sorted(found)})
        logs.text(bd, slug, f"{rel} leftover source words translated: {listed}")

    # Never ship these in silence: the model would not translate them, and no
    # amount of retrying changes that (measured: the failures fail every attempt).
    # Naming them is what lets the reader fix them — or pin the term.
    if stubborn:
        print(f"\n  ⚠ {len(stubborn)} word(s) the model would not translate — "
              "yours to decide:")
        for rel, word, sentence in stubborn:
            print(f"    {rel}  “{word}”  in: …{sentence[:90]}…")
        logs.jsonl(bd, {"status": "leftover_stuck",
                        "items": [{"file": r, "word": w, "sentence": s}
                                  for r, w, s in stubborn]})
    return [updated.get(row[0], row) for row in results]


def _needs_repair(bd: str, cfg: dict, rel: str) -> bool:
    """True if the file saved in target/ no longer passes verification.

    A done file can start failing after the rules change — typically when a
    glossary term is added: it was clean under the old checks, not under the new
    ones. Its translation lives in target/, not in the cache (which is pruned on
    success), so repair must be able to work from there.
    """
    path = os.path.join(P.target(bd), rel)
    if not os.path.isfile(path):
        return False
    with open(path, encoding="utf-8") as f:
        translated = f.read()
    return not verify_file(_read_original(bd, rel), translated, cfg)["passed"]


def cmd_repair(args):
    bd = _require_book(args.slug)
    cfg = merged_config(bd)
    target = P.target(bd)
    files = read_spine(find_opf(target))
    rels = [_rel(p, target) for p in files]
    state = load_state(bd)
    done = set(state.get("done", []))
    with Cache(bd) as cache:
        # Two sources of work: a cached failed translation, or a file already
        # saved in target/ that no longer passes (e.g. after a glossary change).
        pending = set(cache.pending())
        if args.file:
            if args.file not in pending and not _needs_repair(bd, cfg, args.file):
                print(f"Nothing to repair for {args.file}: it passes verification "
                      "(or was never translated — run 'translate' first).")
                return
            targets = [args.file]
        else:
            targets = [r for r in rels
                       if r in pending or (r in done and _needs_repair(bd, cfg, r))]
        if not targets:
            print("Nothing to repair: every translated file passes verification.")
            return
        print(f"Completing {len(targets)} file(s)…\n")
        for rel in targets:
            _process_file(bd, args.slug, rel, cfg, translate_it=False, cache=cache)


def _requeue(bd: str, slug: str, rels: list, known: set = None,
             quiet: bool = False) -> list:
    """Drop files from ``done`` and from the cache so they are translated afresh.

    Shared by ``redo``, ``verify --fix`` and the gate. ``known`` is the set of
    valid spine paths; unknown entries are reported and skipped. ``quiet`` drops
    the closing hint when ``run`` is about to translate anyway.
    """
    target = P.target(bd)
    known = known if known is not None else {
        _rel(p, target) for p in read_spine(find_opf(target))}
    state = load_state(bd)
    done = set(state.get("done", []))
    queued = []
    with Cache(bd) as cache:
        for rel in rels:
            if rel not in known:
                print(f"  ✗ {rel}: not in the spine — skipped")
                continue
            state["done"] = [d for d in state.get("done", []) if d != rel]
            state.get("failed", {}).pop(rel, None)
            cache.drop(rel)
            queued.append(rel)
            print(f"  ↻ {rel}: {'dropped from done' if rel in done else 'cleared'}"
                  f" — will be re-translated")
    if queued:
        save_state(bd, state)
        if not quiet:
            print(f"\n{len(queued)} file(s) queued. Next: ai-epub-translator run {slug}")
    return queued


def cmd_redo(args):
    """Mark files for a fresh translation (drop them from done + cache).

    Use after ``verify`` flags a file whose translation is damaged (abbreviated
    prose, dropped section): the next ``translate`` redoes it from scratch with the
    current checks and prompt.
    """
    bd = _require_book(args.slug)
    _requeue(bd, args.slug, args.files)


def cmd_verify(args):
    """Run the deterministic checks; with ``--fix`` requeue whatever fails."""
    bd = _require_book(args.slug)
    cfg = merged_config(bd)
    target = P.target(bd)
    files = read_spine(find_opf(target))
    if args.file:
        files = [f for f in files if os.path.relpath(f, target) == args.file]
    failed = []
    for path in files:
        rel = _rel(path, target)
        original_text = _read_original(bd, rel)
        with open(path, encoding="utf-8") as f:
            translated = f.read()
        ver = verify_file(original_text, translated, cfg)
        flag = "✓" if ver["passed"] else "✗"
        print(f"{flag} {rel} (score {ver['score']})")
        if not ver["passed"]:
            failed.append(rel)
            for r in ver["reasons"]:
                print(f"    - {r}")

    if not getattr(args, "fix", False):
        return
    if not failed:
        print("\nNothing to requeue: every file passed.")
        return
    print(f"\nRequeueing {len(failed)} failed file(s):")
    _requeue(bd, args.slug, failed,
             known={_rel(p, target) for p in read_spine(find_opf(target))})


def cmd_polish(args):
    """The quality gate's polish, on demand: judge, then fix what is flagged."""
    bd = _require_book(args.slug)
    cfg = merged_config(bd)
    base_url = args.base_url or cfg["base_url"]
    model = args.model or cfg["model"]
    threshold = int(args.min_score)
    rels = [r for r in _done_rels(bd) if not args.file or r == args.file]
    print(f"Polishing {args.slug} — judging {len(rels)} file(s), "
          f"fixing those below {threshold}/10\n")
    results = _judge_files(bd, cfg, rels, base_url, model)
    if results is None:
        return 1
    flagged = [row for row in results
               if row[1] is not None and row[1] < threshold and _has_issue(*row[1:])]
    if not flagged:
        print("\nNothing below the mark.")
        return 0
    after = _gate_polish(bd, args.slug, cfg, results, flagged, base_url, model)
    if after is None:
        print("  ✗ LLM unreachable — stopping.")
        return 1
    improved = sum(1 for before, now in zip(results, after) if now[1] != before[1])
    print(f"\n{improved} file(s) improved.")
    if improved:
        print(f"Rebuild the EPUB: ai-epub-translator pack {args.slug}")
    return 0


def _polish_chat(base_url, model):
    """A quiet chat callable bound to an endpoint/model (for polish blocks)."""
    import io as _io

    from . import llm

    def _chat(prompt, max_tokens):
        return llm.chat(prompt, base_url, model, max_tokens=max_tokens,
                        progress=Progress(Progress.STREAM, total=len(prompt),
                                          out=_io.StringIO()))
    return _chat


def _judge_files(bd: str, cfg: dict, rels: list, base_url: str, model: str,
                 indent: str = "  ", status: str = "checked"):
    """Judge each translated file; print one line each; ``None`` on an outage.

    Returns ``[(rel, score, comment)]``. A sampled judgement is blind to
    whatever the sampler cut away — exactly the omission this check hunts for —
    so it is flagged loudly; it should be rare.
    """
    out = []
    for i, rel in enumerate(rels, 1):
        original_text = _read_original(bd, rel)
        with open(os.path.join(P.target(bd), rel), encoding="utf-8") as f:
            translated = f.read()
        try:
            res = check_translation(original_text, translated, cfg, base_url, model)
        except RuntimeError as e:
            print(f"{indent}⚠ LLM unavailable ({e})")
            logs.jsonl(bd, {"file": rel, "status": "check_error", "error": str(e)})
            return None
        score, comment = res["score"], (res["comment"] or "ok")
        mark = f"{score}/10" if score is not None else "??"
        flag = "  ⚠ judged on a SAMPLE (file too large)" if res.get("sampled") else ""
        print(f"{indent}[{i}/{len(rels)}] {mark:>5}  {rel} — {comment}{flag}")
        logs.jsonl(bd, {"file": rel, "status": status, "score": score,
                        "comment": comment, "sampled": res.get("sampled", False),
                        "completion_tokens": res["completion_tokens"]})
        out.append((rel, score, comment))
    return out


def _done_rels(bd: str) -> list:
    target = P.target(bd)
    done = set(load_state(bd).get("done", []))
    return [r for r in (_rel(p, target)
                        for p in read_spine(find_opf(target))) if r in done]


def cmd_check(args):
    """Judge translation accuracy per file via the LLM (run after translating)."""
    bd = _require_book(args.slug)
    cfg = merged_config(bd)
    rels = [r for r in _done_rels(bd) if not args.file or r == args.file]
    if not rels:
        print("No translated files to check (run 'run' first).")
        return
    print(f"Checking accuracy of {len(rels)} file(s) with {args.model or cfg['model']}…\n")
    results = _judge_files(bd, cfg, rels, args.base_url or cfg["base_url"],
                           args.model or cfg["model"], indent="")
    if not results:
        return
    valid = [s for _, s, _ in results if s is not None]
    if valid:
        low = [rel for rel, s, _ in results if s is not None and s < 7]
        print(f"\nAccuracy: avg {sum(valid) / len(valid):.1f}/10 over {len(valid)} file(s).")
        if low:
            print(f"Below 7/10: {len(low)} file(s) — " + ", ".join(low[:5]))
            print("\nWhat to do, depending on the comment above:")
            print("  a term wrong over and over (e.g. 'X instead of Y'):")
            print(f"    ai-epub-translator glossary {args.slug} --suggest")
            print(f'    ai-epub-translator glossary {args.slug} --add "X=Y"')
            print(f"    ai-epub-translator run {args.slug}"
                  "           re-translates just the units with that term")
            print("  omissions / scattered errors in one file:")
            print(f"    ai-epub-translator redo {args.slug} <file>   then   run {args.slug}")


def _book_health(bd: str, slug: str, cfg: dict) -> dict:
    """Deterministic health report for one book (no LLM).

    Returns counts plus the per-file failures, grouped by cause, so the caller can
    render a compact overview.
    """
    target = P.target(bd)
    files = read_spine(find_opf(target))
    state = load_state(bd)
    done = set(state.get("done", []))
    report = {"total": len(files), "done": len(done), "checked": 0,
              "passed": 0, "failures": [], "untranslated": 0}
    for path in files:
        rel = _rel(path, target)
        if rel not in done:
            report["untranslated"] += 1
            continue
        original_text = _read_original(bd, rel)
        with open(path, encoding="utf-8") as f:
            translated = f.read()
        ver = verify_file(original_text, translated, cfg)
        report["checked"] += 1
        if ver["passed"]:
            report["passed"] += 1
        else:
            reasons = "; ".join(ver["reasons"])
            report["failures"].append({"file": rel, "score": ver["score"],
                                       "cause": _category(reasons),
                                       "reasons": reasons})
    return report


def cmd_check_all(args):
    """Overall health report: deterministic checks (+ optional LLM accuracy).

    Without a slug every book is covered. The deterministic pass is free and
    always runs; ``--llm`` adds the per-file accuracy judgement, which costs one
    LLM call per translated file.
    """
    slugs = [args.slug] if args.slug else _slugs()
    if not slugs:
        print(f"No books in {BOOKS} yet. Create one with '{PROG} setup <file.epub>'.")
        return

    overall_ok = True
    for slug in slugs:
        bd = _require_book(slug)
        cfg = merged_config(bd)
        print(f"\n{'=' * 60}\n{slug}  ({cfg['source_lang']} → {cfg['target_lang']})\n{'=' * 60}")
        rep = _book_health(bd, slug, cfg)
        pct = round(100 * rep["done"] / rep["total"]) if rep["total"] else 0
        print(f"  translated : {rep['done']}/{rep['total']} ({pct}%)"
              + (f" · {rep['untranslated']} still to do" if rep["untranslated"] else ""))
        print(f"  structure  : {rep['passed']}/{rep['checked']} clean")
        if rep["failures"]:
            overall_ok = False
            by_cause: dict = {}
            for f in rep["failures"]:
                by_cause.setdefault(f["cause"], []).append(f)
            print(f"  issues     : {len(rep['failures'])} file(s) — "
                  + " · ".join(f"{c}: {len(v)}" for c, v in sorted(by_cause.items())))
            for f in rep["failures"]:
                print(f"    ✗ [{f['cause']}] {f['file']} (score {f['score']})")
                print(f"        {f['reasons'][:150]}")
        elif rep["checked"]:
            print("  issues     : none — every translated file is structurally clean")

        if not args.llm:
            continue
        # optional LLM pass: accuracy of meaning, which the checks cannot judge
        print("\n  LLM accuracy:")
        results = _judge_files(bd, cfg, _done_rels(bd), args.base_url or cfg["base_url"],
                               args.model or cfg["model"], indent="    ") or []
        scores = [s for _, s, _ in results if s is not None]
        if scores:
            avg = sum(scores) / len(scores)
            low = [s for s in scores if s < 7]
            print(f"    → avg {avg:.1f}/10 over {len(scores)} file(s)"
                  + (f", {len(low)} below 7/10" if low else ""))
            if low:
                overall_ok = False

    print(f"\n{'=' * 60}")
    print("✓ All good." if overall_ok
          else "✗ Some issues need attention (see above).")
    return 0 if overall_ok else 1


def _glossary_violations(bd: str, cfg: dict) -> dict:
    """Count, per glossary term, how many translated blocks violate it.

    Deterministic (no LLM): reuses the same check that makes ``verify`` fail.
    """
    target = P.target(bd)
    state = load_state(bd)
    done = set(state.get("done", []))
    counts: dict = {src: 0 for src in (cfg.get("glossary") or {})}
    for path in read_spine(find_opf(target)):
        rel = _rel(path, target)
        if rel not in done:
            continue
        original_text = _read_original(bd, rel)
        with open(path, encoding="utf-8") as f:
            translated = f.read()
        for d in analyze(original_text, translated, cfg).defects:
            if d.kind == GLOSSARY:
                m = re.search(r"term '([^']+)'", d.detail)
                if m and m.group(1) in counts:
                    counts[m.group(1)] += 1
    return counts


def _near_twins(words: list) -> list:
    """Pairs of near-homograph words: same length, differing in one character.

    This is the shape of the dangerous case — ``exotérisme`` vs ``ésotérisme``,
    one letter apart yet opposite in meaning — which a model happily collapses
    into a single rendering.
    """
    pairs = []
    by_len: dict = {}
    for w in words:
        by_len.setdefault(len(w), []).append(w)
    for same_len in by_len.values():
        for i, a in enumerate(same_len):
            for b in same_len[i + 1:]:
                diff = sum(1 for x, y in zip(a, b) if x != y)
                if diff == 1:
                    pairs.append((a, b))
    return pairs


def _suggest_terms(bd: str, cfg: dict, min_hits: int = 5) -> list:
    """Heuristic, LLM-free hunt for terms a model is likely to confuse.

    Reports frequent source words that are near-homographs of each other (one
    character apart): the pattern that produced the exotérisme/ésotérisme bug.
    Returns ``[(word_a, word_b, hits_a, hits_b)]``, most frequent first.
    """
    target = P.target(bd)
    state = load_state(bd)
    done = set(state.get("done", []))
    word_re = re.compile(r"\b\w{6,}\b", re.UNICODE)
    freq: dict = {}
    for path in read_spine(find_opf(target)):
        rel = _rel(path, target)
        if rel not in done:
            continue
        original_text = _read_original(bd, rel)
        text = "".join(t.raw for t in tokenize(original_text) if t.kind == X_TEXT)
        for w in word_re.findall(_fold(text)):
            freq[w] = freq.get(w, 0) + 1
    frequent = [w for w, n in freq.items() if n >= min_hits]
    out = [(a, b, freq[a], freq[b]) for a, b in _near_twins(frequent)]
    return sorted(out, key=lambda t: -(t[2] + t[3]))


def cmd_glossary(args):
    """Show, extend or extract the per-book glossary."""
    bd = _require_book(args.slug)
    cfg = merged_config(bd)
    terms = dict(cfg.get("glossary") or {})

    if args.add:                                   # the only writing path
        added = []
        for pair in args.add:
            if "=" not in pair:
                print(f"  ✗ '{pair}': expected the form \"source=target\"")
                continue
            src, dst = (p.strip() for p in pair.split("=", 1))
            if not src or not dst:
                print(f"  ✗ '{pair}': empty term")
                continue
            terms[src] = dst
            added.append((src, dst))
        if added:
            path = save_glossary(bd, terms)
            for src, dst in added:
                print(f"  + {src} -> {dst}")
            print(f"\nSaved to {path}.")
            print(f"Next: ai-epub-translator run {args.slug}"
                  "   (re-translates just the units with those terms)")
        return

    if args.suggest:
        print("Candidates (heuristic, no LLM): frequent terms one letter apart —\n"
              "a model tends to collapse them into a single rendering, even when\n"
              "they mean the opposite.\n")
        cands = _suggest_terms(bd, cfg, int(args.min_hits))
        if not cands:
            print("  no candidates.")
            return
        for a, b, na, nb in cands[:15]:
            print(f"  {a} ({na}×)  vs  {b} ({nb}×)")
        print("\nCheck how they are rendered; if they collapse, pin the right one:")
        print(f'  ai-epub-translator glossary {args.slug} --add "term=rendering"')
        return

    if args.extract:
        target = P.target(bd)
        state = load_state(bd)
        done = [_rel(p, target) for p in read_spine(find_opf(target))
                if _rel(p, target) in set(state.get("done", []))]
        if args.file:
            done = [r for r in done if r == args.file]
        if not done:
            print("No translated file to analyse yet — run 'run <slug>' first.")
            return
        rel = done[0] if args.file else max(
            done, key=lambda r: len(_read_original(bd, r)))   # the biggest chapter
        print(f"Terminology review of {rel} with "
              f"{args.model or cfg['model']}…\n")
        original_text = _read_original(bd, rel)
        with open(os.path.join(target, rel), encoding="utf-8") as f:
            translated = f.read()
        try:
            cands = extract_glossary(original_text, translated, cfg,
                                     args.base_url or cfg["base_url"],
                                     args.model or cfg["model"])
        except RuntimeError as e:
            print(f"  ✗ LLM error: {e}")
            return
        if not cands:
            print("  No systematically wrong term reported.")
            return
        print(f"  {'term':20} {'current':20} {'proposed':20} why")
        for c in cands:
            print(f"  {c['source'][:20]:20} {c['current'][:20]:20} "
                  f"{c['correct'][:20]:20} {c['reason'][:40]}")
        if args.yes:
            for c in cands:
                terms[c["source"]] = c["correct"]
            path = save_glossary(bd, terms)
            print(f"\n✓ {len(cands)} term(s) saved to {path}")
            return
        # Human confirmation required: the model that makes these mistakes is not
        # an authority on the right rendering, and a wrong entry would propagate
        # everywhere (prompt AND check).
        print("\n⚠ NOT saved — you confirm the renderings. The model that gets these"
              "\n  terms wrong is no authority on how they should be translated.")
        print("\nTo accept one:")
        for c in cands[:3]:
            print(f'  ai-epub-translator glossary {args.slug} '
                  f'--add "{c["source"]}={c["correct"]}"')
        print("To accept them all: run again with --yes")
        return

    # default: show the glossary and what violates it
    if not terms:
        print(f"No glossary for '{args.slug}'.\n")
        print("A glossary pins the terms the model gets wrong every time: they go")
        print("into the prompt, are checked per unit, and 'run' applies them.\n")
        print(f"  ai-epub-translator glossary {args.slug} --suggest      heuristic")
        print(f"  ai-epub-translator glossary {args.slug} --extract      ask the LLM")
        print(f'  ai-epub-translator glossary {args.slug} --add "a=b"    by hand')
        return
    print(f"Glossary of {args.slug} ({len(terms)} terms):\n")
    violations = _glossary_violations(bd, cfg)
    for src in sorted(terms):
        n = violations.get(src, 0)
        flag = f"✗ {n} unit(s) to redo" if n else "✓"
        print(f"  {src:24} -> {terms[src]:24} {flag}")
    if any(violations.values()):
        print(f"\nFix the flagged units: ai-epub-translator run {args.slug}")


def _category(reason: str) -> str:
    """Classify a failure reason: llm-error | quality."""
    r = (reason or "").lower()
    if "llm" in r or "http" in r or "unreachable" in r or " 507" in r:
        return "llm-error"
    return "quality"


def cmd_status(args):
    """Progress, the units still failing and why, tokens spent."""
    bd = _require_book(args.slug)
    cfg = merged_config(bd)
    state = load_state(bd)
    target = P.target(bd)
    files = read_spine(find_opf(target))
    done = state.get("done", [])
    failed = state.get("failed", {})
    records = logs.read_jsonl(bd)
    print(f"Book: {args.slug} ({cfg['source_lang']} → {cfg['target_lang']})")
    print(f"Models: {cfg['model']} @ {cfg['base_url']}")
    with Cache(bd) as cache:
        pending = cache.pending()
        print(f"Progress: {len(done)}/{len(files)} translated, "
              f"{len(failed)} failed, {len(pending)} pending")
        for rel in pending:
            ok, bad = cache.unit_status(rel)
            print(f"\n  {rel}: {ok} unit(s) ok, {bad} failed")
            for idx, attempts, why in cache.failed_units(rel)[:8]:
                print(f"    unit {idx} ({attempts} attempt(s)): {why}")
    for rel, why in failed.items():
        if rel not in pending:
            print(f"\n  - [{_category(why)}] {rel}: {why}")
    if records:
        # score can be present-but-null (the LLM judge often emits no parseable
        # n/10), and dict.get's default only fills a MISSING key, not a null one —
        # so filter explicitly, or sum([.., None]) raises TypeError.
        scores = [s for s in (r.get("score") for r in records) if s is not None]
        toks = sum(r.get("completion_tokens", 0) or 0 for r in records)
        avg = f"{sum(scores) // len(scores)}" if scores else "n/a"
        print(f"\nAttempts: {len(records)} · avg score {avg} · total tokens ~{toks}")


def cmd_config(args):
    """``config init`` writes the commented template; ``config show`` explains the
    effective values, each with the layer it came from."""
    if args.action == "init":
        dest = P.user_config_path()
        if os.path.exists(dest) and not args.force:
            sys.exit(f"{dest} exists — pass --force to overwrite it")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "defaults.toml"), encoding="utf-8") as f:
            template = f.read()
        with open(dest, "w", encoding="utf-8") as f:
            f.write(template)
        print(f"Wrote {dest}\nSet [model] base_url and model, then: {PROG} doctor")
        return
    bd = _require_book(args.slug) if args.slug else None
    cfg = merged_config(bd, with_sources=True)
    print("Layers (lowest to highest):")
    for name, path, values in layers(bd):
        state = "" if name in ("defaults", "env") else (
            "" if os.path.isfile(path) else "  (absent)")
        print(f"  {name:9} {path}{state}")
    print(f"\nLibrary: {BOOKS}\n")
    for key in sorted(k for k in cfg if not k.startswith("_") and
                      k not in ("glossary", "glossary_notes", "glossary_exceptions")):
        print(f"  {key:18} = {cfg[key]!r:50}  [{cfg['_sources'].get(key, '-')}]")


def cmd_doctor(args):
    """Is everything in place to translate? Says what is not, and how to fix it."""
    import json
    import urllib.request
    cfg = merged_config()
    ok = True

    def good(msg):
        print(f"  ✓ {msg}")

    def bad(msg, fix):
        nonlocal ok
        ok = False
        print(f"  ✗ {msg}\n      → {fix}")

    print(f"{PROG} {__version__}")
    ucp = P.user_config_path()
    if os.path.isfile(ucp):
        good(f"user config: {ucp}")
    else:
        bad(f"no user config at {ucp}", f"{PROG} config init")
    print(f"  · library: {BOOKS} ({len(_slugs())} book(s))")
    base_url = args.base_url or cfg["base_url"]
    model = args.model or cfg["model"]
    try:
        with urllib.request.urlopen(f"{base_url}/models", timeout=5) as r:
            ids = [m.get("id") for m in json.load(r).get("data", [])]
        good(f"server at {base_url} ({len(ids)} model(s))")
    except Exception as e:                            # noqa: BLE001 — reported
        bad(f"no server at {base_url} ({e})",
            "start your local server (Ollama, LM Studio, llama-server, omlx) or set "
            "[model] base_url in the user config")
        ids = None
    if not model:
        bad("no model configured",
            "set [model] model in the user config"
            + (f" — the server offers: {', '.join(ids[:8])}" if ids else ""))
    elif ids is not None and model not in ids and model.split("/")[-1] not in ids:
        bad(f"model '{model}' is not on the server's list",
            f"the server offers: {', '.join(ids[:8])}")
    elif ids is not None:
        try:
            import io

            from .llm import chat
            from .ui import Progress
            res = chat("Reply with the single word OK.", base_url, model, max_tokens=4,
                       retries=1, progress=Progress(Progress.STREAM, out=io.StringIO()))
            good(f"model '{model}' answers ({res['elapsed']}s)")
        except Exception as e:                        # noqa: BLE001 — reported
            bad(f"model '{model}' does not answer ({e})", "check the server log")
    print("\nAll good." if ok else "\nFix the items above, then run doctor again.")
    return 0 if ok else 2


def cmd_units(args):
    """Show a file as the model sees it: one segment per unit, with placeholders.

    No LLM. The way to see what a glossary check or a rejected placeholder is
    talking about, and to judge a unit before pinning a term.
    """
    bd = _require_book(args.slug)
    cfg = merged_config(bd)
    sk = U.segment(_read_original(bd, args.file), cfg)
    units = sk.translatable
    chars = sum(len(u.visible) for u in units)
    markers = sum(len(u.runs) for u in units)
    print(f"{args.file}: {len(units)} unit(s), {chars} chars of prose, "
          f"{markers} placeholder(s), {len(llm._batches(units, cfg['batch_chars']))} batch(es)\n")
    for u in units:
        print(f"[{u.idx}] {u.visible}\n")


def _slugs() -> list:
    """The books of the library: every folder that carries a ``book.toml``."""
    if not os.path.isdir(BOOKS):
        return []
    return sorted(d for d in os.listdir(BOOKS)
                  if os.path.isfile(os.path.join(BOOKS, d, BOOK_TOML)))


def cmd_path(args):
    """Print the library, or one book's folder.

    The library is wherever the config points; a script or an agent needs this to
    reach a book's ``book.toml``, ``glossary.toml`` or ``.work/``.
    """
    print(_require_book(args.slug) if args.slug else BOOKS)


def cmd_list(args):
    """List the translatable books of the library, with a one-line progress summary."""
    if not os.path.isdir(BOOKS):
        print(f"No books directory: {BOOKS}")
        return
    slugs = _slugs()
    if not slugs:
        print(f"No books in {BOOKS} yet. Create one with '{PROG} setup <file.epub>'.")
        return
    print(f"Books in {BOOKS} ({len(slugs)}):\n")
    for slug in slugs:
        bd = book_dir(slug)
        try:
            cfg = merged_config(bd)
            target = P.target(bd)
            nfiles = len(read_spine(find_opf(target))) if os.path.isdir(target) else 0
            state = load_state(bd)
            done = len(state.get("done", []))
            failed = len(state.get("failed", {}))
            with Cache(bd) as cache:
                pending = len(cache.pending())
            print(f"  {slug:26} {cfg['source_lang']}→{cfg['target_lang']}  "
                  f"{done}/{nfiles} done · {failed} failed · {pending} pending")
        except (SystemExit, Exception) as e:                       # noqa: BLE001
            print(f"  {slug:26} (unreadable: {e})")


_DESCRIPTION = """\
EPUB translation harness — translate a book with a local LLM. The model translates
prose, never markup; nothing is saved until it passes verification.
"""

_EPILOG = """\
a usual run
  ai-epub-translator setup ~/Desktop/Moby-Dick.epub --source english --target italian
  ai-epub-translator glossary moby-dick --suggest     pin the risky terms first
  ai-epub-translator run moby-dick                    translate, check, then EPUB
  ai-epub-translator run moby-dick --min-score 8      demand more before packing

what happens (see README.md for the whole story)
  units       a chapter is cut into prose units; inline tags become placeholders
              (<g1>…</g1>, <x2/>); the model translates the prose; the tags go
              back where the placeholders are. Every unit is cached at once.
  checks      placeholders intact, prose not abbreviated, no ellipsis added,
              glossary terms rendered — then the whole file: well-formed XHTML,
              tags/attributes/ids/hrefs/page-breaks identical, lang updated.
  retries     a rejected unit is asked again alone, told what was wrong; what
              the model never gets right fails the file BY NAME.
  the gate    the model judges every chapter (-> 9/10 | faithful); flagged ones
              are polished with the judge's note; what still reads wrong blocks
              the EPUB. A term wrong over and over is a glossary job.

ai-epub-translator <command> -h    help + examples for one command
"""


def _help_formatter(prog):
    return argparse.RawDescriptionHelpFormatter(prog, max_help_position=32)


def _endpoint_args(p):
    p.add_argument("--base-url", help="LLM endpoint (overrides [model].base_url)")
    p.add_argument("--model", help="model id (overrides [model].model)")


def main(argv: list = None) -> int:
    if hasattr(signal, "SIGPIPE"):                    # `units … | head` must not trace
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    ap = argparse.ArgumentParser(prog=PROG, description=_DESCRIPTION,
                                 epilog=_EPILOG, formatter_class=_help_formatter)
    ap.add_argument("-l", "--list", dest="do_list", action="store_true",
                    help="list translatable books and exit")
    ap.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    ap.add_argument("--books", metavar="DIR",
                    help="the library (default: $AI_EPUB_TRANSLATOR_BOOKS, [paths] "
                         "library, or the XDG data dir)")
    ap.add_argument("--config", metavar="FILE",
                    help="user config file (default: ~/.config/ai-epub-translator/config.toml)")
    sub = ap.add_subparsers(dest="cmd", title="commands")

    def cmd(name, func, help, description, epilog=""):
        p = sub.add_parser(name, help=help, description=description,
                           epilog=epilog, formatter_class=_help_formatter)
        p.set_defaults(func=func)
        return p

    cmd("list", cmd_list, "list the books of the library",
        "Print each book of the library with its languages and progress. No LLM.")

    p = cmd("path", cmd_path, "print the library, or a book's folder",
            "Print the library directory, or the folder of one book — what a script "
            "or an agent needs to reach book.toml, glossary.toml or .work/.",
            f"examples\n  {PROG} path\n  {PROG} path moby-dick\n")
    p.add_argument("slug", nargs="?", help="a book of the library")

    p = cmd("config", cmd_config, "write the user config, or show the effective one",
            "'config init' writes a commented template to the user config path; "
            "'config show [slug]' prints every effective value with the layer it "
            "came from (defaults, user, book, env).",
            f"examples\n  {PROG} config init\n  {PROG} config show\n"
            f"  {PROG} config show moby-dick\n")
    p.add_argument("action", choices=("init", "show"))
    p.add_argument("slug", nargs="?", help="with show: a book, for its effective config")
    p.add_argument("--force", action="store_true", help="with init: overwrite")

    p = cmd("doctor", cmd_doctor, "check the config, the server and the model",
            "Report whether the user config exists, the server answers, the model is "
            "on its list and replies — with the fix for whatever is missing. Exit "
            "code 2 when something needs attention.")
    _endpoint_args(p)

    p = cmd("run", cmd_run,
            "run a configured book to completion (translate + verify + judge + EPUB)",
            "Translate every chapter unit by unit, verify, ask again for the units "
            "that failed, judge the result and pack the EPUB. Stops by itself when a "
            "pass no longer changes anything. Resume-safe: run it again after a "
            "crash — or after editing the glossary — and it picks up from the units "
            "it still lacks. Use 'setup <epub>' first.",
            "examples\n  ai-epub-translator run moby-dick\n"
            "  ai-epub-translator run moby-dick --progress stream\n"
            "  ai-epub-translator run moby-dick --min-score 8\n"
            "  ai-epub-translator run moby-dick --no-llm-check --open\n")
    p.add_argument("book", metavar="<slug|folder>",
                   help="a book already set up (slug or its folder)")
    p.add_argument("--passes", default=4,
                   help="max passes over the book (default 4); a pass that changes "
                        "nothing stops the run earlier")
    p.add_argument("--min-score", dest="min_score", default=7,
                   help="quality gate: pass mark out of 10 (default 7)")
    p.add_argument("--no-llm-check", dest="no_llm_check", action="store_true",
                   help="skip the quality gate and pack as soon as the checks pass")
    p.add_argument("--open", action="store_true", help="open the finished EPUB")
    p.add_argument("--progress", choices=PROGRESS_MODES,
                   help="display mode: stream | percent | both")
    _endpoint_args(p)

    p = cmd("setup", cmd_setup, "configure a new book from an EPUB",
            "Derive the slug from the filename (unless --slug is given), unpack to "
            "target/, snapshot to original/ (the baseline of every check) and write "
            "book.toml. Prints the next command to run.",
            "examples\n  ai-epub-translator setup ~/Desktop/Effective\\ C.epub\n"
            "  ai-epub-translator setup Madame-Bovary.epub --slug bovary --source french\n")
    p.add_argument("epub", help="path to the EPUB file")
    p.add_argument("--slug", help="explicit book slug (default: derived from filename)")
    p.add_argument("--source", help="source language (default: the user config)")
    p.add_argument("--target", help="target language (default: the user config)")

    p = cmd("repair", cmd_repair, "ask again for the units of a file that failed",
            "For every file that failed verification, ask the model again for the "
            "units it got wrong (the validated units are kept from the cache), "
            "splice, verify. Also completes a file already saved in target/ that "
            "stopped passing after a glossary change: only the units that now fail "
            "are re-asked.",
            "examples\n  ai-epub-translator repair moby-dick\n"
            "  ai-epub-translator repair moby-dick OEBPS/chapter-11.xhtml\n")
    p.add_argument("slug", help="book slug (a folder of the library)")
    p.add_argument("file", nargs="?", help="limit to one spine file (relative path)")

    p = cmd("polish", cmd_polish, "fix meaning/style issues the quality gate flagged",
            "For each translated file the LLM judge scores below the mark, hand its "
            "note back to the model and rewrite only the units it applies to, on the "
            "placeholder-protected text. A change is kept only if it still verifies "
            "and scores higher; otherwise the original stays.",
            "examples\n  ai-epub-translator polish moby-dick\n"
            "  ai-epub-translator polish moby-dick --min-score 9\n"
            "  ai-epub-translator polish moby-dick OEBPS/chapter-04.xhtml\n")
    p.add_argument("slug", help="book slug (a folder of the library)")
    p.add_argument("file", nargs="?", help="limit to one spine file (relative path)")
    p.add_argument("--min-score", dest="min_score", default=7,
                   help="fix files scoring below this out of 10 (default 7)")
    _endpoint_args(p)

    p = cmd("redo", cmd_redo, "queue files for a fresh translation",
            "Drop the given spine files from 'done' and from the cache, so the next "
            "'run' translates them from scratch — the way out for a unit the model "
            "never got right.",
            "examples\n  ai-epub-translator redo moby-dick OEBPS/chapter-02.xhtml\n"
            "\ntip: 'verify <slug> --fix' queues everything that fails, at once.\n")
    p.add_argument("slug", help="book slug (a folder of the library)")
    p.add_argument("files", nargs="+", help="spine file(s), relative path")

    p = cmd("verify", cmd_verify, "run the deterministic checks only (no LLM)",
            "Check every translated file against original/: well-formed XHTML, "
            "tag/code/pagebreak counts, ids/hrefs, lang, length, the structural diff "
            "and the per-block content checks (abbreviated prose, added ellipsis, "
            "glossary). Read-only unless --fix is given.",
            "examples\n  ai-epub-translator verify moby-dick\n"
            "  ai-epub-translator verify moby-dick OEBPS/chapter-02.xhtml\n"
            "  ai-epub-translator verify moby-dick --fix\n")
    p.add_argument("slug", help="book slug (a folder of the library)")
    p.add_argument("file", nargs="?", help="limit to one spine file (relative path)")
    p.add_argument("--fix", action="store_true",
                   help="requeue the files that fail so the next 'run' redoes them")

    p = cmd("units", cmd_units, "show a file as the model sees it (no LLM)",
            "Print the prose units of a spine file with their placeholders — what a "
            "rejected unit or a glossary check is talking about.",
            "examples\n  ai-epub-translator units moby-dick OEBPS/chapter-01.xhtml\n")
    p.add_argument("slug", help="book slug (a folder of the library)")
    p.add_argument("file", help="spine file (relative path)")

    p = cmd("llm-check", cmd_check, "LLM accuracy report per file",
            "For each translated file, send original + translation to the LLM and "
            "get a one-line accuracy judgment (<score>/10 | <comment>). Judges "
            "meaning, not markup — use 'verify' for that.",
            "examples\n  ai-epub-translator llm-check moby-dick\n"
            "  ai-epub-translator llm-check moby-dick OEBPS/chapter-05.xhtml\n")
    p.add_argument("slug", help="book slug (a folder of the library)")
    p.add_argument("file", nargs="?", help="limit to one spine file (relative path)")
    _endpoint_args(p)

    p = cmd("glossary", cmd_glossary, "per-book glossary: pin the terms the model gets wrong",
            "Show, extend or extract a book's glossary.toml. A term goes into "
            "the prompt (prevention), is checked per unit (a wrong rendering fails "
            "'verify') and 'run' re-translates only the units that carry it. "
            "Without arguments it lists the terms and how many units violate each.",
            "examples\n  ai-epub-translator glossary moby-dick\n"
            '  ai-epub-translator glossary moby-dick --add "sperm whale=capodoglio"\n'
            "  ai-epub-translator glossary moby-dick --suggest\n"
            "  ai-epub-translator glossary moby-dick --extract\n"
            "\n--suggest/--extract only propose: you confirm. The model that makes\n"
            "the mistake is no authority on the fix.\n")
    p.add_argument("slug", help="book slug (a folder of the library)")
    p.add_argument("--add", action="append", metavar='"source=target"',
                   help="pin a term (repeatable); the only writing path")
    p.add_argument("--suggest", action="store_true",
                   help="heuristic candidates (deterministic, no LLM)")
    p.add_argument("--extract", action="store_true",
                   help="ask the LLM for systematically mistranslated terms")
    p.add_argument("--file", help="with --extract: analyse this file")
    p.add_argument("--yes", action="store_true",
                   help="with --extract: save the candidates without confirmation")
    p.add_argument("--min-hits", dest="min_hits", default=5,
                   help="with --suggest: minimum term frequency (default 5)")
    _endpoint_args(p)

    p = cmd("check-all", cmd_check_all, "health report (all books, or one)",
            "For every translated file run the deterministic checks and summarize "
            "the issues by cause; add --llm to also judge accuracy (one LLM call per "
            "file). Exits non-zero when something needs attention.",
            "examples\n  ai-epub-translator check-all\n  ai-epub-translator check-all moby-dick --llm\n")
    p.add_argument("slug", nargs="?", help="limit to one book (default: all)")
    p.add_argument("--llm", action="store_true", help="also judge accuracy per file")
    _endpoint_args(p)

    p = cmd("status", cmd_status, "progress, failed units, tokens",
            "Progress of a book, the units still failing and why, tokens spent.")
    p.add_argument("slug", help="book slug (a folder of the library)")

    p = cmd("pack", cmd_pack, "pack target/ into a ready-to-read EPUB",
            "Pack books/<slug>/target/ into a valid .epub (mimetype stored first) "
            "and print its path. 'run' does this by itself when the book is complete.",
            "examples\n  ai-epub-translator pack moby-dick --open\n"
            "  ai-epub-translator pack moby-dick --out ~/Desktop/libro.epub\n")
    p.add_argument("slug", help="book slug (a folder of the library)")
    p.add_argument("--out", help="output path (default: books/<slug>/<slug>.<dest>.epub)")
    p.add_argument("--open", action="store_true",
                   help="open the EPUB with the OS default application")

    args = ap.parse_args(argv)
    global BOOKS
    if args.config:
        os.environ[P.ENV_CONFIG] = os.path.abspath(os.path.expanduser(args.config))
    if args.books or args.config:
        BOOKS = P.library_dir(args.books, load_user().get("library"))
    if args.do_list or args.cmd is None:
        cmd_list(args)
        return 0
    return args.func(args) or 0
