"""The Homebrew formula is generated (``tools/brew_formula.py``) and committed, so
it can drift from ``pyproject.toml`` between one release and the next. These checks
are offline: they read the two files and compare what has to agree — the version in
the sdist URL, the licence, and an ``lxml`` pin that satisfies the dependency.

The formula itself is built for real by ``brew install``; what cannot be caught
there is a formula that installs the *previous* version perfectly.
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FORMULA = os.path.join(ROOT, "Formula", "ai-epub-translator.rb")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _field(text: str, name: str) -> str:
    m = re.search(rf'^\s*{name} "([^"]+)"', text, re.M)
    assert m, f"no {name} in the formula"
    return m.group(1)


def _version(text: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", text)[:3])


@unittest.skipUnless(os.path.isfile(FORMULA), "no Formula/ (running from the sdist)")
class TestBrewFormula(unittest.TestCase):

    def setUp(self):
        self.formula = _read(FORMULA)
        self.pyproject = _read(os.path.join(ROOT, "pyproject.toml"))

    def test_the_formula_packages_the_current_version(self):
        version = re.search(r'^version = "([^"]+)"', self.pyproject, re.M).group(1)
        url = _field(self.formula, "url")
        self.assertIn(f"ai_epub_translator-{version}.tar.gz", url,
                      "regenerate: python3 tools/brew_formula.py")
        self.assertRegex(_field(self.formula, "sha256"), r"^[0-9a-f]{64}$")

    def test_the_licence_and_the_description_match_the_package(self):
        self.assertEqual(_field(self.formula, "license"), "MIT")
        desc = _field(self.formula, "desc")
        self.assertLessEqual(len(desc), 80)                  # Homebrew's limit
        self.assertFalse(desc.lower().startswith(("a ", "an ", "the ", "ai epub")))

    def test_lxml_is_pinned_and_satisfies_the_dependency(self):
        floor = re.search(r'dependencies = \["lxml>=([\d.]+)"\]', self.pyproject).group(1)
        m = re.search(r'resource "lxml" do\s+url "([^"]+)"\s+sha256 "([0-9a-f]{64})"',
                      self.formula)
        self.assertIsNotNone(m, "the lxml resource is missing from the formula")
        pinned = re.search(r"lxml-([\d.]+)\.tar\.gz", m.group(1)).group(1)
        self.assertGreaterEqual(_version(pinned), _version(floor))

    def test_the_build_dependencies_lxml_needs_are_declared(self):
        # lxml is compiled from its sdist: without these two it does not build.
        for dep in ("libxml2", "libxslt"):
            self.assertIn(f'depends_on "{dep}"', self.formula)
        self.assertRegex(self.formula, r'depends_on "python@3\.\d+"')


if __name__ == "__main__":
    unittest.main()
