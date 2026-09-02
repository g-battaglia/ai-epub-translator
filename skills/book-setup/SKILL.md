---
name: book-setup
description: >-
  Configure a new book for the EPUB translation harness (this repo), end to end:
  unpack the EPUB, decide the languages and register, and build the terminology
  glossary from measured evidence plus a web lookup of the attested renderings.
  Use whenever a new EPUB has to be added to the library, or when a book's glossary
  has to be built or revised. Executable by any agent (opencode, PI Mono, Claude
  Code) driven by any model.
---

# book-setup — configure a new book for the harness

Run this to set up a new book end to end. It works with **any coding agent**
(opencode, PI Mono, Claude Code) and **any model**, small ones included: every step is
a command to run or a rule to apply, and the one thing that needs knowledge the model
does not have — *what the target language actually calls this concept* — is looked up
**on the web with your own tools**, not recalled from memory.

Nothing here lives inside the harness. `ai-epub-translator` translates books; deciding a book's
terminology is your job, and it is done with the commands below plus a browser.

## What you are producing

Two files in the book's folder — `ai-epub-translator path <slug>` prints where it is:

| file | what it holds |
|---|---|
| `book.toml` | source/target language, register — written by `setup` |
| `glossary.toml` | the pinned terms + free-form notes — **the whole point** |

A glossary term does three things: it goes into every translation prompt
(prevention), it makes `verify` fail on a block that ignores it (detection), and it
makes `run` re-translate just that block (correction). Notes only do the first, which
is why conventions go in the notes and hard rules go in the terms.

**A glossary is not a dictionary.** Pin only what the model gets wrong *and* what you
have confirmed. Five entries that are right beat forty that are guessed: a wrong entry
fails `verify` on correct translations and sends good blocks back to be re-translated.

---

## 1. Set the book up

```bash
ai-epub-translator setup "/path/to/The Book.epub" --source english --target italian
```

Use plain English language names (`english`, `french`, `bulgarian`, `german`). The
slug comes from the filename; override it with `--slug` when the filename is a mess
(z-library downloads usually are). This unpacks the EPUB into `target/`, keeps a
pristine `original/` and writes `book.toml`.

## 2. Read the book

```bash
python3 - "$(ai-epub-translator path <slug>)" <<'EOF'
import glob, re, sys
for f in sorted(glob.glob(sys.argv[1] + '/.work/original/**/*.*htm*', recursive=True))[:12]:
    t = re.sub(r'<[^>]+>', ' ', open(f, encoding='utf-8', errors='replace').read())
    print(f[-40:], '|', ' '.join(t.split())[:90])
EOF
```

Answer three questions and write the answers down — you will need them in step 4:

