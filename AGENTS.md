# AGENTS.md — working on AI EPUB Translator

EPUB translation harness. It translates a book **chapter by chapter** with a **small
local model (gemma-4-26b)**, never shows the model any markup, and saves nothing
that does not pass verification.

Entry point: `main.py` (a checkout) or the `ai-epub-translator` command (installed).
Package: `ai_epub_translator/`. Start with `ai-epub-translator -h`.

## The one rule

**The harness must work with gemma-4-26b, not with a stronger model.** When something
fails, the answer is never "use a better model" — it is to find what the harness is
doing wrong. This has been true every single time so far:

- tags dropped, invented, mistyped by the model → it took a 1,700-line repair
  module to patch each slip, and four chapters of Cosmos and Psyche still never
  converged. The answer was not a better patch: **take the markup away from the
  model** (`units.py`). It translates prose with `<g1>…</g1>` placeholders and
  the original tags are spliced back. Measured on the chapter that had failed
  seven runs in a row: 7/7 units valid at the first attempt, five runs of five.
- a term always mistranslated → pin it in the glossary, and **show it properly**

That last one is the cautionary tale. `exotérisme`/`ésotérisme` (opposite meanings,
one letter apart) came out collapsed onto one word. The diagnosis "the model can't
distinguish them" was **wrong**: the glossary was sorted alphabetically, so the two
lines ended up separated by unrelated entries and the model never saw them as a pair.
Listing them adjacently: 0/5 → 5/5 correct. Same model, same terms, different order.

## Configuring a new book

Follow **[skills/book-setup/SKILL.md](skills/book-setup/SKILL.md)** —
a numbered procedure any agent can execute (opencode, PI Mono, Claude Code), whatever
model drives it. `skills/book-glossary/SKILL.md` goes deeper on the glossary
alone. Read the skill; what follows is only why it is shaped that way.

Setting a book up is **not** part of the harness and never will be. `main.py setup`
unpacks the EPUB and writes `book.toml`; everything that needs a decision — the field,
the register, the terminology — is the agent's job, because it needs the one thing the
harness has no access to: what the target language's own literature calls a concept.

That is the step to be careful about. **Do not ask the model what a term should be.**
Asked directly, gemma answers something plausible, and a plausible-but-invented
glossary entry is worse than no entry at all: it fails `verify` on correct
translations and sends good blocks back to be re-translated. Look it up on the web
instead, with whatever search or fetch tool you have. Wikipedia's *language links* are
the sharpest instrument — the same article in two languages, linked by editors of
both, so the target-language title is the attested term and not a translation of the
words. The skill has the exact API URLs.

Two rules that come from real damage in this corpus:

- **Measure before pinning.** Translate the same real paragraph 5 times. `trine` came
  out as "si trine"/"trinari" 5/5 → pinned, and fixed 15/15. Twenty other astrological
  terms came out right 5/5 → not pinned, because an entry could only add risk.
- **Check the entry cannot misfire.** The source term is matched as a *substring*:
  pinning `orb` would fire on every `absorb` in the book and fail verification on
  correct text. Count the occurrences before adding.

## Working principles

**Deterministic first, the model as a last resort.** The markup never leaves the
harness: a unit's tags become numbered placeholders, the model translates the
prose, the tags go back where the placeholders are. Only what genuinely needs
language goes to the model — a batch of paragraphs for context, a *single unit*
when one is rejected, never a whole chapter to "fix".

**Measure, do not suppose.** Every claim in this codebase came from a number. Before
concluding anything about the model, run it 5 times — a single sample is noise, and
has misled us more than once. Before "fixing" a defect, check it is real:
`ai-epub-translator verify <slug>` costs nothing.

**Nothing is saved until it passes.** A file that fails verification is never written
to `target/`; its units live in the cache and the failing ones are asked again from
there. The quality gate then judges *meaning* before the EPUB is built. A unit the
model never gets right fails the file **by name** — never a silent fallback, never
a paragraph left in the source language (that happened once, and only a glossary
term revealed it).

**Never lose work.** Every unit is cached the instant it validates. A crash, a
Ctrl-C, a server stall in any later step costs at most one batch.

## Layout

