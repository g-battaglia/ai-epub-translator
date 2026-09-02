"""The agent skills follow the Agent Skills layout (skills.sh): one folder per skill
under ``skills/``, a ``SKILL.md`` with ``name`` and ``description`` in its frontmatter.

Agents install them with ``npx skills add g-battaglia/ai-epub-translator``; nothing
is mirrored in the repository, so there is one copy to keep right.
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = ("book-setup", "book-glossary")


def _frontmatter(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert m, f"{path}: no YAML frontmatter"
    fields = {}
    for line in m.group(1).splitlines():
        if re.match(r"[a-z_]+:", line):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


class TestSkillsLayout(unittest.TestCase):

    def test_every_skill_has_a_valid_skill_md(self):
        for skill in SKILLS:
            path = os.path.join(ROOT, "skills", skill, "SKILL.md")
            self.assertTrue(os.path.isfile(path), f"{path} is missing")
            fm = _frontmatter(path)
            self.assertEqual(fm.get("name"), skill)
            self.assertIn("description", fm)

    def test_no_stale_mirrors_are_tracked(self):
        for base in (".claude/skills", ".codex/skills", ".agents/skills", ".opencode/skill"):
            for skill in SKILLS:
                self.assertFalse(
                    os.path.islink(os.path.join(ROOT, base, skill, "SKILL.md")),
                    f"{base}/{skill} is a symlink: mirrors were replaced by skills/")
