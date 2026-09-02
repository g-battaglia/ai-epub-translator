"""LLM call (OpenAI-compatible) with streaming output.

No external dependencies (only urllib). Tokens are printed in real time.

API
---
chat(...)              low-level streaming call -> {text, prompt_tokens, ...}
translate_units(...)   translate a file's prose units in batches (see units.py)
check_translation(...) the LLM-as-judge accuracy report
"""

from __future__ import annotations

import http.client
import io
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request

from .ui import Progress

# Defaults for transient-error retries (overridable via config [llm]).
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_S = 5
# Streaming watchdogs. urlopen(timeout=) only guards connect and each socket read,
# and it is defeated by a server that trickles sub-line bytes (or hangs mid-line):
# every dribble resets the read timer, so readline() blocks forever and the in-loop
# checks — which run only between lines — never fire. A real MLX deadlock did exactly
# this and hung a run for 20+ minutes. The fix is a watchdog thread that force-closes
# the response from outside once a deadline is crossed, unblocking the stuck read;
# the closed socket raises, and the retry/backoff loop recovers. STALL_S bounds the
# gap between chunks; TOTAL_S bounds the whole call.
STREAM_STALL_S = 180
STREAM_TOTAL_S = 1200

# Exceptions considered transient (worth retrying with backoff).
_TRANSIENT = (urllib.error.URLError, ConnectionError, TimeoutError,
              http.client.IncompleteRead, http.client.BadStatusLine, OSError)


class StreamTooSlow(RuntimeError):
    """A stream made steady progress but could not finish within STREAM_TOTAL_S.

    Not transient: the call is too large for the model's throughput at the
    configured total deadline, so retrying it from zero cannot converge — the
    model reached ~90 % last time and will again, because nothing about the call
    changes between attempts. Surfaced (rather than retried) so the caller can
    split the piece finer instead of looping for ever.
    """

GLOSSARY_HEADER = """
Glossary (MANDATORY — these terms are systematically mistranslated; use exactly
the given rendering, including all its inflected forms):
"""

GLOSSARY_TWIN_WARNING = (
    "  ^^ the two above look almost identical but are DIFFERENT terms: read the\n"
    "     letters of each occurrence before translating it, and never render both\n"
    "     with the same word.\n"
)


def _order_terms(terms: dict) -> list:
    """Order glossary entries so near-identical source terms sit side by side.

    Measured on the real corpus: with the entries sorted alphabetically,
    ``exotérisme`` and ``ésotérisme`` (opposite meanings, one letter apart) ended
    up separated by unrelated lines, and the model collapsed both onto the same
    word — 0 correct out of 5. Listing each near-twin pair adjacently, so the
    contrast is visible in one glance, gives 5 out of 5. Same model, same terms:
    only the order changed.

    Returns ``[(src, dst, is_twin_pair_end)]``, where the flag marks the second
    member of a pair (the line after which a warning is worth printing).
    """
    from .structdiff import _fold  # local: avoids a cycle
    remaining = dict(sorted(terms.items()))
    out, used = [], set()
    for src, dst in list(remaining.items()):
        if src in used:
            continue
        folded = _fold(src)
        twin = next(
            (o for o in remaining
             if o not in used and o != src and len(_fold(o)) == len(folded)
             and sum(1 for a, b in zip(_fold(o), folded) if a != b) == 1),
            None)
        out.append((src, remaining[src], False))
        used.add(src)
        if twin:
            out.append((twin, remaining[twin], True))   # right after its twin
            used.add(twin)
    return out


def glossary_section(cfg: dict) -> str:
    """Render the glossary block for a prompt, or '' when there is no glossary.

    With no glossary the prompt is byte-identical to the one without this feature.
    Near-twin terms are grouped (see :func:`_order_terms`) and the book's free-form
    notes, when present, are appended: they carry the *why* a term is a trap,
    which a bare mapping cannot express.
    """
    terms = cfg.get("glossary") or {}
    notes = (cfg.get("glossary_notes") or "").strip()
    if not terms and not notes:
        return ""
    out = []
    if terms:
        out.append(GLOSSARY_HEADER.rstrip("\n"))
        for src, dst, twin_end in _order_terms(terms):
            out.append(f"  {src} -> {dst}")
            if twin_end:
                out.append(GLOSSARY_TWIN_WARNING.rstrip("\n"))
    if notes:
        out.append("\nNotes on this book's terminology:")
        out.extend(f"  {line}" if line.strip() else ""
                   for line in notes.splitlines())
    return "\n".join(out) + "\n"


