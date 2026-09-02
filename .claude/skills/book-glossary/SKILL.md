---
name: book-glossary
description: >-
  Build or revise the terminology glossary of a book translated by this
  harness: find the terms the local model gets systematically wrong, confirm the
  attested rendering, and pin them in books/<slug>/glossary.toml. Use when a term
  comes out wrong over and over, or when 'verify' keeps flagging the same word.
  For a brand-new book use the book-setup skill instead.
---

# book-glossary — build a book's terminology glossary

The deep dive on the glossary alone. For setting up a whole book — languages,
register, sample translation, then this — follow
**[book-setup](../book-setup/SKILL.md)** instead; it is the numbered procedure and it
covers everything here in its steps 4 to 8.

The division of labour: the translator is a **small local model** (gemma) — fast and
cheap, with one weakness: it gets **delicate terms** wrong, systematically. Whoever
sets the book up does not translate it, they **decide the terms**: a handful of lines
that hold for the whole book. That decision needs evidence, not fluency — measure what
the model does, then look the right rendering up on the web — which is why any agent
can do it, whatever model drives it.

## Why this exists (the case that created it)

In *De l'unité transcendante des religions* (Schuon, French→Italian) gemma translated
`exotérisme` as "esoterismo" — **the opposite term** — collapsing two distinct concepts
into one, ~90 times across three chapters.

No automated check could notice: the tags match, the length is right, the XHTML is
valid. **Only the words say the wrong thing.** A glossary is the only way to make that
error visible to a machine.

## The project in one screen

This repo is an EPUB translation harness: it translates a book **chapter by chapter**
with a local LLM and **saves nothing that does not pass verification**.

```
setup <epub>  →  glossary <slug>  →  run <slug>  →  llm-check <slug>
                 (pin the terms)      (translate+verify+repair+EPUB)
```

Guiding principle: **deterministic first, the model as a last resort**. A dropped tag
is restored by copying it from the original (zero tokens); only what genuinely needs
language goes to the model, and always for a **single block**, never a whole chapter.

The glossary works on three fronts:
1. **prevention** — the terms go into the translation prompt;
2. **detection** — a block holding the source term but not the required rendering is a
   `glossary` defect: `verify` fails (deterministic, free);
3. **correction** — `run` re-translates **only the blocks** with that term.

File: `books/<slug>/glossary.toml`, per book, optional (absent = no effect at all).

## Procedure

### 1. Find the candidates (free, no LLM)

```bash
ai-epub-translator glossary <slug> --suggest
```

It looks for **frequent terms one letter apart** — the exact shape of the danger. On
moby-dick it finds them by itself:

```
  esoterisme (60×)  vs  exoterisme (45×)
  esoterique (35×)  vs  exoterique (50×)
```

Many pairs are graphic coincidences (`contre`/`centre`): you discard those.

### 2. Check against the real text — this is your job

**Trust neither the heuristic nor the model: look at the text.** For each candidate:

```bash
b=books/<slug>
# does the term really occur in the original? how often?
grep -oiE "\bexotéri\w*" $b/.work/.work/original/**/*.html | wc -l

# how does the translation render it? (the decisive datum)
grep -ohiE "\b(esoteri|essoteri)\w*" $b/target/**/*.html | sort | uniq -c

# read the context: what does it mean there?
grep -ohiE '.{60}\bexotérisme.{60}' $b/.work/.work/original/**/*.html | sed 's/<[^>]*>//g' | head -3
```

The alarm signal: the original has **two** distinct terms, the translation has **one**.
Real example: 32 `exotéri*` + 25 `ésotéri*` → 59 "esoteri*" and **zero** "essoteri*".
They were fused.

### 3. Decide the rendering (judgement, not automation)

Criteria:
- **use the attested technical term** of the discipline, not the literal translation
  (`intellection` → "intellezione", not "intellettualità");
- **make sure it does not collide** with another term of the book. Real trap:
  `intellection` (19×) and `intellectualité` (14×) coexist in Schuon and are different
  concepts — mapping both onto one word recreates the very bug you are fixing;
- **prefer the rendering the model already uses**, when it is right: if it gets it
  right 8 times out of 9, the glossary only has to make the 9th deterministic;
- **pin both terms of a pair**, so they stay distinct by construction.

### 4. Write the glossary

```bash
ai-epub-translator glossary <slug> --add "sperm whale=capodoglio"
ai-epub-translator glossary <slug> --add "whaleboat=lancia baleniera"
```

`--add` is **the only command that writes**. Inflected forms are covered by the stem:
`essoterismo` also matches "essoterici", "essoteriche".

### 5. Check the effect

```bash
ai-epub-translator glossary <slug>     # violations per term
ai-epub-translator verify <slug>        # which blocks now fail
ai-epub-translator run <slug>           # re-translates just those blocks
```

A good glossary shows **✗ only on the terms that are genuinely wrong** and **✓ on the
ones already right**: if everything is ✗, the rendering you chose is probably wrong.

## Alternative: ask the local model

```bash
ai-epub-translator glossary <slug> --extract
```

Sends original+translation to gemma and asks which terms it gets wrong. Useful as a
lead, but it **prints candidates without saving**: it is the same model that makes the
mistakes proposing the fixes. A wrong entry propagates **everywhere** (prompt *and*
check), so confirmation stays human. `--extract --yes` accepts them all.

If you are a large model, **do steps 1–3 yourself**: you can see the text and you know
the terminology better than gemma can judge itself.

## Rules for the agent

- **Never invent a term by conjecture.** If you cannot find the evidence in the text
  (original and translation), do not add it. A wrong glossary is worse than none: it
  fails `verify` on correct translations and re-translates good blocks.
- **One term per concept.** Never two concepts on the same rendering.
- **Few entries, targeted.** A glossary is not a dictionary: only the terms the model
  gets *systematically* wrong and whose error *changes the meaning*.
- **Verify after writing.** `glossary <slug>` must show ✓ on the terms already right:
  that is the proof the check is precise and not a blanket alarm.
- **The book rules.** The terminology of the discipline and the author's usage come
  before any general translation rule.
- **A term right in the prose can be wrong in a name.** "archetypal" is *archetipico*
  everywhere except inside the journal title *Archetypal Psychology*. Do not unpin the
  term: name the context under `[exceptions]` in `glossary.toml`
  (`"archetypal" = ["Archetypal Psychology"]`) and the check skips it there.

## Complete example (real case)

```toml
# books/moby-dick/glossary.toml
[terms]
"exotérique"      = "essoterico"       # ✗ 37 blocks to repair
"exotérisme"      = "essoterismo"      # ✗ 27 blocks
"intellection"    = "intellezione"     # ✗ 7 blocks
"intellectualité" = "intellettualità"  # ✓ already right: pinning guards it
"ésotérique"      = "esoterico"        # ✓ already right
"ésotérisme"      = "esoterismo"       # ✓ already right
```

The ✓ entries are not useless: they **stop** a future re-translation from getting them
wrong, and keep the concepts apart by construction.
