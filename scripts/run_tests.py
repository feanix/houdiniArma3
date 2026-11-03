"""Run the houdiniArma3 unittest suite.

Works under system CPython (3.12+) and under Houdini's hython (3.13):

    python scripts/run_tests.py
    "E:/SteamLibrary/steamapps/common/Houdini Indie/bin/hython.exe" scripts/run_tests.py

Puts <repo>/python on sys.path, discovers tests/test*.py and exits non-zero
when any test fails or errors. -q (quiet) or -v (verbose) set the verbosity.
Other arguments name what to run instead of everything: a module, class or
test id, with or without the "tests." prefix (an unknown name fails the run), for example
    run_tests.py test_new_p3d_batch
    run_tests.py tests.test_new_p3d_batch.TestNewP3d.test_lods_are_resolution_1_geometry_and_memory
Standard library only.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON_DIR = os.path.join(REPO_ROOT, "python")
TESTS_DIR = os.path.join(REPO_ROOT, "tests")


def _load_named(name):
    """Load one test id: a module, class or test, with or without the leading ``tests.``
    (``test_foo``, ``tests.test_foo.TestX``, ``test_foo.TestX.test_y``). A bad name fails loudly."""
    if not name.startswith("tests."):
        name = "tests." + name
    # An unknown name comes back as a failing test, so the run exits non-zero.
    return unittest.defaultTestLoader.loadTestsFromName(name)


def main(argv):
    print("Python %s" % sys.version.replace("\n", " "))
    print("Executable: %s" % sys.executable)
    sys.stdout.flush()

    verbosity = 1
    if "-v" in argv:
        verbosity = 2
    elif "-q" in argv:
        verbosity = 0
    names = [arg for arg in argv if not arg.startswith("-")]

    if PYTHON_DIR not in sys.path:
        sys.path.insert(0, PYTHON_DIR)

    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)

    if names:
        suite = unittest.TestSuite(_load_named(name) for name in names)
    else:
        suite = unittest.defaultTestLoader.discover(
            start_dir=TESTS_DIR, pattern="test*.py", top_level_dir=REPO_ROOT
        )
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
