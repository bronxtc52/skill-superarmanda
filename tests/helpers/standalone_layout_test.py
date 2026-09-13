#!/usr/bin/env python3
"""Acceptance checks for the installable standalone skill layout."""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "superarmanda"


class StandaloneLayoutTest(unittest.TestCase):
    def test_skill_is_installable_from_the_canonical_skill_directory(self):
        expected = {
            "SKILL.md",
            "references/profiles.md",
            "references/workflow.md",
            "references/review-contract.md",
            "references/pr-review.md",
            "schemas/review-result.schema.json",
            "scripts/state.py",
            "scripts/review.py",
            "scripts/codex_review.py",
            "scripts/pr_review.py",
        }
        self.assertEqual(
            {path.relative_to(SKILL).as_posix() for path in SKILL.rglob("*") if path.is_file()},
            expected,
        )


if __name__ == "__main__":
    unittest.main()
