"""``ai_epub_translator`` — a self-contained EPUB translation harness.

The package translates an EPUB chapter by chapter by calling a local LLM,
checks translation quality deterministically (hard-fail), and — rather than
re-translating a whole file when the model corrupts some markup — **repairs
the broken spot surgically**.

Modules
-------
config     configuration loading (harness + per-book) via a tiny TOML parser
epub       OPF/spine discovery and archive unpacking
llm        LLM call (OpenAI-compatible) with streaming output
xhtml      tolerant XHTML lexer (parses even broken markup) — the foundation
structdiff structural diff original↔translated + defect localization
repair     deterministic repairs + targeted per-block LLM correction
verify     deterministic quality checks (hard-fail) — the final gate
state      resume state (done/failed) + pending-output sidecar
logs       structured logging (human-readable + JSONL)
cli        command-line entry point with subcommands

The CLI entry point is :func:`ai_epub_translator.cli.main` (imported explicitly by the
``main.py`` launcher); it is not re-exported here to avoid import side effects.
"""


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("ai-epub-translator")
    except Exception:                                 # noqa: BLE001 — not installed
        return "0+local"


__version__ = _version()