def _stream_read(resp, progress):
    """Consume an SSE stream, bounded by a watchdog thread.

    Returns ``(chunks, usage, finish_reason, killed)``. ``killed`` is ``None`` on a
    clean finish, otherwise the reason string of the deadline that was crossed. A
    watchdog thread force-closes ``resp`` once the stall or total deadline passes:
    the in-loop time checks run only between lines and so never see a read that
    blocks mid-line, but closing the socket from outside unblocks it. The blocked
    read then raises, which we turn into a ``killed`` reason (a transient error for
    the caller) instead of a crash.
    """
    chunks, usage, finish_reason = [], None, None
    state = {"last": time.time(), "start": time.time(),
             "reason": None, "done": False}
    stop = threading.Event()

    def _deadline(now):
        """The reason string if a deadline has passed at ``now``, else None."""
        if now - state["last"] > STREAM_STALL_S:
            return f"no data for {STREAM_STALL_S}s (server stalled)"
        if now - state["start"] > STREAM_TOTAL_S:
            return f"stream exceeded {STREAM_TOTAL_S}s"
        return None

    def _watch():
        # Covers the case the in-loop check cannot: a read blocked mid-line (a
        # sub-line trickle or a hung generation) never returns to the loop, so only
        # closing the socket from outside can unblock it.
        while not stop.wait(1.0):
            reason = _deadline(time.time())
            if reason:
                state["reason"] = reason
                try:
                    resp.close()                        # unblocks the stuck read
                except Exception:
                    pass
                return

    watchdog = threading.Thread(target=_watch, daemon=True)
    watchdog.start()
    try:
        for raw in resp:
            now = time.time()
            # Fast path: when the loop is iterating (e.g. a keepalive trickle), the
            # deadline is caught here, without waiting on the watchdog to close the
            # socket. The watchdog is only needed when the loop is blocked instead.
            reason = _deadline(now)
            if reason:
                state["reason"] = reason
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            # A real SSE event is "activity"; empty reads and comment/keepalive lines
            # are not. Resetting the stall timer only here is what lets STALL_S tell a
            # dead trickle (a server holding the socket open with empty bytes, like the
            # MLX deadlock) from a generation that is simply slow — the distinction the
            # caller uses to retry the one and re-split the other (see chat()).
            state["last"] = now
            payload = line[6:]
            if payload == "[DONE]":
                state["done"] = True
                break
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if obj.get("usage"):
                usage = obj["usage"]
            try:
                choice = obj["choices"][0]
                token = choice.get("delta", {}).get("content", "")
                if token:
                    chunks.append(token)
                    progress.step(token)
                fr = choice.get("finish_reason")
                if fr:
                    finish_reason = fr
                    state["done"] = True
            except (KeyError, IndexError):
                continue
    except Exception:
        # A read that raises because the watchdog closed the socket is the
        # expected way a deadline surfaces; anything else is a real error.
        if not state["reason"]:
            raise
    finally:
        stop.set()
        watchdog.join(timeout=2.0)
    # A deadline that fires only after the stream already finished (a sub-second
    # race) must not discard a good result.
    killed = state["reason"] if (state["reason"] and not state["done"]) else None
    return chunks, usage, finish_reason, killed


