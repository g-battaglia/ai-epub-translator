# AI EPUB Translator

Translate EPUB ebooks with a **local LLM** — offline, private, free, no API key.
Point it at any OpenAI-compatible server (Ollama, LM Studio, llama.cpp, vLLM, omlx),
pick a language pair, and get back a translated EPUB with every formatting detail of
the original intact. Built and measured on a small model (gemma-4-26b), so it does
not need a frontier model to translate a whole book well.

**What makes it different: the model never sees the markup.** Each chapter is cut
into prose units; inline tags become placeholders (`<g1>…</g1>`), the model
translates the prose, and the original tags are spliced back deterministically.
Footnotes, italics, links, page breaks, code blocks and images survive by
construction — and **nothing is saved until it passes verification**.

## Features

- **Offline ebook translation** with any local LLM served over an OpenAI-compatible
  API — your books never leave your machine.
- **Formatting preserved**: the XHTML structure, attributes, ids, hrefs, footnote
  markers and page-break markers are copied verbatim, never regenerated.
- **Verified, not hoped for**: every translated unit is checked (placeholders,
  length, no summarizing, glossary terms) and every file is diffed against the
  original before it is written.
- **Quality gate**: the model then judges each chapter for faithfulness; chapters that
  read wrong are polished with the judge's own note, and a book that still reads
  wrong is not packed.
- **Terminology glossary**: pin the terms a model gets wrong every time; they go into
  the prompt, are checked per unit, and only the units that violate them are redone.
- **Resume-safe**: every unit is cached the instant it validates. Stop it, crash it,
  edit the glossary — the next run asks only for what is missing.
- **No dependencies** beyond Python 3 and lxml. One command translates a book.

## Install

```bash
uv tool install ai-epub-translator        # or: pipx install ai-epub-translator
ai-epub-translator config init            # writes ~/.config/ai-epub-translator/config.toml
ai-epub-translator doctor                 # is the server up? which models does it offer?
```

On a Mac, Homebrew installs the same command — this repository is its own tap:

```bash
brew tap g-battaglia/ai-epub-translator https://github.com/g-battaglia/ai-epub-translator
brew install g-battaglia/ai-epub-translator/ai-epub-translator
```

Or straight from a checkout: `git clone … && cd ai-epub-translator && uv run main.py …`.
Python ≥ 3.9 and lxml, nothing else.

## Quick start

```bash
ai-epub-translator setup ~/Books/Moby-Dick.epub --source english --target german
ai-epub-translator glossary moby-dick --suggest       # pin the risky terms first (recommended)
ai-epub-translator run moby-dick                      # translate, verify, judge, pack the EPUB
```

`run` prints where the finished `.epub` is (`<library>/moby-dick/moby-dick.de.epub`).
`ai-epub-translator <command> -h` gives help and real examples.

## Configure

`ai-epub-translator config init` writes a commented `~/.config/ai-epub-translator/config.toml`
(`%APPDATA%\ai-epub-translator\` on Windows). Three keys matter:

```toml
[model]
base_url = "http://localhost:11434/v1"   # Ollama; LM Studio 1234, llama-server 8080, omlx 8000…
model = "gemma-4-26b"                    # as the server names it (`doctor` lists them)

[paths]
library = "~/Books/translations"         # default: ~/.local/share/ai-epub-translator/books
```

`ai-epub-translator doctor` tells you what is missing. Every setting, the servers
and the precedence rules: [docs/configuration.md](https://github.com/g-battaglia/ai-epub-translator/blob/main/docs/configuration.md).

## How it works, in one paragraph

A chapter is cut into prose units; inline tags become placeholders
(`<span class="italic"><span>kairos</span></span>` → `<g1>kairos</g1>`); the model
translates the prose in batches; the original tags are spliced back; the file is
diffed against the original; a unit the model got wrong is asked again alone, told
what was wrong. Then the model judges every chapter and the EPUB is built only if
the book reads faithfully. Every unit is cached the instant it validates, so a
Ctrl-C costs one batch. The details, the checks and the recovery paths:
[docs/how-it-works.md](https://github.com/g-battaglia/ai-epub-translator/blob/main/docs/how-it-works.md).

## FAQ

**Does it work offline?** Yes. The only network call is to the LLM server you
configure — a local one by default. No cloud API, no key, no usage fees.

**Which models and servers?** Anything that speaks the OpenAI chat-completions API:
Ollama, LM Studio, llama.cpp's server, vLLM, omlx. The harness was built and measured
on gemma-4-26b; a larger model works too, a much smaller one has not been measured.
Set `[model] base_url` and `model` in `config.toml`.

**Which languages?** Any pair the model handles. Set `--source`/`--target` at setup
(language names: `english`, `french`, `italian`, …). Tested on English, French and
Bulgarian sources into Italian.

**Does it keep the formatting?** Yes, by construction: italics, links, footnotes,
page breaks, images, tables and code are copied from the original, never rewritten
by the model. Only the prose changes (and the `lang` attribute).

**How long does a book take?** Measured on an Apple-silicon Mac with gemma-4-26b:
about 140 characters of prose per second, so a 300-page novel (~600 k characters)
is 1–2 hours, plus the quality gate.

**Can I translate a PDF?** Not directly: convert it to EPUB first (calibre does it),
then translate the EPUB.

**Is the translation good?** The model's — the harness guarantees the structure, checks
what can be checked (nothing summarized, terms rendered), has the model judge every
chapter, and tells you exactly which chapter still reads wrong and why. The last
word on terminology is yours: that is what the glossary is for.

## Documentation

- [How it works](https://github.com/g-battaglia/ai-epub-translator/blob/main/docs/how-it-works.md) — units, placeholders, the checks, the recovery paths, a book start to finish
- [Configuration](https://github.com/g-battaglia/ai-epub-translator/blob/main/docs/configuration.md) — every setting, the servers, precedence
- [The glossary](https://github.com/g-battaglia/ai-epub-translator/blob/main/docs/glossary.md) — pinning the terms a model gets wrong, with exceptions
- [For AI agents](https://github.com/g-battaglia/ai-epub-translator/tree/main/skills) — `npx skills add g-battaglia/ai-epub-translator` installs the book-setup and book-glossary skills; [AGENTS.md](https://github.com/g-battaglia/ai-epub-translator/blob/main/AGENTS.md) is the guide to the code
- [Contributing](https://github.com/g-battaglia/ai-epub-translator/blob/main/CONTRIBUTING.md) · [Releasing](https://github.com/g-battaglia/ai-epub-translator/blob/main/docs/RELEASING.md) · [Changelog](https://github.com/g-battaglia/ai-epub-translator/blob/main/CHANGELOG.md)

## License

MIT. The tool translates books you own, for your own reading; it contains, fetches
and distributes no book. A translation of a copyrighted work is a derivative work:
what you do with it is your responsibility.
