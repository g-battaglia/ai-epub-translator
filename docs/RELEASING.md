# Releasing

Releases are published to PyPI by GitHub Actions through **trusted publishing**
(OIDC): no API token is stored anywhere.

## One-time setup

1. PyPI → Account settings → Publishing → *Add a pending publisher*: project
   `ai-epub-translator`, owner `g-battaglia`, repository `ai-epub-translator`,
   workflow `release.yml`, environment `pypi`. Repeat on test.pypi.org with
   environment `testpypi`.
2. GitHub → Settings → Environments: create `testpypi` and `pypi`; on `pypi`
   require a reviewer, so a tag cannot publish without a click.

## Every release

1. Update `CHANGELOG.md` (move *Unreleased* under the new version and date).
2. Bump `version` in `pyproject.toml` — the only place it lives; the CLI reads it
   through `importlib.metadata`.
3. `git commit -m "release: vX.Y.Z"`, `git tag -a vX.Y.Z -m "vX.Y.Z"`,
   `git push && git push --tags`.
4. The `release` workflow builds the sdist and wheel, checks them with twine,
   waits for the environment approval, publishes to PyPI and attaches the files
   to a GitHub release with generated notes.

Dry run: *Actions → release → Run workflow* with `repository = testpypi`, then
`uvx --index-url https://test.pypi.org/simple/ ai-epub-translator --version`.

## Publishing by hand

The workflow is not the only way in, and the upload step checks PyPI first: if the
version is already there it is skipped, so a hand-made release does not turn the
run red. With an API token (`__token__` as the username):

```bash
uv build
uvx twine check dist/*
export TWINE_USERNAME=__token__
uvx twine upload dist/*          # the token goes in at the password prompt
git push origin vX.Y.Z           # release notes, artefacts, formula refresh
```

Doing it this way has one advantage worth knowing: the formula generated with
`--sdist dist/*.tar.gz` carries the SHA-256 of the very bytes you upload, so it is
already right before the release exists.

## Homebrew

**This repository is its own tap** — there is no separate `homebrew-*` repository,
so the tap has to be added by URL once:

```bash
brew tap g-battaglia/ai-epub-translator https://github.com/g-battaglia/ai-epub-translator
brew install g-battaglia/ai-epub-translator/ai-epub-translator
```

(That is the price of keeping everything here: Homebrew resolves the short name
`g-battaglia/x` to `github.com/g-battaglia/homebrew-x`, which this repository is
not. The repository must be **public** for anyone to tap it.)

`Formula/ai-epub-translator.rb` is generated, never hand-edited. It carries the
SHA-256 of the sdist on PyPI, which exists only after the upload, so **the formula
commit always follows the tag**. The `homebrew` job of the `release` workflow does
it: it waits for PyPI to show the new version, regenerates the formula and commits
it to `main` with the built-in `GITHUB_TOKEN` — no secret to configure.

By hand:

```bash
python3 tools/brew_formula.py --wait 300      # version from pyproject.toml
uv run python -m unittest tests.test_brew
git commit -am "brew: vX.Y.Z" && git push
```

The formula builds a virtualenv on `python@3.13` and compiles `lxml` — the one
runtime dependency — against Homebrew's keg-only `libxml2`/`libxslt`: the install
takes a couple of minutes and there is no bottle. The generator pins `lxml` to its
latest sdist every time it runs.

Try a formula before committing it: put it in a scratch tap
(`brew tap-new giacomo/localtest --no-git`), point its `url` at the local
`dist/*.tar.gz` with `file://`, then `brew install --build-from-source
giacomo/localtest/ai-epub-translator`, `brew test` and
`brew uninstall ai-epub-translator && brew untap giacomo/localtest`.

## Versioning

SemVer. Patch for fixes, minor for new commands or config keys, major for a
change in the library layout or the cache schema that needs a migration.
