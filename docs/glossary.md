# The glossary

Some errors are invisible to every structural check: the tags match, the length is
right, but the text says the opposite. Real case: the model rendered `exotérisme`
(the exoteric, outward dimension) as "esoterismo" — **its opposite** — collapsing two
distinct concepts into one, ~90 times across three chapters.

A book's `glossary.toml` closes that gap:

```toml
[terms]
"exotérisme"      = "essoterismo"
"exotérique"      = "essoterico"
"intellection"    = "intellezione"
"intellectualité" = "intellettualità"   # correct already: pinning keeps it distinct
```

- **prevention**: the terms go into the translation prompt;
- **detection**: a block with the source term but not the required rendering fails
  `verify` (deterministic, free);
- **correction**: `run` re-translates **only those units**, and rejects answers that
  repeat the mistake.

A term that is right in the prose can be wrong inside a proper name — "archetypal"
is *archetipico*, except in the journal title *Archetypal Psychology*. Name the
context and the check skips it:

```toml
[exceptions]
"archetypal" = ["Archetypal Psychology"]
```

The terminology decision stays human: the model that gets a term wrong is no authority
on how it should be rendered, and a wrong entry propagates everywhere (prompt *and*
check).