def chat(prompt: str, base_url: str, model: str, *,
         max_tokens: int, temperature: float = 0.15,
         progress=None, retries: int = DEFAULT_RETRIES,
         backoff_s: int = DEFAULT_BACKOFF_S) -> dict:
    """Low-level streaming call with transient-error retries.

    Returns ``{text, prompt_tokens, completion_tokens, finish_reason, elapsed,
    attempts}``. Transient failures (HTTP 5xx, connection reset, timeout,
    incomplete reads) are retried with exponential backoff; a non-transient HTTP
    error (4xx) or exhaustion of retries raises ``RuntimeError`` with an honest
    message. ``progress`` drives token display and is reset between attempts.
    """
    if progress is None:
        progress = Progress(Progress.STREAM, total=len(prompt))
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }).encode()
    url = f"{base_url}/chat/completions"
    started = time.time()
    last_exc, attempts = None, 0
    for attempt in range(1, retries + 1):
        attempts = attempt
        chunks, usage, finish_reason = [], None, None
        progress.reset()
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json", "Authorization": "Bearer local"},
            )
            # Low socket timeout as a first line of defence; the watchdog thread
            # below is what actually bounds stall and total time (see _stream_read).
            with urllib.request.urlopen(req, timeout=STREAM_STALL_S) as resp:
                chunks, usage, finish_reason, killed = _stream_read(resp, progress)
            if killed:                                  # deadline crossed
                if killed.startswith("stream exceeded"):
                    # Steady generation that out-ran the TOTAL deadline: the piece
                    # is too large for the model's throughput within STREAM_TOTAL_S.
                    # This is not transient — retrying from zero repeats the same
                    # 90 %+ run and times out again (see _translate_chunked's
                    # re-split for the remedy). Surface it instead of looping.
                    raise StreamTooSlow(killed)
                raise TimeoutError(killed)              # stall: transient, retry
            last_exc = None
            break                                       # success
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code < 500:                            # 4xx: not transient
                raise RuntimeError(f"LLM HTTP {e.code} ({base_url}): {e.reason}")
            # 5xx: fall through to retry
        except _TRANSIENT as e:
            last_exc = e
        if attempt < retries:
            wait = backoff_s * (2 ** (attempt - 1))
            print(f"\n  ⚠ transient LLM error ({_describe(last_exc)}); "
                  f"retry {attempt + 1}/{retries} in {wait}s", file=sys.stderr)
            time.sleep(wait)

    progress.finish()
    if last_exc is not None:
        raise RuntimeError(f"LLM error after {attempts} attempt(s) ({base_url}): "
                           f"{_describe(last_exc)}")

    text = "".join(chunks)
    pt = (usage or {}).get("prompt_tokens") or len(prompt) // 4
    ct = (usage or {}).get("completion_tokens") or len(text) // 4
    return {"text": text, "prompt_tokens": pt, "completion_tokens": ct,
            "finish_reason": finish_reason, "elapsed": round(time.time() - started, 1),
            "attempts": attempts}


def _describe(exc) -> str:
    """Short, honest description of a transient exception (no misleading wording)."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    return f"{type(exc).__name__}: {exc}"


# Targeted focus injected into a per-block prompt when the verifier localized a
# specific defect. Empty by default (a first-pass translation sees no focus); the
# re-translate / repair paths set it so the model is told exactly what it got wrong.
GLOSSARY_FOCUS_TPL = (
    "\n"
    "IMPORTANT: a previous translation of THIS block got the glossary terms below "
    "wrong\n"
    "(left in {source_lang}, or rendered with the wrong word). Render EVERY "
    "occurrence\n"
    "with EXACTLY the form shown on the right:\n"
    "{terms}\n"
)


def _glossary_focus(terms: list, cfg: dict) -> str:
    """Render the glossary focus block for a list of glossary focus entries.

    An entry is ``(src, dst)`` or ``(src, dst, note)``. The optional note is the
    evidence gathered by the verifier — typically *what the previous output wrote
    instead* — and is indented under its term so the model sees the exact word to
    stop using next to the exact word to use.
    """
    out = []
    for entry in terms:
        src, dst = entry[0], entry[1]
        note = entry[2] if len(entry) > 2 else ""
        out.append(f"  {src} -> {dst}")
        if note:
            out.extend(f"      {line}" for line in note.splitlines())
    return GLOSSARY_FOCUS_TPL.format(
        source_lang=cfg.get("source_lang", "inglese"), terms="\n".join(out))


# Two ways to clear a source word the model carried over, and both are needed:
# measured on ten real occurrences, rewriting the {target_lang} sentence fixed
# five and translating the {source_lang} one afresh fixed five — but not the same
# five. Seven of ten fall to the pair. Both prompts are deliberately bare: adding
# the surrounding block, the glossary, or a second mention of the word made the
# model return the sentence untouched.
LEFTOVER_FIX_TPL = """This {target_lang} sentence contains a word left in \
{source_lang}: "{word}".
Rewrite the sentence replacing ONLY that word with the {target_lang} rendering the
context calls for. Change nothing else. Return only the sentence, no comments.

