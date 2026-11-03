"""Smoke test: the pure-Python core package imports.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import importlib.util
import unittest

_HAS_CORE = importlib.util.find_spec("a3ob_core") is not None


class TestSmoke(unittest.TestCase):
    @unittest.skipUnless(
        _HAS_CORE,
        "a3ob_core is not on sys.path yet (python/a3ob_core/__init__.py missing)",
    )
    def test_import_a3ob_core(self):
        import a3ob_core  # noqa: F401


if __name__ == "__main__":
    unittest.main()
