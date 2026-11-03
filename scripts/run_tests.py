"""Run the houdiniArma3 unittest suite.

Works under system CPython (3.12+) and under Houdini's hython (3.13):

    python scripts/run_tests.py
    "E:/SteamLibrary/steamapps/common/Houdini Indie/bin/hython.exe" scripts/run_tests.py

Puts <repo>/python on sys.path, discovers tests/test*.py and exits non-zero
when any test fails or errors. Extra arguments are passed to unittest's
verbosity switch: -q (quiet) or -v (verbose, the default is normal).
Standard library only.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON_DIR = os.path.join(REPO_ROOT, "python")
TESTS_DIR = os.path.join(REPO_ROOT, "tests")


def main(argv):
    print("Python %s" % sys.version.replace("\n", " "))
    print("Executable: %s" % sys.executable)
    sys.stdout.flush()

    verbosity = 1
    if "-v" in argv:
        verbosity = 2
    elif "-q" in argv:
        verbosity = 0

    if PYTHON_DIR not in sys.path:
        sys.path.insert(0, PYTHON_DIR)

    suite = unittest.defaultTestLoader.discover(
        start_dir=TESTS_DIR, pattern="test*.py", top_level_dir=REPO_ROOT
    )
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