- **What field is this?** ("astrology", "traditionalist metaphysics", "C
  programming"). One or two words. This is the *context word* the web lookup needs.
- **What register?** Does the author address the reader as "you"? Then `tu`. Is it an
  academic treatise? Then impersonal. The default is `tu`; change it in `book.toml`
  under `[register]` only if the book is clearly formal.
- **Which words carry the argument?** The vocabulary a reader of that field would
  recognize. **This list is your candidate list for the glossary** — do not decide
  renderings yet, but note every term that is load-bearing, technical, or has a
  non-obvious rendering in the target language. Step 3 measures which of them the
  model actually breaks; step 4 looks up the right rendering.

## 3. Confirm the term is really broken — five runs, not one

There is no pre-translation of a sample chapter. The empirical evidence is this check,
which translates **one real paragraph** from the book five times. It is targeted and
fast, and it replaces a throwaway sample run entirely.

For each candidate from step 2 that you suspect the model might fumble, run it:

```bash
uv run --with ai-epub-translator python3 - <<'EOF'
import glob, re
from ai_epub_translator import units as U
from ai_epub_translator.cli import book_dir
from ai_epub_translator.config import merged_config

SLUG, TERM = '<slug>', '<the suspect word>'
BD = book_dir(SLUG)                      # the book's folder, wherever the library is
cfg = merged_config(BD)
block = None
for f in sorted(glob.glob(f'{BD}/.work/original/**/*.*htm*', recursive=True)):
    for m in re.finditer(r'<p\b.*?</p>', open(f, encoding='utf-8', errors='replace').read(), re.S):
        txt = ' '.join(re.sub(r'<[^>]+>', ' ', m.group(0)).split())
        if 200 < len(txt) < 800 and re.search(r'\b%s\b' % TERM, txt, re.I) and not block:
            block = m.group(0)
for i in range(5):
    out, reasons = U.translate_paragraph(block, cfg)
    print(i + 1, ' '.join(re.sub(r'<[^>]+>', ' ', out).split())[:150] if not reasons else reasons)
EOF
```

Read the five outputs. **5/5 correct → do not pin it**, the model already knows this
word and an entry would only add risk. Wrong or unstable → go to step 4.

Suspect first the terms with a non-obvious target rendering, and terms the two
languages spell alike but use differently. Common words the model gets right every
time cost nothing to leave alone.

## 4. Find the attested rendering — on the web, not from memory

This is the step that makes the whole procedure work with a small model: **do not ask
the model what the term should be, look it up.** Use your web search / fetch tool.

**First move — the language link.** Wikipedia articles for the same concept in two
languages are linked by editors of both, so the target-language title *is* the term
of art, not a translation of the words. Fetch these two URLs (JSON, no key needed):

```
https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch=TERM+FIELD&srlimit=4&format=json
https://en.wikipedia.org/w/api.php?action=query&prop=langlinks&titles=ARTICLE&lllang=it&redirects=1&format=json
```

Replace `en`/`it` with the source and target language codes, `TERM+FIELD` with the
term plus the field word from step 2 (`trine+astrology`), and `ARTICLE` with a title
from the first response. The `langlinks` value is your candidate — strip any
disambiguator in brackets: *Aspetto (astrologia)* → **aspetto**.

**Second move — confirm it in the target language.** Many terms are a *section* of a
wider article and have no page of their own, so the language link cannot reach them
(`trine` lives inside "Astrological aspect"). Then propose a rendering and check it
exists as a term of that field:

```
https://it.wikipedia.org/w/api.php?action=query&list=search&srsearch=CANDIDATE+FIELD&srlimit=3&format=json
https://it.wikipedia.org/w/api.php?action=query&prop=extracts&exintro=1&explaintext=1&titles=ARTICLE&format=json
```

*Trigono astrologia* returns **Trigono (astrologia)** with a definition that names the
120° aspect — confirmed. An invented word returns nothing on topic.

A plain web search for `"<term>" <field> <target language>` works too; prefer sources
that belong to the field (an association, a standard translation of the same author, a
specialist dictionary) over a generic bilingual dictionary, which gives the everyday
sense and not the term of art.

**If you cannot confirm it, do not pin it.** Report it to the user instead. An
unconfirmed entry is worse than no entry.

## 5. Check the entry is safe before adding it

Four hazards, all cheap to check, each one has bitten this project:

1. **Substring collision on the source side.** The check fires whenever the source
   term appears *anywhere* in a block, matched as a substring. `orb` also occurs
   inside `absorb` and `orbit`, so pinning it fails verification on correct
   translations. Test it:
   ```bash
   grep -ohiE "\w*<term>\w*" "$(ai-epub-translator path <slug>)"/.work/original/**/*.*html | sort | uniq -c | sort -rn | head
   ```
   If the term appears mostly inside longer words, do not pin it — put it in the notes.
2. **Stem masking on the target side.** The rendering is matched on its stem (the last
   four characters are dropped when it is longer than seven), so `esoterismo` →
   `esoter` also matches the English `esoterism`. If the source term and its rendering
   share a stem, the check can be satisfied by the untranslated word. Prefer a
   rendering that does not.
3. **Two concepts, one rendering.** Never map two different source terms onto the same
   word — that recreates the exact bug the glossary exists to prevent. Check the
   rendering you chose is not already the right rendering of another term in the book.
4. **A rare term is not worth the risk.** Count the occurrences first; a word that
   appears twice belongs in the notes.

```bash
ai-epub-translator glossary <slug> --add "trine=trigono"      # the only writing path
```

Inflected forms are covered by the stem: `trigono` also matches `trigoni`.

## 6. Write the notes

`--add` writes the terms; open the book's `glossary.toml` and add the `[notes]`
section by hand. Notes travel into **every** translation prompt but are never checked,
so this is where conventions go — everything you confirmed in step 4 that does not
deserve the strictness of a pin:

```toml
[terms]
"trine" = "trigono"

[notes]
text = """
Howard Sasportas, "The Twelve Houses" — psychological astrology. The register is warm
and direct: the author addresses the reader as "you". Keep that voice.

The vocabulary is fixed by the discipline — never translate these literally:
- house -> casa · cusp -> cuspide · Midheaven / MC -> Medio Cielo (MC)
- aspects: sextile -> sestile · square -> quadrato · trine -> TRIGONO

TRINE is the one word this model gets wrong every time: left alone it writes "si
trine", "trinari", which is not Italian. It is a noun (a trine to Pluto -> un trigono
a Plutone) and a VERB (planets trine each other -> sono in trigono tra loro).
"""
```

Write the notes in English, like the rest of the repo. Say *why*, not just *what*: a
trap, a distinction, a convention of the field.

## 7. Hand it over to `run`

```bash
ai-epub-translator run <slug>
```

That is all the user needs from here: translate, verify, repair, judge, build the
EPUB. It is resume-safe — stop and relaunch anytime; chapters already done are kept.
Launch it in the background writing to a log and watch the log rather than polling.

Once some chapters exist, `ai-epub-translator glossary <slug>` lists each term with ✓ or the count
of translated blocks that still violate it:

```bash
ai-epub-translator glossary <slug>      # ✓ / ✗ per term, on the chapters translated so far
```

A term already rendered right shows ✓. **If everything is ✗, your rendering is
probably the wrong one** — go back to step 4 rather than forcing it.

## 8. Refine the glossary once chapters exist (optional)

The glossary built in steps 3–6 covers what you foresaw. Once `run` has translated one
or two chapters, three tools — all of which need a translated chapter to analyse — can
surface terms the upfront pass missed. Add what they find with `--add`, then `run`
re-translates just the flagged blocks.

```bash
ai-epub-translator glossary <slug> --suggest     # deterministic, free
```

Frequent source terms **one letter apart** — the shape of the worst trap
(`esoterisme` / `exoterisme`: opposite meanings, one letter, in a book about their
distinction). Most pairs it reports are coincidences; keep the ones that are both
real terms of the field.

```bash
ai-epub-translator glossary <slug> --extract     # asks the local model
```

A lead, **never an authority**: it is the same model that makes the mistakes. Every
candidate it proposes still goes through step 4.

**Source words left untranslated** — the strongest signal, and free. This prints the
words that survive from the original into the translation, each with the sentence it
sits in, so you can judge every line at a glance:

```bash
python3 - "$(ai-epub-translator path <slug>)" <<'EOF'
import glob, os, re, sys
from collections import Counter
BD = sys.argv[1]
strip = lambda s: ' '.join(re.sub(r'<[^>]+>', ' ', s).split())
src, out, sample = Counter(), Counter(), {}
for t in sorted(glob.glob(f'{BD}/.work/target/**/*.*htm*', recursive=True)):
    o = t.replace('/target/', '/original/', 1)
    if not os.path.isfile(o):
        continue
    tt, oo = (open(p, encoding='utf-8', errors='replace').read() for p in (t, o))
    if tt == oo:
        continue                                   # not translated yet
    # ignore what is italicized on purpose: foreign terms, titles
    plain = strip(re.sub(r'<(i|em|cite)\b[^>]*>.*?</\1>', ' ', tt, flags=re.S))
    for m in re.finditer(r"\b[a-z]{4,}\b", plain):
        w = m.group(0)
        out[w] += 1
        sample.setdefault(w, (os.path.basename(t), plain[max(0, m.start() - 45):m.end() + 45]))
    for w in re.findall(r"\b[a-z]{4,}\b", strip(oo).lower()):
        src[w] += 1
# a word carried over is rarer in the translation than in the original; a word that
# simply exists in both languages ("come", "cause") is not
cand = [(w, n, src[w]) for w, n in out.items() if src.get(w, 0) >= 3 and n <= src[w] * 0.5]
for w, n, s in sorted(cand, key=lambda x: -x[1])[:12]:
    f, ctx = sample[w]
    print(f"{w}  ({n}x here, {s}x in the original)  {f}\n    …{ctx}…")
EOF
```

Lowercase only (proper names are not the defect) and ranked. Most lines will be words
that exist in both languages — Italian plurals in *-e* (`cause`, `note`, `false`),
verb forms (`serve`, `veda`) — and the context makes them obvious to discard. What
remains is real. On the Guénon book this printed, ninth in the list:

```
quite  (4x here, 93x in the original)  Chapter29.xhtml
    …queste possono essere spiegate quite naturalmente e senza ricorrere a…
```

*This is also how `trine` was found in* The Twelve Houses: the model was writing "si
trine", "trinari", which is not Italian at all.

---

## Rules

- **Never invent a term.** No confirmation from step 4, no entry. Tell the user what
  you could not confirm.
- **Measure before you pin** (step 3) and **check before you add** (step 5). Both are
  cheap; both catch entries that would make the book worse.
- **Few entries.** Only terms the model gets systematically wrong *and* whose error
  changes the meaning.
- **The book rules.** The field's terminology and the author's usage come before any
  general translation rule.
- **Do not touch the rest of the config.** Retries, batch sizes and the quality
  gate are the harness's business and already work.
- **Book content never enters a repository** — it is copyrighted. The library lives
  outside any checkout; never commit a chapter, an EPUB or a book's slug.
