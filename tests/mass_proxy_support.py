"""Helpers for the tests that cook the real A3OB assets in hython. Not a test file (the name does not start with test).

The assets in ``houdini/otls`` are installed into the running session only (``hou.hda.installFile``): nothing is
written to the user's Houdini preferences.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import collections
import os
import tempfile
import unittest

from tests.hou_support import HAS_HOU, REPO_ROOT, hou, install_all_assets

Result = collections.namedtuple("Result", "errors warnings messages")

_installed = False


def install_assets() -> None:
    """Install every generated asset of ``houdini/otls`` into this session (once)."""
    global _installed
    if _installed:
        return
    # hou_support owns installs; this holds the files for the whole run, so other modules cannot uninstall them.
    install_all_assets()
    _installed = True


def cook(node) -> Result:
    """Cook a node and return its errors, warnings and messages (a cook error does not raise here)."""
    try:
        node.cook(force=True)
    except hou.OperationFailed:
        pass
    return Result(tuple(node.errors()), tuple(node.warnings()), tuple(node.messages()))


def make_source(network, geo):
    """A File SOP in ``network`` that outputs a copy of ``hou.Geometry`` ``geo`` (it is saved to a temp file)."""
    handle, path = tempfile.mkstemp(suffix=".bgeo", prefix="a3ob_test_")
    os.close(handle)
    geo.saveToFile(path)
    node = network.createNode("file")
    node.parm("file").set(path.replace("\\", "/"))
    node.setUserData("a3ob_test_file", path)
    return node


class NodeTestCase(unittest.TestCase):
    """Gives each test a fresh geometry network and removes its temp files afterwards."""

    @classmethod
    def setUpClass(cls):
        if HAS_HOU:
            install_assets()

    def setUp(self):
        self.network = hou.node("/obj").createNode("geo")

    def tearDown(self):
        for child in self.network.children():
            path = child.userData("a3ob_test_file")
            if path and os.path.exists(path):
                os.remove(path)
        self.network.destroy()
