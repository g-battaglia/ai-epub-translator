# Contributing

Read [AGENTS.md](AGENTS.md) first: the one rule (the harness must work with a small
local model — every failure so far was a harness bug, not a model limit), the
working principles, and the traps.

## Running the tests

```bash
uv sync --dev
uv run python -m unittest discover -s tests            # ~5 s, no LLM
AI_EPUB_TRANSLATOR_CORPUS=1 uv run python -m unittest tests.test_units   # + your own library
uv run ruff check .
```

The corpus round-trip runs over the books of your library (point it elsewhere with
`AI_EPUB_TRANSLATOR_BOOKS=…`); it is the guarantee that the skeleton of every real
EPUB is copied byte for byte. Without it the suite is self-contained: no book, no
LLM, no network.

## Before claiming anything about the model

Run it five times (`tools/measure_units.py`). A single sample is noise, and it
has produced wrong diagnoses in this project before. When a claim is measurable,
put the number in the commit message.

## Commit messages

Present tense, one line saying what changed and — when there is one — the measured
number, then the why. English, like everything else in the repo.

## Pull requests

CI runs the suite on Linux, macOS and Windows and installs the built wheel. A PR
that adds a fix adds the regression test for it.