{sentence}"""

LEFTOVER_RETRANSLATE_TPL = """Translate this sentence from {source_lang} to \
{target_lang}. Return only the translation, no comments.

{sentence}"""


def build_leftover_fix_prompt(sentence: str, word: str, cfg: dict) -> str:
    """Prompt to translate one carried-over word, in place, inside its sentence."""
    return LEFTOVER_FIX_TPL.format(
        source_lang=cfg.get("source_lang", "english"),
        target_lang=cfg.get("target_lang", "italian"),
        word=word, sentence=sentence)


def build_leftover_retranslate_prompt(sentence: str, cfg: dict) -> str:
    """Prompt to translate the original sentence afresh (the second attempt)."""
    return LEFTOVER_RETRANSLATE_TPL.format(
        source_lang=cfg.get("source_lang", "english"),
        target_lang=cfg.get("target_lang", "italian"),
        sentence=sentence)


# --- accuracy check (LLM-as-judge) --------------------------------------------

# Per-side char budget for the accuracy check. The judge should see the WHOLE text:
# sampling hides exactly the defect we hunt for (a summarized passage is invisible
# if the sampler cut it away). The limit is therefore a safety net for extreme
# files, not a routine behaviour — gemma-4-26b advertises a 200k-token window, so
# 150k chars per side (~44k tokens, ~88k for both) fits with wide margin.
MAX_CHECK_CHARS = 150000

CHECK_TPL = """You are a strict translation reviewer. Compare the ORIGINAL text with its
TRANSLATION and judge only TRANSLATION ACCURACY: meaning preserved, no omissions or
unwarranted additions, correct terminology, numbers and proper names. Ignore markup
and tag differences (those are checked separately). Be concise and honest.

Reply with EXACTLY one line, in English, in this format:
<score 0-10>/10 | <at most 12 words: the main issue, or "faithful">

ORIGINAL ({source_lang}):
{orig}

TRANSLATION ({target_lang}):
{trad}
"""


def _sample(text: str, limit: int = MAX_CHECK_CHARS) -> str:
    """Return ``text`` trimmed to ~``limit`` chars via head/middle/tail sampling.

    Only reached by texts beyond the (large) limit — see :data:`MAX_CHECK_CHARS`.
    """
    if len(text) <= limit:
        return text
    third = limit // 3
    mid_start = (len(text) - third) // 2
    return (text[:third] + "\n[…]\n" + text[mid_start:mid_start + third]
            + "\n[…]\n" + text[-third:])


def build_check_prompt(orig: str, trad: str, cfg: dict,
                       limit: int = MAX_CHECK_CHARS) -> str:
    """Prompt asking the model to judge translation accuracy in one line."""
    return CHECK_TPL.format(
        source_lang=cfg.get("source_lang", "inglese"),
        target_lang=cfg.get("target_lang", "italiano"),
        orig=_sample(orig, limit), trad=_sample(trad, limit),
    )


def parse_check_report(report: str) -> tuple:
    """Parse a one-line accuracy report into ``(score, comment)``.

    ``score`` is an int 0-10 or ``None`` if no ``n/10`` was found.
    """
    for line in report.splitlines():
        m = re.search(r'(\d{1,2})\s*/\s*10', line)
        if m:
            score = max(0, min(10, int(m.group(1))))
            comment = line[m.end():].lstrip(' |/-—:').strip()
            return score, comment
    first = report.strip().splitlines()[0] if report.strip() else ""
    return None, first


def translate_text(text: str, cfg: dict, base_url: str, model: str) -> str:
    """Translate a short string (e.g. OPF metadata) preserving any markup.

    Used for the book title / description in ``content.opf``, which the EPUB
    reader displays but which are outside the spine and so never reach the normal
    translation pipeline. Output is captured quietly.
    """
    if not text or not text.strip():
        return text
    prompt = (
        f"Translate the following from {cfg.get('source_lang', 'english')} "
        f"to {cfg.get('target_lang', 'italian')}. Keep any markup/entities "
        f"unchanged; translate only the prose. Return ONLY the translation.\n\n{text}"
    )
    buf = io.StringIO()
    prog = Progress(Progress.STREAM, total=len(prompt), out=buf)
    res = chat(prompt, base_url, model,
               max_tokens=max(64, len(text) * 3),
               retries=int(cfg.get("retries", 3)),
               backoff_s=int(cfg.get("backoff_s", 5)), progress=prog)
    out = res["text"].strip()
    return out or text


# --- glossary extraction (LLM-assisted, human-confirmed) ----------------------

EXTRACT_TPL = """You are a terminology reviewer. Compare the ORIGINAL text with its
TRANSLATION and find TERMS that are translated WRONGLY and SYSTEMATICALLY — above all
pairs of distinct source terms collapsed into one, or terms rendered with the opposite
meaning. Ignore style, word order and one-off slips: only recurring terminology.