```
main.py                 thin launcher
ai_epub_translator/
  cli.py                subcommands, orchestration, the `run` loop
  paths.py              user config (XDG), the library, a book's .work/ layout
  config.py             config layers + glossary (tiny TOML parser, no deps)
  units.py              tag protection: segment, placeholders, validate, splice
  llm.py                streaming LLM, unit batches + retries, prompts, judge
  xhtml.py              tolerant lexer
  structdiff.py         structural + content diff (the `struct` check)
  repair.py             quality-gate rewrites (polish, leftovers), lang, entities
  verify.py             the final gate (hard-fail)
  cache.py              SQLite recovery cache (files + units)
  epub.py  state.py  ui.py  logs.py
tools/measure_units.py  the protocol measured on real chapters, five runs each
tests/                  unittests, no LLM needed (mocked), no book required
Formula/                the Homebrew formula, generated by tools/brew_formula.py
skills/                 book-setup · book-glossary (Agent Skills layout, skills.sh)
```

The **library is not in the repository**: by default it is
`~/.local/share/ai-epub-translator/books`, one folder per book
(`book.toml`, `glossary.toml`, `<slug>.<lang>.epub`, `.work/`), and it moves with
`[paths] library`, `$AI_EPUB_TRANSLATOR_BOOKS` or `--books`. See
[docs/configuration.md](docs/configuration.md).

The skills follow the Agent Skills layout (`skills/<name>/SKILL.md` with `name`
and `description` in the frontmatter). Any agent installs them into its own
directory with `npx skills add g-battaglia/ai-epub-translator` (from a checkout:
`npx skills add .`); those directories are gitignored, so there is exactly one
copy to keep right. `tests/test_skills.py` checks the layout.

## Code style

- **English everywhere**: docstrings, comments, CLI output, docs. No Italian in the
  codebase.
- Type hints on public functions; docstrings say *why*, not *what*.
- No new runtime dependencies. System Python 3 + lxml, nothing else.
- Comments earn their place by stating a constraint the code cannot show — a measured
  fact, a trap. Not narration.

## Before you commit

```bash
python3 -m unittest discover -s tests                     # must stay green (~10 s)
AI_EPUB_TRANSLATOR_CORPUS=1 python3 -m unittest tests.test_units  # byte-exact round-trip of
                                                          # every original of your library
ai-epub-translator check-all                              # if you have books: costs nothing
```

Add a regression test for every bug you fix. Several bugs here were subtle
(a counter that never incremented, a guard that stopped one pass too early) and would
have come back silently.

## Things that will bite you

- **`repair` works on two sources**: a file whose cached units are not all in, *or* a
  `done` file in `target/` that no longer passes (e.g. after a glossary change). In
  the second case the units of the saved file are paired 1:1 with the original's
  (`units.aligned`, 349/350 measured) and only the failing ones are asked again.
- **lxml does not round-trip**: it resolves numeric entities and normalizes CRLF.
  Never re-serialize a tree; splice by offset instead. `units.segment` +
  `units.reassemble` is byte-exact on the awkward page in `tests/samples.py` and on
  every original of a real library — keep both green
  (`tests/test_units.py::test_real_books_round_trip` needs
  `AI_EPUB_TRANSLATOR_CORPUS=1`); they are what guarantees the skeleton is copied
  verbatim.
- **Placeholders are the only thing the model can break.** `<g1>…</g1>` is a pair,
  `<x2/>` a lone run; names are XML-valid on purpose (lxml validates the nesting,
  `<1>` it would reject). A rejected unit is told *which* placeholder went missing.
  The relaxed check (last attempt) accepts a reordering that still nests — the
  same trade `structdiff._inline_moves` makes.
- **Retries are bounded per unit, across runs** (`units.retries`, plus the
  `attempts` column): a unit past its budget is not asked again, it is reported
  with its last reason (`status` lists them) and `redo <file>` starts it over.
  Identical prompt at temperature 0.15 gives the identical answer, so the
  re-asks run at 0.4.
- **What is opaque is invisible to the glossary check**: code, `<tt>`, footnote
  markers, anything without letters is hidden from the model, so the check runs on
  the text the model actually saw.
- **No book ever enters the repository.** The library lives outside it, and book
  content is copyrighted: no EPUB, no chapter, no slug of a real book in a test, a
  fixture or a commit. The suite runs without a single book.
