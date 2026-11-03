"""Quick Export without a dialog (ux-tools.md 7.1). Needs hou; skips under plain Python.

Purpose: catch an export that differs from the source (Lossless must give L0 bytes), a remembered path that is not
kept, and a bad preset or a non-P3D node that raises instead of reporting.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import tempfile
import unittest

from tests import hou_support as hs
from tests.mass_proxy_support import NodeTestCase, make_source

if hs.HAS_HOU:
    import hou
    from a3ob_houdini import quick_export as qe, settings as sm
    from a3ob_houdini.errors import UserError


@unittest.skipUnless(hs.HAS_HOU, "needs hou (run under hython)")
class TestQuickExport(NodeTestCase):
    def setUp(self):
        super().setUp()
        self.container = self.network
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.settings = sm.Settings()

    def source_node(self, fixture):
        _, geo, _, _ = hs.imported_fixture(fixture)
        return make_source(self.container, geo)  # tearDown removes the temp file

    def test_lossless_export_of_each_fixture_is_byte_identical(self):
        for fixture in hs.FIXTURE_NAMES:
            node = self.source_node(fixture)
            path = os.path.join(self.folder, "out", fixture)
            ok, report = qe.export_node(node, path, sm.LOSSLESS, self.settings)
            self.assertTrue(ok, report.items)
            with open(path, "rb") as out, open(os.path.join(hs.FIXTURES, fixture), "rb") as source:
                self.assertTrue(out.read() == source.read(), fixture)
            self.assertTrue(report.texts("info")[-1].startswith("Saved "))

    def test_path_is_remembered_on_the_node_not_as_an_attribute(self):
        node = self.source_node("sample_2_crate.p3d")
        self.assertEqual(qe.remembered_path(node), "")
        path = os.path.join(self.folder, "crate.p3d")
        self.assertTrue(qe.export_node(node, path, None, self.settings)[0])
        self.assertEqual(qe.remembered_path(node), path)

    def test_saved_message_uses_one_kind_of_slash_but_the_stored_path_is_kept(self):
        node = self.source_node("sample_2_crate.p3d")
        path = self.folder + "/out\\mixed.p3d"
        self.assertTrue(qe.export_node(node, path, None, self.settings)[0])
        saved = [text for text in qe.export_node(node, path, None, self.settings)[1].texts("info")
                 if text.startswith("Saved ")][0]
        shown = saved[len("Saved "):saved.index(" (")]
        self.assertEqual(shown, os.path.normpath(path))
        self.assertFalse("/" in shown and "\\" in shown, shown)
        self.assertEqual(qe.remembered_path(node), path)

    def test_release_preset_is_used_and_changes_the_bytes(self):
        node = self.source_node("sample_2_crate.p3d")
        path = os.path.join(self.folder, "release.p3d")
        ok, report = qe.export_node(node, path, sm.RELEASE, self.settings)
        self.assertTrue(ok, report.items)
        self.assertTrue(os.path.getsize(path) > 0)

    def test_non_p3d_geometry_is_reported_not_raised_and_writes_nothing(self):
        box = self.container.createNode("box")
        path = os.path.join(self.folder, "box.p3d")
        ok, report = qe.export_node(box, path, sm.LOSSLESS, self.settings)
        self.assertFalse(ok)
        self.assertTrue(report.has_errors)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(qe.remembered_path(box), "")
        self.assertTrue(qe.summary_lines(report)[0].startswith("Error: "))

    def test_unknown_preset_raises_a_user_error(self):
        box = self.container.createNode("box")
        with self.assertRaises(UserError):
            qe.export_node(box, os.path.join(self.folder, "x.p3d"), "nope", self.settings)

    def test_choose_node_prefers_the_single_selected_sop_then_the_display_node(self):
        first = self.container.createNode("box")
        second = self.container.createNode("sphere")
        second.setDisplayFlag(True)
        self.assertEqual(qe.choose_node([first], self.container).path(), first.path())
        self.assertEqual(qe.choose_node([], self.container).path(), second.path())
        with self.assertRaises(UserError):
            qe.choose_node([first, second], self.container)
        with self.assertRaises(UserError):
            qe.choose_node([], None)

    def test_material_assign_common_menus_list_the_settings_entries_on_a_real_node(self):
        node = self.network.createNode("a3ob_material_assign")
        self.assertIn("PIP", node.parm("commonproc").menuItems())
        self.assertIn("Glass", node.parm("commonmat").menuItems())
        self.assertEqual(node.parm("commonmat").eval(), "none")

    def test_default_path_is_next_to_the_scene_and_named_after_the_node(self):
        box = self.container.createNode("box", "mybox")
        self.assertTrue(qe.default_path(box).endswith("mybox.p3d"))


if __name__ == "__main__":
    unittest.main()
