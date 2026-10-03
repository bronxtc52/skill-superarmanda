#!/usr/bin/env python3
"""A wave session exports WAB_* (WAB_DIR, WAB_MAX_RUNS, WAB_RUN_DIR ...). Running the suites
from inside such a session must neither read nor write the live run directory: a subset of
each suite runs in a subprocess with WAB_* aimed at a foreign directory, which must stay
byte-identical."""
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HELPERS = Path(__file__).resolve().parent
SUBSETS = (
    ("superarmanda_state_test.py", ["RunCounter"]),
    ("superarmanda_waves_test.py", ["W1MaxRuns"]),
)


def snapshot(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


class WabEnvIsolation(unittest.TestCase):
    def test_suites_do_not_touch_a_foreign_wab_directory(self):
        with tempfile.TemporaryDirectory() as foreign:
            root = Path(foreign)
            (root / "runs.json").write_text('{"sentinel": 1}\n', encoding="utf-8")
            before = snapshot(root)
            env = dict(os.environ, WAB_DIR=foreign, WAB_MAX_RUNS="1", WAB_RUN_DIR=foreign,
                       WAB_WAVE="W1", WAB_PLAN_SHA256="0" * 64)
            for script, names in SUBSETS:
                proc = subprocess.run([sys.executable, str(HELPERS / script), *names],
                                      env=env, capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, f"{script}\n{proc.stderr[-2000:]}")
            self.assertEqual(snapshot(root), before)


if __name__ == "__main__":
    unittest.main()