Reply with ONE LINE PER TERM, nothing else, in this exact format:
source_term | current_rendering | correct_rendering | short reason

If everything is fine, reply exactly: NONE

ORIGINAL ({source_lang}):
{orig}

TRANSLATION ({target_lang}):
{trad}
"""


def parse_glossary_extraction(reply: str) -> list:
    """Parse the extraction reply into ``[{source, current, correct, reason}]``.

    Deliberately strict: a line must have the four pipe-separated fields, and both
    terms must be short (a term, not a sentence). Anything else is dropped — the
    model must not smuggle prose into the glossary.
    """
    out = []
    for line in reply.splitlines():
        line = line.strip().lstrip("-*0123456789. ").strip()
        if not line or line.upper() == "NONE" or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        source, current, correct = parts[0], parts[1], parts[2]
        reason = parts[3] if len(parts) > 3 else ""
        if not source or not correct:
            continue
        if len(source) > 40 or len(correct) > 40:      # a term, not a sentence
            continue
        if source.lower() == correct.lower():
            continue
        out.append({"source": source, "current": current,
                    "correct": correct, "reason": reason})
    return out


def extract_glossary(orig: str, trad: str, cfg: dict,
                     base_url: str, model: str) -> list:
    """Ask the model which terms are systematically mistranslated.

    Returns candidate entries; the caller must have a human confirm them — the
    model that makes these mistakes is not an authority on the right rendering.
    """
    limit = int(cfg.get("check_max_chars", MAX_CHECK_CHARS))
    prompt = EXTRACT_TPL.format(
        source_lang=cfg.get("source_lang", "inglese"),
        target_lang=cfg.get("target_lang", "italiano"),
        orig=_sample(orig, limit), trad=_sample(trad, limit),
    )
    buf = io.StringIO()
    prog = Progress(Progress.STREAM, total=len(prompt), out=buf)
    res = chat(prompt, base_url, model, max_tokens=512, progress=prog,
               retries=int(cfg.get("retries", 3)),
               backoff_s=int(cfg.get("backoff_s", 5)))
    return parse_glossary_extraction(res["text"])


def check_translation(orig: str, trad: str, cfg: dict,
                      base_url: str, model: str) -> dict:
    """Ask the model to judge the accuracy of a translation.

    The whole text of both sides is sent (see :data:`MAX_CHECK_CHARS`); only an
    extreme file is sampled, and ``sampled`` reports it so the caller can warn —
    a sampled judgement is blind to anything the sampler cut away.

    The model's (short) reply is captured without live streaming so the caller can
    print a clean per-file table. Returns ``{report, score, comment, sampled,
    prompt_tokens, completion_tokens, elapsed}``.
    """
    limit = int(cfg.get("check_max_chars", MAX_CHECK_CHARS))
    sampled = len(orig) > limit or len(trad) > limit
    prompt = build_check_prompt(orig, trad, cfg, limit)
    buf = io.StringIO()
    prog = Progress(Progress.STREAM, total=len(prompt), out=buf)
    res = chat(prompt, base_url, model, max_tokens=96, progress=prog,
               retries=int(cfg.get("retries", 3)),
               backoff_s=int(cfg.get("backoff_s", 5)))
    report = res["text"].strip()
    score, comment = parse_check_report(report)
    return {"report": report, "score": score, "comment": comment,
            "sampled": sampled,
            "prompt_tokens": res["prompt_tokens"],
            "completion_tokens": res["completion_tokens"],
            "elapsed": res["elapsed"]}


# --- unit translation (tag protection; see units.py) --------------------------

UNITS_TPL = """Translate the segments below from {source_lang} to {target_lang}.{register}

