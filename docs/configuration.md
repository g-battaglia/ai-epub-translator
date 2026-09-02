# Configuration

`ai-epub-translator config init` writes a commented template to
`~/.config/ai-epub-translator/config.toml` (`%APPDATA%\ai-epub-translator\` on
Windows). Set the server and the model; the rest has sensible defaults:

```toml
[model]
base_url = "http://localhost:11434/v1"   # Ollama; LM Studio 1234, llama-server 8080, omlx 8000…
model = "gemma-4-26b"                    # as the server names it (`doctor` lists them)

[languages]
source = "english"                       # defaults for `setup`
target = "italian"

[paths]
library = "~/Books/translations"         # default: ~/.local/share/ai-epub-translator/books
```

A book overrides anything in its own `book.toml`; `ai-epub-translator config show
<slug>` prints every effective value with the layer it came from (defaults, user,
book, environment). `AI_EPUB_TRANSLATOR_BASE_URL` / `_MODEL` / `_BOOKS` /
`_CONFIG` override from the environment, `--base-url` / `--model` / `--books` /
`--config` from the command line.

### Servers

| server | `base_url` | notes |
|---|---|---|
| Ollama | `http://localhost:11434/v1` | `ollama pull <model>`; model id = the Ollama name |
| LM Studio | `http://localhost:1234/v1` | start the server in the Developer tab |
| llama.cpp `llama-server` | `http://localhost:8080/v1` | any model id is accepted |
| vLLM | `http://localhost:8000/v1` | the served model name |
| omlx (Apple silicon) | `http://localhost:8000/v1` | what this harness was measured on |

Anything that speaks `POST /v1/chat/completions` with streaming works.

## Every setting

Every key of the user config, overridable per book in `book.toml`:

```toml
[model]      model, base_url               # any OpenAI-compatible server
[languages]  source, target                # names or ISO codes; the lang attribute is derived
[register]   default = "tu"                # how the text addresses the reader
[paths]      library                       # where the books are
[verify]     truncation_ratio, block_text_ratio, block_text_min, check_max_chars
[units]      batch_chars, retries          # prose per call; re-asks per rejected unit
[repair]     block_retries                 # gate rewrites (polish, leftovers)
[llm]        retries, backoff_s
[ui]         progress = "percent"          # stream | percent | both
[detection]  code_class_hints              # CSS classes that mark code
```

## Precedence

Defaults → user config → the book's `book.toml` → environment (`AI_EPUB_TRANSLATOR_BASE_URL`, `_MODEL`, `_BOOKS`, `_CONFIG`) → command-line flags (`--base-url`, `--model`, `--books`, `--config`). `ai-epub-translator config show [slug]` prints every effective value with its source.

Nothing is read from the directory you happen to be in: the same command means the
same library and the same settings from anywhere.

## The library

One folder per book, wherever `[paths] library` points — by default
`~/.local/share/ai-epub-translator/books` (`%LOCALAPPDATA%` on Windows).
Everything you edit or want is at the top level; the machinery lives in `.work/`,
which you never need to open.

```
<library>/
└── <slug>/                   one book — created by `ai-epub-translator setup`
    ├── book.toml                the language pair, where the EPUB came from, overrides
    ├── glossary.toml            terms the model must render a given way (optional)
    ├── <slug>.<lang>.epub       the finished translation (written by `run`)
    └── .work/                   internals: safe to delete, costs a re-translation
        ├── original/            the unpacked EPUB, pristine — the baseline of every check
        ├── target/              the unpacked EPUB being translated in place
        ├── logs/                translate.log (one line per file), files.jsonl (records)
        ├── cache.sqlite         every translated unit, the instant it validates
        └── state.json           which files are done, which failed and why
```

A book folder is self-contained: move it to another library and it keeps its
progress, its cache and its glossary.
