# How it works

```
original ──segment──▶ units ──LLM (prose + placeholders)──▶ answers ──splice──▶ file ──verify──▶ target/
              │                          │ rejected (a placeholder lost,          │
              └ skeleton, verbatim       │  text abbreviated, a term wrong)       └ fails? a harness bug,
                                         └ asked again, alone, with the reason      reported as such
```

A **unit** is a run of inline content between two block boundaries — a paragraph,
a heading, a list item. Inside it, `<span class="italic"><span>kairos</span></span>`
reaches the model as `<g1>kairos</g1>`; code, footnote markers and anything
without letters is one opaque `<x2/>`. Half of the units of a typical calibre
EPUB carry no placeholder at all. Units travel in batches of ~16k characters (a
2–3 minute call on gemma-4-26b), so the model keeps paragraph-to-paragraph context;
a rejected unit is asked again on its own, told exactly what was wrong.

Measured on a real 77-chapter book (Cosmos and Psyche, 1.9 MB of XHTML, 30,000
tags): 390 unit samples, 98.7 % valid at the first attempt, 100 % after one retry —
including the four chapters the previous, markup-reproducing approach had never
managed to get through.

## What gets checked

Every translated file is compared with `original/`. All must pass:

1. well-formed XHTML
2. key tag counts identical
3. code intact (configured code classes)
4. page-break markers identical
5. `id` and `href` preserved
6. `xml:lang`/`lang` updated
7. length ≥ threshold (no truncation)
8. **structural diff**: tags and attributes aligned 1:1, plus the **per-block content
   checks**:
   - a `<p>` whose text is < 70% of the original (`block_text_ratio`) was
     **abbreviated** by the model;
   - added ellipses (`…`/`...` more than in the original) = omitted text;
   - **glossary**: a pinned term not rendered as required.

The content checks are the only ones that see summarized prose or wrong terms: the tags
all still match, so structure alone is blind to them.

## How it recovers

No dead ends: every class of failure has a way out, and the model is the **last resort**.

| problem | response |
|---|---|
| **markup damage** (a dropped `</span>`, an invented `</p>`) | cannot happen: the model never sees markup, the skeleton is copied verbatim |
| **placeholder lost or reordered** | the unit is asked again on its own, told exactly which placeholder, warmer on the second try |
| **summarized prose** ("..." instead of text) | the unit is rejected on length / added ellipsis and asked again |
| **wrong term** | glossary: the unit is asked again with the term, and with what it wrote instead |
| **a unit the model never gets right** | the file fails **by name** (`unit 17: placeholder </g2> missing`) — never a silent fallback |
| **a chapter too slow for one call** | the batch is halved |
| **transient errors** (5xx, resets, timeouts) | retried with backoff (`[llm] retries`, `backoff_s`) |
| **crash or Ctrl-C mid-way** | per-unit cache: the next run asks only for what is missing |
| **a rule changes** (a term pinned after the fact) | `run` re-asks only the units that now fail, the rest of the file is kept |

## Translating a book, start to finish

### 1. Set the book up

```bash
ai-epub-translator setup ~/Books/Moby-Dick.epub --source english --target italian
```

Creates `moby-dick/` in the library: `book.toml` (the language pair, where the
EPUB came from) and `.work/` with the unpacked EPUB and its pristine snapshot
(the baseline of every check). The slug comes from the filename;
[Configuration](configuration.md) describes the folder and where the library is.

### 2. Pin the risky terms — do this first

A term the model gets wrong it gets wrong **every single time**, and no structural
check can see it: `exotérisme` rendered "esoterismo" — its opposite — is still perfect
XHTML. Pinning it costs one line; finding out later costs a re-translation.

```bash
ai-epub-translator glossary moby-dick --suggest    # frequent terms 1 letter apart
ai-epub-translator glossary moby-dick --extract     # or ask the LLM
ai-epub-translator glossary moby-dick --add "sperm whale=capodoglio"
```

`--suggest` and `--extract` only **propose**: you confirm. To build a glossary with a
capable model (Claude & co.), there is a skill: `skills/book-glossary/`.

### 3. Run it

```bash
ai-epub-translator run moby-dick
```

Translates every chapter unit by unit, verifies, asks again for the units the model
got wrong — then the **quality gate**: the model judges every chapter
(`→ 9/10 | faithful`) and the EPUB is built **only if the book reads faithfully**.
Chapters below the mark are re-translated once; what is still wrong **blocks the EPUB**
and is reported to you.

**Resume-safe**: stop it whenever you like and run it again — it picks up from where it
stopped, and never re-translates what is already done. Same after a crash, or after you
edit the glossary.

```bash
ai-epub-translator run moby-dick --min-score 8    # demand more
ai-epub-translator run moby-dick --no-llm-check   # skip the gate
```

The markup checks always apply: `--no-llm-check` only skips the judgement of meaning.

### 4. Polish the wording (optional)

The gate lets minor issues through (a chapter at 7–8/10 is faithful, just not
perfect). To tighten them:

```bash
ai-epub-translator polish moby-dick          # fix chapters below 7/10
ai-epub-translator polish moby-dick --min-score 9   # hold them to 9/10
```

For each chapter the judge scores low, its note (a register slip, a gender error, a
mistranslated term) is handed back to the model, which rewrites **only the affected
units** on the placeholder-protected text. A change is kept only if it still passes
`verify` **and** scores higher; otherwise the original stays — so polishing never
makes a file worse, even when the judge itself is wrong.

### 5. If a chapter is still reported wrong

That is a decision, not a retry. The report tells you which chapter and why:

- **a term wrong over and over** → back to step 2 (glossary), then `run` again: only
  the units with that term are re-translated;
- **wording that polish could not lift** → inspect and decide;
- **build it anyway** → `ai-epub-translator pack moby-dick`.
