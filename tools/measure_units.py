"""Measure the unit/placeholder protocol on real chapters, several runs each.

The project rule: never conclude anything about the model from one sample. This
script translates the first batches of the given spine files ``--runs`` times
with the real model and reports, per file and overall, how many segments the
model returned valid at the first attempt, how many after the single-unit
retries, and why the rest failed. The numbers belong in the commit message.

    python3 tools/measure_units.py moby-dick OEBPS/chapter-01.xhtml \\
        text/part0023.html --runs 5 --max-batches 1
"""

from __future__ import annotations

import argparse
import collections
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_epub_translator import llm  # noqa: E402
from ai_epub_translator import units as U
from ai_epub_translator.cli import _read_original, book_dir  # noqa: E402
from ai_epub_translator.config import merged_config  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("slug")
    ap.add_argument("files", nargs="+", help="spine files (relative paths)")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--max-batches", type=int, default=1)
    ap.add_argument("--batch-chars", type=int, default=None)
    args = ap.parse_args(argv)

    bd = book_dir(args.slug)
    cfg = merged_config(bd)
    if args.batch_chars:
        cfg["batch_chars"] = args.batch_chars
    chat = llm.default_chat(cfg)
    calls = {"n": 0}

    def counting_chat(prompt, max_tokens, **kw):
        calls["n"] += 1
        return chat(prompt, max_tokens, **kw)

    events = []                          # (file, run, idx, first?, ok, reason)
    real_accept = llm.accept_unit

    def spy_accept(unit, answer, cfg_, strict=True):
        inner, why, focus = real_accept(unit, answer, cfg_, strict=strict)
        events.append((unit.idx, inner is not None, why, strict))
        return inner, why, focus

    llm.accept_unit = spy_accept
    totals = collections.Counter()
    reasons = collections.Counter()
    print(f"{args.slug}: {cfg['model']} · {args.runs} run(s) per file · "
          f"up to {args.max_batches} batch(es) per file\n")
    for rel in args.files:
        src = _read_original(bd, rel)
        sk = U.segment(src, cfg)
        batches = llm._batches(sk.translatable,
                               int(cfg.get("batch_chars", llm.DEFAULT_BATCH_CHARS)))
        sample = [u for b in batches[:args.max_batches] for u in b]
        chars = sum(len(u.visible) for u in sample)
        markers = sum(len(u.runs) for u in sample)
        print(f"[{rel}] {len(sk.translatable)} units, sampling {len(sample)} "
              f"({chars} chars, {markers} placeholders, max "
              f"{max((len(u.runs) for u in sample), default=0)} per unit)")
        for run in range(1, args.runs + 1):
            events.clear()
            calls["n"] = 0
            t0 = time.time()
            res = llm.translate_units(sample, cfg, counting_chat)
            dt = time.time() - t0
            first = {}
            for idx, ok, why, strict in events:
                first.setdefault(idx, (ok, why))
            n_first = sum(1 for ok, _ in first.values() if ok)
            n_final = sum(1 for inner, _ in res.values() if inner is not None)
            missing = [u.idx for u in sample if u.idx not in first]
            for ok, why in first.values():
                if not ok:
                    reasons[why.split(":")[0][:60]] += 1
            for idx in missing:
                reasons["segment missing from the answer"] += 1
            failed = [(idx, why) for idx, (inner, why) in res.items()
                      if inner is None]
            totals["units"] += len(sample)
            totals["first"] += n_first
            totals["final"] += n_final
            totals["calls"] += calls["n"]
            totals["secs"] += dt
            print(f"  run {run}: first attempt {n_first}/{len(sample)}, "
                  f"final {n_final}/{len(sample)}, {calls['n']} call(s), "
                  f"{dt:.0f}s ({chars / max(dt, 1):.0f} chars/s)")
            for idx, why in failed[:5]:
                print(f"      ✗ unit {idx}: {why}")
        sys.stdout.flush()
    u = totals["units"] or 1
    print(f"\nTOTAL: {totals['units']} unit-samples · first attempt "
          f"{100 * totals['first'] / u:.1f}% · after retries "
          f"{100 * totals['final'] / u:.1f}% · {totals['calls']} calls · "
          f"{totals['secs'] / 60:.0f} min")
    if reasons:
        print("first-attempt rejections:")
        for why, n in reasons.most_common():
            print(f"  {n:4d}  {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