Rules:
- Each segment is one paragraph, wrapped as <seg id="N">…</seg>. Return EVERY
  segment, in the same order, with the same id and the same wrapping — and
  nothing else: no comments, no markdown.
- Translate EVERY sentence completely. NEVER summarize, shorten, or replace text
  with "..." or "…". A translated segment is about as long as its original.
- Tags such as <g1>…</g1> and <x2/> are placeholders for formatting. Keep every
  one of them exactly as written and in the same order; a pair <g1>…</g1> must
  wrap the translation of the words it wraps in the original. Never add, drop,
  rename or reorder a placeholder, and never add any other tag.
- A pair around a single letter at the start of a segment is a drop cap: keep it
  around the first letter of the translation (<g1>O</g1>ur → <g1>N</g1>otre,
  <g1>I</g1>l nostro).
- Do not translate proper names.
{glossary}
Segments:
{segments}
"""

UNIT_FIX_TPL = """Translate ONE segment from {source_lang} to {target_lang}.{register}

A previous translation of this segment was rejected: {reason}.
{focus}
Rules: translate EVERY sentence completely, never summarize or use "..." to omit
text; keep every placeholder (<g1>…</g1>, <x2/>) exactly as written, in the same
order, around the same words (a pair around a single first letter is a drop cap:
keep it around the first letter of the translation); add no other tag; do not
translate proper names. Return ONLY <seg id="1">…</seg>, no comments and no
markdown.
{glossary}
<seg id="1">{text}</seg>
"""

POLISH_TPL = """Improve the translation of the segments below ({source_lang} -> {target_lang}).{register}

A reviewer flagged this issue with the translation of this chapter:
  {issue}

