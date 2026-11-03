"""Exports always get a .p3d ending (task #26). The helper runs under CPython; the writes need hou.

Purpose: catch a write that leaves a file without the ending (the owner's Quick Export "test_2026..." case), a backup
or temp name that does not follow the final name, and a message that names the wrong path.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import shutil
import tempfile
import unittest

from a3ob_houdini import messages
from a3ob_houdini.p3d_export import with_p3d_extension, write_file
from tests import hou_support as hs
from tests.mass_proxy_support import NodeTestCase, make_source

if hs.HAS_HOU:
    from a3ob_houdini import quick_export as qe, settings as sm
    from a3ob_houdini.nodes import p3d_export as export_node


class TestHelper(unittest.TestCase):
    def test_cases(self):
        for given, wanted in (("model", "model.p3d"), ("model.P3D", "model.P3D"), ("model.p3d", "model.p3d"),
                              ("model.v2", "model.v2.p3d"), ("a/b\\c", "a/b\\c.p3d"), ("", "")):
            self.assertEqual(with_p3d_extension(given), wanted)

    def test_a_variable_that_holds_the_ending_is_not_extended(self):
        # The message and the written file must agree when the path is an environment variable.
        folder = tempfile.mkdtemp(prefix="a3ob_ext_")
        self.addCleanup(shutil.rmtree, folder, True)
        os.environ["A3OB_TEST_MODEL"] = os.path.join(folder, "model.p3d")
        self.addCleanup(os.environ.pop, "A3OB_TEST_MODEL", None)
        self.assertEqual(with_p3d_extension("$A3OB_TEST_MODEL"), "$A3OB_TEST_MODEL")
        os.environ["A3OB_TEST_STEM"] = os.path.join(folder, "stem")
        self.addCleanup(os.environ.pop, "A3OB_TEST_STEM", None)
        self.assertEqual(with_p3d_extension("$A3OB_TEST_STEM"), "$A3OB_TEST_STEM.p3d")
        self.assertTrue(write_file("$A3OB_TEST_MODEL", b"one", messages.Report()))
        self.assertEqual(os.listdir(folder), ["model.p3d"])

    def test_write_file_adds_the_ending_and_the_backup_follows(self):
        folder = tempfile.mkdtemp(prefix="a3ob_ext_")
        self.addCleanup(shutil.rmtree, folder, True)
        report = messages.Report()
        base = os.path.join(folder, "noext")
        self.assertTrue(write_file(base, b"one", report))
        self.assertTrue(write_file(base, b"two", report, backup=True))
        self.assertEqual(sorted(os.listdir(folder)), ["noext.p3d", "noext.p3d.bak"])


@unittest.skipUnless(hs.HAS_HOU, "needs hou (run under hython)")
class TestExtensionInHoudini(NodeTestCase):
    def setUp(self):
        super().setUp()
        folder = tempfile.mkdtemp(prefix="a3ob_ext_")
        self.addCleanup(shutil.rmtree, folder, True)
        self.folder = folder.replace("\\", "/")

    def source_node(self):
        _, geo, _, _ = hs.imported_fixture("sample_2_crate.p3d")
        return make_source(self.network, geo)

    def test_save_to_disk_without_ending_writes_dot_p3d_only(self):
        node = self.network.createNode("a3ob_p3d_export")
        node.setInput(0, self.source_node())
        node.parm("file").set(self.folder + "/noext")
        report = export_node.save(node)
        self.assertEqual(os.listdir(self.folder), ["noext.p3d"])
        self.assertFalse(os.path.exists(self.folder + "/noext"))
        shown = os.path.normpath(self.folder + "/noext.p3d")  # messages show the native slash (task #30)
        self.assertTrue(any(text.endswith("to %s." % shown) for text in report.texts("info")),
                        report.texts("info"))

    def test_quick_export_without_ending_writes_dot_p3d_only(self):
        node = self.source_node()
        path = self.folder + "/test_20261103_1811"
        ok, report = qe.export_node(node, path, sm.LOSSLESS, sm.Settings())
        self.assertTrue(ok, report.items)
        self.assertEqual(os.listdir(self.folder), ["test_20261103_1811.p3d"])
        self.assertEqual(qe.remembered_path(node), path + ".p3d")
        self.assertIn(os.path.normpath(path + ".p3d"), report.texts("info")[-1])


if __name__ == "__main__":
    unittest.main()
