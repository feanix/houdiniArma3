"""The suite always tests this checkout's HDAs, even when another copy is already loaded (task 35).

Purpose: catch a regression where a copy of an A3OB HDA loaded earlier (the owner's Houdini package loads the main
checkout's files into every hython) wins over the checkout under test. Hou-only; skips under plain Python.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import shutil
import tempfile
import unittest

from tests import hou_support as hs

ASSET = "a3ob_mass.hda"


@unittest.skipUnless(hs.HAS_HOU, "needs hou (run with hython)")
class CheckoutDefinitionWins(unittest.TestCase):
    def test_other_copy_installed_first(self):
        mine = os.path.join(hs.OTLS_DIR, ASSET).replace("\\", "/")
        key = hs._key(mine)
        held = hs._holders.pop(key, 0)  # take over from earlier test classes, restored below
        hou = hs.hou
        tmp = tempfile.mkdtemp(prefix="a3ob_other_")
        other = os.path.join(tmp, ASSET).replace("\\", "/")
        try:
            if held:  # only installed if an earlier class holds it
                hou.hda.uninstallFile(mine)
            shutil.copytree(mine, other)  # the asset is an expanded folder
            hou.hda.installFile(other)  # the "package" copy, loaded before the suite's install
            for definition in hou.hda.definitionsInFile(other):
                definition.setIsPreferred(True)
            held_now = hs.install_hdas_for_class([mine])
            try:
                for definition in hou.hda.definitionsInFile(mine):
                    node_type = hou.nodeType(definition.nodeTypeCategory(), definition.nodeTypeName())
                    self.assertEqual(hs._key(node_type.definition().libraryFilePath()), key)
                    self.assertTrue(hs._inside_checkout(node_type.definition().libraryFilePath()))
            finally:
                hs.uninstall_hdas(held_now)
        finally:
            if other in [p.replace("\\", "/") for p in hou.hda.loadedFiles()]:
                hou.hda.uninstallFile(other)
            shutil.rmtree(tmp, ignore_errors=True)
            if held:  # put back what earlier classes still hold
                hs._holders.pop(key, None)
                hs.install_hdas_for_class([mine] * held)


if __name__ == "__main__":
    unittest.main()