Each segment gives the ORIGINAL and the CURRENT translation. Rewrite a CURRENT
translation only where that issue applies to it, fixing the issue and nothing
else. Tags such as <g1>…</g1> and <x2/> are placeholders for formatting: keep
every one exactly as written, in the same order, around the same words.
Return ONLY the segments you change, each as <seg id="N">new translation</seg>
in the order given, no comments and no markdown. If the issue applies to none
of them, reply exactly: NONE
{glossary}
{segments}
"""

# A batch of units in one call. Chars of prose, not of markup: measured at
# ~41 tok/s on this model, 16k chars (~5-6k tokens of output) is a 2-3 minute
# call — long enough for paragraph-to-paragraph context, short enough that a
# stall or a Ctrl-C costs little. The unit cap keeps a table of contents (300
# one-line items) from becoming a numbering exercise the model gets lost in.
DEFAULT_BATCH_CHARS = 16000
MAX_BATCH_UNITS = 40
# Second and later attempts on a rejected unit run warmer: measured on this
# corpus, an identical prompt at 0.15 reproduces the identical answer, so a
# retry at that temperature is a retry of the failure.
RETRY_TEMPERATURE = 0.4

_SEG_RE = re.compile(r'<seg\s+id\s*=\s*"?(\d+)"?\s*>(.*?)(?=</seg\s*>|<seg\s+id|\Z)',
                     re.S | re.I)
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*$", re.M)


def _register_line(cfg: dict) -> str:
    register = cfg.get("register") or "tu"
    return f' Address the reader as "{register}".' if register else ""


def _langs(cfg: dict) -> dict:
    return {"source_lang": cfg.get("source_lang", "english"),
            "target_lang": cfg.get("target_lang", "italian"),
            "register": _register_line(cfg), "glossary": glossary_section(cfg)}


def build_units_prompt(batch: list, cfg: dict) -> str:
    """Prompt translating a batch of :class:`units.Unit` in one call.

    Segments are numbered 1..N per call, not by their index in the file: small
    numbers the model can hardly get wrong, and the answer is mapped back by
    position — so even a renumbered reply still lands on the right units.
    """
    segments = "\n".join(f'<seg id="{i}">{u.visible}</seg>'
                         for i, u in enumerate(batch, 1))
    return UNITS_TPL.format(segments=segments, **_langs(cfg))


def build_unit_fix_prompt(unit, reason: str, cfg: dict, focus: str = "") -> str:
    """Prompt re-translating a single rejected unit, told why it was rejected."""
    return UNIT_FIX_TPL.format(reason=reason, focus=focus, text=unit.visible,
                               **_langs(cfg))


def build_polish_prompt(pairs: list, issue: str, cfg: dict) -> str:
    """Prompt fixing a reviewer's note across a batch of ``(unit, current)``.

    Unlike a rejected translation, the defect here is prose the judge deemed
    wrong (register, gender, a term). The whole batch travels in one call and
    the model returns only what it changes: measured on this corpus a flagged
    chapter has 18 to 588 units, and one call per unit was the cost of polish.
    """
    segments = "\n".join(
        f'<seg id="{i}">\nORIGINAL: {u.visible}\nCURRENT: {cur}\n</seg>'
        for i, (u, cur) in enumerate(pairs, 1))
    return POLISH_TPL.format(issue=issue, segments=segments, **_langs(cfg))


def parse_units_reply(text: str, expected: int = 0) -> dict:
    """``{id: answer}`` from a model reply, tolerantly.

    Markdown fences and prose around the segments are ignored, a missing
    ``</seg>`` ends at the next ``<seg`` or at the end, an escaped ``&lt;seg``
    is read as the tag. With ``expected`` set, a reply carrying exactly that
    many segments under other ids (the model renumbered) is mapped by position.
    """
    text = _FENCE_RE.sub("", text)
    text = re.sub(r"&lt;(/?seg\b[^&<>]*)&gt;", r"<\1>", text)
    out = {}
    for m in _SEG_RE.finditer(text):
        out[int(m.group(1))] = m.group(2).strip()
    if expected and len(out) == expected and set(out) != set(range(1, expected + 1)):
        out = dict(zip(range(1, expected + 1), out.values()))
    return out


def _batches(units: list, batch_chars: int) -> list:
    """Consecutive runs of units within the char budget (a giant unit alone)."""
    batches, cur, size = [], [], 0
    for u in units:
        n = len(u.visible)
        if cur and (size + n > batch_chars or len(cur) >= MAX_BATCH_UNITS):
            batches.append(cur)
            cur, size = [], 0
        cur.append(u)
        size += n
    if cur:
        batches.append(cur)
    return batches


def accept_unit(unit, answer: str, cfg: dict, strict: bool = True) -> tuple:
    """Validate one answer for ``unit``; return ``(inner, reason, focus)``.

    ``inner`` is the unit's translated content with the original markup spliced
    back, or ``None``; ``reason`` names the defect and ``focus`` carries the
    extra instruction for a retry (the glossary evidence, when that is it).
    """
    from . import units as U
    inner, why = U.render(unit, answer, strict=strict)
    if inner is None:
        return None, why, ""
    why = U.check_content(unit, answer, cfg)
    if not why:
        return inner, "", ""
    focus = ""
    if why.startswith("term "):
        from .repair import _glossary_focus_terms
        from .structdiff import _glossary_defects
        o_text, t_text = unit.plain, U.plain_text(answer)
        missing = _glossary_defects(o_text, t_text, cfg.get("glossary") or {},
                                    cfg.get("glossary_exceptions"))
        focus = _glossary_focus(
            _glossary_focus_terms(missing, o_text, t_text,
                                  cfg.get("glossary") or {}), cfg)
    return None, why, focus


def translate_units(units: list, cfg: dict, chat_fn, *, progress=None,
                    on_unit=None, history: dict = None, total: int = 0,
                    done: int = 0, log=print) -> dict:
    """Translate ``units`` in batches; retry each rejected unit on its own.

    ``chat_fn(prompt, max_tokens, temperature=, progress=)`` performs the call
    and may raise ``RuntimeError`` (server unreachable), which propagates.
    ``on_unit(unit, inner, reason)`` is called for every outcome the moment it
    is known — the caller wires it to the cache. ``history`` (``{idx:
    (attempts, reason)}``) is what earlier runs already tried: a unit past its
    budget is not asked again, it is reported with its last reason (``redo``
    starts it over). ``total``/``done`` only feed the progress line.
    Returns ``{idx: (inner_or_None, reason)}``.
    """
    retries = int(cfg.get("unit_retries", 2))
    batch_chars = int(cfg.get("batch_chars", DEFAULT_BATCH_CHARS))
    history = history or {}
    results: dict = {}

    def settle(u, inner, reason):
        results[u.idx] = (inner, reason)
        if on_unit:
            on_unit(u, inner, reason)

    fresh = []
    for u in units:
        n, why = history.get(u.idx, (0, ""))
        if n > retries:
            settle(u, None, f"gave up after {n} attempts ({why})")
        else:
            fresh.append(u)
    queue = _batches(fresh, batch_chars)
    n_batches, seen = len(queue), done
    total = total or len(units)
    while queue:
        batch = queue.pop(0)
        prompt = build_units_prompt(batch, cfg)
        size = sum(len(u.visible) for u in batch)
        log(f"  batch {n_batches - len(queue)}/{n_batches} · units "
            f"{seen + 1}–{seen + len(batch)} of {total} · "
            f"{100 * seen // max(total, 1)}% done")
        if progress is not None:
            progress.reset(total=size)
        try:
            res = chat_fn(prompt, max_tokens=max(1024, size * 3 + 512),
                          temperature=0.15, progress=progress)
        except StreamTooSlow:
            # Steady generation that outran the total deadline: nothing about
            # the call changes on a retry, so halve the batch instead. A single
            # unit that is still too slow is a real failure to surface.
            if len(batch) == 1:
                raise
            half = len(batch) // 2
            log("  (batch too slow for one call: splitting in two)")
            queue[:0] = [batch[:half], batch[half:]]
            n_batches += 1
            continue
        seen += len(batch)
        answers = parse_units_reply(res.get("text", ""), expected=len(batch))
        rejected = []
        for i, u in enumerate(batch, 1):
            answer = answers.get(i)
            if answer is None:
                rejected.append((u, "the segment was missing from the answer", ""))
                continue
            inner, why, focus = accept_unit(u, answer, cfg, strict=True)
            if inner is None:
                rejected.append((u, why, focus))
            else:
                settle(u, inner, "")
        for u, why, focus in rejected:
            prior = history.get(u.idx, (0, ""))[0]
            inner, why = _retry_unit(u, why, focus, cfg, chat_fn,
                                     max(0, retries - max(0, prior - 1)))
            settle(u, inner, why)
    return results


def _retry_unit(unit, reason: str, focus: str, cfg: dict, chat_fn,
                left: int) -> tuple:
    """Ask for one unit alone, up to ``left`` times; the last try is relaxed.

    Returns ``(inner_or_None, reason)``. Never a silent fallback: a unit the
    model cannot get right is reported as failed, by name.
    """
    for attempt in range(1, left + 1):
        prompt = build_unit_fix_prompt(unit, reason, cfg, focus=focus)
        res = chat_fn(prompt, max_tokens=max(512, len(unit.visible) * 3 + 512),
                      temperature=RETRY_TEMPERATURE, progress=None)
        answer = parse_units_reply(res.get("text", ""), expected=1).get(1)
        if answer is None:
            # a lone segment often comes back bare: take the whole reply
            answer = _FENCE_RE.sub("", res.get("text", "")).strip()
        inner, why, focus = accept_unit(unit, answer, cfg,
                                        strict=(attempt < left))
        if inner is not None:
            return inner, ""
        reason = why
    return None, reason


def default_chat(cfg: dict):
    """A chat callable bound to the configured endpoint and model.

    Block-level calls stream to the terminal only in ``stream`` mode; otherwise
    the caller's progress bar (per batch) is the indicator and retries are quiet.
    """
    stream = (cfg.get("progress") or "percent") == "stream"

    def _chat(prompt: str, max_tokens: int, temperature: float = 0.15,
              progress=None) -> dict:
        if progress is None:
            progress = None if stream else Progress(
                Progress.STREAM, total=len(prompt), out=io.StringIO())
        return chat(prompt, cfg["base_url"], cfg["model"],
                    max_tokens=max_tokens, temperature=temperature,
                    retries=int(cfg.get("retries", 3)),
                    backoff_s=int(cfg.get("backoff_s", 5)), progress=progress)
    return _chat
