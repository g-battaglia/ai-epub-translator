# CLAUDE.md — AI EPUB Translator

Read **[AGENTS.md](./AGENTS.md)** first: architecture, the one rule, working
principles, and the traps. This file only adds what is specific to working here with
Claude.

## In one line

Translates an EPUB with a **small local model (gemma-4-26b)**, and saves nothing that
does not pass verification. `ai-epub-translator -h` and README.md tell the whole story.

## What the user expects from you

- Everything in the repo is **English**: code,
  comments, CLI output, docs.
- **The glossary is your job.** When a term comes out wrong over and over, that is
  where judgement earns its keep: read the text, look the rendering up, pin it.
  `skills/book-setup/` is the end-to-end procedure for a new book and
  `skills/book-glossary/` goes deeper on the glossary alone (install them with
  `npx skills add .`). Follow the skill
  even though you could improvise it: it is written to be executed by *any* agent
  (opencode, PI Mono) driven by *any* model, so every step it prescribes is one a
  small model can also perform — and the numbers in it were measured, not assumed.
- **Look terms up, do not recall them.** The one thing no model here should answer
  from memory is what the target language calls a concept. Use your web tools; the
  skill has the Wikipedia language-link recipe. If you cannot confirm a rendering,
  say so and do not pin it.
- **Do not reach for a stronger model.** The goal is an harness that works with
  gemma-4-26b. Every failure so far turned out to be a harness bug, not a model limit.

## Before claiming anything about the model

Run it **five times**. A single sample is noise, and it has produced two wrong
diagnoses in this project already ("the model can't distinguish these terms" — it
could; the glossary was simply unreadable to it).

When a claim is measurable, measure it and put the number in the commit message.
That is how `0/5 → 5/5` ended up in the history, and why the fix is trustworthy.

## Running a book

```bash
uv run main.py list                        # what exists, and how far along
uv run main.py verify <slug>               # free, no LLM, safe anytime
uv run main.py run <slug>                  # translate + verify + retry + judge + EPUB
```

`run` is long: launch it in the background writing to a log, and watch the log rather
than polling. It is resume-safe — stopping and relaunching it costs nothing.

The library is outside the repository (`~/.local/share/ai-epub-translator/books` by
default). Books are copyrighted: nothing of a real book — not a chapter, not a slug —
belongs in the repo, in a test or in a commit.

## Judgement calls that are the user's, not yours

- **Which rendering a term should have** — propose, show the evidence, let them pick.
- **Throwing away a translation** (`redo`, `verify --fix`) — it costs minutes of LLM.
- **Packing a book the gate rejected** — the text stays as it is; that is their call.
