"""Command line of scripts/build_user_docs.py: --help and a bad argument must write nothing.

Purpose: catch a regression to hand-rolled argument checks, where an unknown flag would rebuild every file.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "build_user_docs.py")


def run(*args):
    return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, cwd=ROOT)


class TestBuildUserDocsArgs(unittest.TestCase):
    def test_help_prints_usage_and_writes_nothing(self):
        done = run("--help")
        self.assertEqual(done.returncode, 0)
        self.assertIn("usage:", done.stdout)
        self.assertNotIn("wrote ", done.stdout)

    def test_unknown_argument_fails_and_writes_nothing(self):
        done = run("--bogus")
        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn("wrote ", done.stdout)

    def test_check_still_works(self):
        self.assertEqual(run("--check").returncode, 0)


if __name__ == "__main__":
    unittest.main()
